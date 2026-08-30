"""Dashboard graceful-restart coordination shared by API, jobs and scheduler."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Protocol, TypeVar


RESTART_EXIT_CODE = 75
RESTART_SUPERVISED_ENV = "BINGGO_RESTART_SUPERVISED"

_T = TypeVar("_T")


class _ExitAwareServer(Protocol):
    should_exit: bool


class RestartPendingError(RuntimeError):
    """A restart was accepted, so no new mutable work may start."""


class RestartUnavailableError(RuntimeError):
    """The current entry point has no supervisor capable of restarting it."""


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}


class RestartControl:
    """Serialize restart acceptance with all new Job/scheduler start attempts.

    The gate is intentionally process-local.  A successful restart creates a new
    process and therefore a fresh gate while ``active_profile.json`` remains the
    only cross-process switch state.
    """

    def __init__(self) -> None:
        self._gate = threading.RLock()
        self._server: _ExitAwareServer | None = None
        self._restart_pending = False
        self._restart_requested = False

    @contextmanager
    def new_work_guard(self) -> Iterator[None]:
        """Prevent new work from racing between the idle check and shutdown."""

        with self._gate:
            if self._restart_pending:
                raise RestartPendingError("Binggo 正在切换账号并重新启动")
            yield

    def begin_restart(self, transition: Callable[[], _T]) -> _T:
        """Atomically verify restart support, perform a transition and close the gate."""

        with self._gate:
            if self._restart_pending:
                raise RestartPendingError("Binggo 正在切换账号并重新启动")
            if not _env_enabled(RESTART_SUPERVISED_ENV) or self._server is None:
                raise RestartUnavailableError("当前启动方式不支持自动重新启动 Binggo")
            result = transition()
            self._restart_pending = True
            return result

    def register_server(self, server: _ExitAwareServer) -> None:
        """Register the Uvicorn server controlled by this process."""

        with self._gate:
            if self._server is not None and self._server is not server:
                raise RuntimeError("已有 Dashboard 服务实例注册")
            self._server = server
            self._restart_pending = False
            self._restart_requested = False

    def request_restart(self) -> bool:
        """Ask Uvicorn to stop gracefully after the accepting response was sent."""

        with self._gate:
            if not self._restart_pending or self._server is None:
                return False
            self._restart_requested = True
            self._server.should_exit = True
            return True

    def finish_server(self, server: _ExitAwareServer) -> bool:
        """Unregister ``server`` and report whether its supervisor should respawn it."""

        with self._gate:
            if self._server is not server:
                return False
            self._server = None
            return self._restart_requested

    def is_restart_pending(self) -> bool:
        with self._gate:
            return self._restart_pending

    def is_restart_supported(self) -> bool:
        with self._gate:
            return _env_enabled(RESTART_SUPERVISED_ENV) and self._server is not None

    def reset_for_tests(self) -> None:
        """Clear process state.  Production code must never hot-reset this object."""

        with self._gate:
            self._server = None
            self._restart_pending = False
            self._restart_requested = False


restart_control = RestartControl()


__all__ = [
    "RESTART_EXIT_CODE",
    "RESTART_SUPERVISED_ENV",
    "RestartControl",
    "RestartPendingError",
    "RestartUnavailableError",
    "restart_control",
]
