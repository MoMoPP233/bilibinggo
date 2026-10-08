"""Supervisor-to-service graceful exit channel; no HTTP shutdown endpoint."""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Iterator

from src.restart_control import restart_control

TRAY_CONTROL_ENV = "BINGGO_TRAY_CONTROL"
_logger = logging.getLogger(__name__)


def _control_lines() -> Iterator[str]:
    if sys.stdin is not None:
        yield from sys.stdin
        return
    # PyInstaller windowed children can set sys.stdin to None even when their
    # supervisor supplied a real inherited stdin pipe.
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel32.GetStdHandle.restype = ctypes.c_void_p
        handle = kernel32.GetStdHandle(0xFFFFFFF6)  # STD_INPUT_HANDLE
        if handle and handle != ctypes.c_void_p(-1).value:
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
            with os.fdopen(fd, "r", encoding="ascii") as stream:
                yield from stream


def shutdown_current_service() -> bool:
    """Stop new work, cancel cooperatively, and wait for in-flight work to settle."""
    if not restart_control.begin_shutdown():
        return False
    from web.auto_scheduler import auto_scheduler
    from web.job_runner import runner

    auto_scheduler.stop(reason="系统托盘请求退出")
    runner.cancel()
    # A forced kill here could lose a pending/unknown mutation result or a
    # checkpoint. Keep the service alive until both workers have really ended.
    while runner.is_running():
        threading.Event().wait(0.25)
    while not auto_scheduler.wait_until_stopped(timeout_sec=2.0):
        _logger.warning("托盘退出仍在等待自动调度器安全停止")
    return restart_control.request_shutdown()


def start_shutdown_listener() -> None:
    """Called after Uvicorn registration; only supervised children read stdin."""
    if os.environ.get(TRAY_CONTROL_ENV) != "1":
        return

    def listen() -> None:
        try:
            for command in _control_lines():
                if command.strip() == "shutdown":
                    shutdown_current_service()
                    return
        except Exception:
            _logger.exception("托盘退出通道异常；服务保持运行")

    threading.Thread(target=listen, name="binggo-tray-control", daemon=True).start()
