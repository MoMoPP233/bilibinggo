"""V112a: offline supervision of a Profile's tray and service pipe."""

from __future__ import annotations

import io
import subprocess
import threading
import types

import pytest

import binggo_launcher as launcher
from src import tray_shutdown
from src.restart_control import RestartControl, RestartPendingError


class Service:
    def __init__(self, *, timeouts: int = 0, exit_code: int = 0) -> None:
        self.stdin = io.BytesIO()
        self.timeouts = timeouts
        self.exit_code = exit_code
        self.waits: list[float | None] = []
        self.exited = False

    def poll(self) -> int | None:
        return self.exit_code if self.exited else None

    def wait(self, timeout: float | None = None) -> int:
        self.waits.append(timeout)
        if self.timeouts:
            self.timeouts -= 1
            raise subprocess.TimeoutExpired("fake-serve", timeout)
        self.exited = True
        return self.exit_code


class Tray:
    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.stopped = False

    def is_healthy(self) -> bool:
        return self.healthy

    def stop(self) -> bool:
        self.stopped = True
        return True


def setup_supervisor(monkeypatch, service: Service) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: service)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "_show_error", messages.append)
    monkeypatch.setattr(launcher, "_open_current_ui", lambda **_kw: None)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    monkeypatch.setattr(launcher, "OPEN_BROWSER", False)
    return messages


@pytest.mark.skipif(launcher.sys.platform != "win32", reason="Windows tray only")
def test_tray_start_failure_requests_graceful_shutdown(monkeypatch, tmp_path):
    service = Service()
    messages = setup_supervisor(monkeypatch, service)
    monkeypatch.setattr(launcher, "_start_supervised_tray", lambda *_args: None)

    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 1
    assert service.stdin.getvalue() == b"shutdown\n"
    assert service.waits == [None] and messages and "托盘" in messages[0]


@pytest.mark.skipif(launcher.sys.platform != "win32", reason="Windows tray only")
def test_dead_tray_loop_recovers_without_stopping_service(monkeypatch, tmp_path):
    service = Service(timeouts=1)
    messages = setup_supervisor(monkeypatch, service)
    first, recovered = Tray(healthy=False), Tray()
    trays = iter([first, recovered])
    monkeypatch.setattr(launcher, "_start_supervised_tray", lambda *_args: next(trays))

    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0
    assert first.stopped and recovered.stopped
    assert service.stdin.getvalue() == b"" and messages == []
    assert service.waits == [launcher.TRAY_CHECK_INTERVAL_SEC] * 2


@pytest.mark.skipif(launcher.sys.platform != "win32", reason="Windows tray only")
def test_dead_tray_loop_recovery_failure_waits_for_safe_exit(monkeypatch, tmp_path):
    service = Service(timeouts=1)
    messages = setup_supervisor(monkeypatch, service)
    first = Tray(healthy=False)
    trays = iter([first, None])
    monkeypatch.setattr(launcher, "_start_supervised_tray", lambda *_args: next(trays))

    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 1
    assert first.stopped and service.stdin.getvalue() == b"shutdown\n"
    assert service.waits == [launcher.TRAY_CHECK_INTERVAL_SEC, None]
    assert messages and "托盘" in messages[0]


@pytest.mark.skipif(launcher.sys.platform != "win32", reason="Windows tray only")
def test_tray_failure_does_not_discard_already_accepted_profile_restart(monkeypatch, tmp_path):
    from src.restart_control import RESTART_EXIT_CODE

    old, new = Service(exit_code=RESTART_EXIT_CODE), Service()
    messages = setup_supervisor(monkeypatch, old)
    processes = iter([old, new])
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: next(processes))
    trays = iter([None, Tray()])
    monkeypatch.setattr(launcher, "_start_supervised_tray", lambda *_args: next(trays))
    monkeypatch.setattr(launcher, "_wait_for_server_stop", lambda: True)

    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0
    assert old.stdin.getvalue() == b"shutdown\n"
    assert new.stdin.getvalue() == b"" and new.exited
    assert messages and "托盘" in messages[0]


def test_tray_recovery_is_bounded_and_profile_local(monkeypatch):
    from src import windows_tray

    service = Service()
    attempts: list[tuple[str, str]] = []
    class RecoveringTray:
        def __init__(self, profile, instance, _open, _exit):
            attempts.append((profile, instance))
        def start(self):
            return len(attempts) == 2
        def is_healthy(self):
            return True
        def stop(self):
            return True

    monkeypatch.setattr(windows_tray, "WindowsTray", RecoveringTray)
    monkeypatch.setattr(launcher.time, "sleep", lambda _sec: None)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "account-1")
    monkeypatch.setattr(launcher, "INSTANCE_ID", "own-instance")
    assert launcher._start_supervised_tray(service, threading.Event()) is not None
    assert attempts == [("account-1", "own-instance")] * 2
    assert service.stdin.getvalue() == b""


def test_all_failed_tray_attempts_do_not_loop_forever(monkeypatch):
    from src import windows_tray

    service = Service()
    attempts = []
    class FailedTray:
        def __init__(self, *_args):
            attempts.append(1)
        def start(self):
            return False
        def stop(self):
            return True

    monkeypatch.setattr(windows_tray, "WindowsTray", FailedTray)
    monkeypatch.setattr(launcher.time, "sleep", lambda _sec: None)
    assert launcher._start_supervised_tray(service, threading.Event()) is None
    assert len(attempts) == launcher.TRAY_RECOVERY_ATTEMPTS


def test_supervisor_pipe_eof_gracefully_closes_orphan_service(monkeypatch):
    control = RestartControl()
    server = types.SimpleNamespace(should_exit=False)
    control.register_server(server)
    monkeypatch.setattr(tray_shutdown, "restart_control", control)
    monkeypatch.setenv(tray_shutdown.TRAY_CONTROL_ENV, "1")
    monkeypatch.setattr(tray_shutdown, "_control_lines", lambda: iter(()))
    import web.auto_scheduler as scheduler_module
    import web.job_runner as job_module
    events: list[str] = []
    monkeypatch.setattr(scheduler_module.auto_scheduler, "stop", lambda **_kw: events.append("stop scheduler"))
    monkeypatch.setattr(scheduler_module.auto_scheduler, "wait_until_stopped", lambda **_kw: True)
    monkeypatch.setattr(job_module.runner, "cancel", lambda: events.append("cancel job"))
    running = iter([True, False])
    monkeypatch.setattr(job_module.runner, "is_running", lambda: next(running))
    done = threading.Event()
    original = tray_shutdown.shutdown_current_service
    def shutdown():
        try:
            return original()
        finally:
            done.set()
    monkeypatch.setattr(tray_shutdown, "shutdown_current_service", shutdown)

    tray_shutdown.start_shutdown_listener()
    assert done.wait(3)
    assert events == ["stop scheduler", "cancel job"] and server.should_exit
    with pytest.raises(RestartPendingError):
        with control.new_work_guard():
            pass


def test_tray_exit_affects_only_its_service_pipe():
    a, b = Service(), Service()
    a_requested, b_requested = threading.Event(), threading.Event()
    assert launcher._request_tray_exit(a, a_requested)
    assert a.stdin.getvalue() == b"shutdown\n"
    assert b.stdin.getvalue() == b"" and not b_requested.is_set()
