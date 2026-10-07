"""Controlled local launch of an existing Profile in a second supervisor."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from src.data_paths import DATA_ROOT_ENV, get_data_root, get_profile_dir, validate_profile_id
from src.runtime_instances import (
    MAX_ADDITIONAL_PORTS, RuntimeBusyError, clear_runtime, get_runtime_instance,
    reserve_runtime, runtime_ready,
)

ROOT = Path(__file__).resolve().parents[1]
CREATE_NO_WINDOW = 0x08000000
STARTUP_TIMEOUT_SEC = 50.0


def _default_port() -> int:
    return 8181 if bool(getattr(sys, "frozen", False)) else 8787


def _launch_args(profile_id: str, port: int, instance_id: str) -> list[str]:
    value = validate_profile_id(profile_id)
    if not 1024 <= port <= 65535 or len(instance_id) != 32 or not all(
        c in "0123456789abcdef" for c in instance_id
    ):
        raise ValueError("运行实例参数无效")
    entry = [sys.executable]
    if not bool(getattr(sys, "frozen", False)):
        entry.append(str(ROOT / "scripts" / "run_dashboard.py"))
    return entry + ["--profile", value, "--port", str(port), "--instance-id", instance_id, "--no-browser"]


def _spawn_runtime(profile_id: str, port: int, instance_id: str) -> subprocess.Popen[bytes]:
    from src.app_paths import bundle_root

    env = os.environ.copy()
    env[DATA_ROOT_ENV] = str(get_data_root())
    kwargs: dict[str, Any] = {"cwd": str(bundle_root()), "env": env, "stdin": subprocess.DEVNULL}
    if sys.platform == "win32":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(_launch_args(profile_id, port, instance_id), **kwargs)


def _wait_ready(proc: subprocess.Popen[bytes], profile_id: str, port: int, instance_id: str) -> bool:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if runtime_ready(profile_id, instance_id=instance_id, port=port):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def launch_existing_profile(profile_id: str) -> dict[str, Any]:
    """Only the profile ID crosses the HTTP boundary; every command part is ours."""
    value = validate_profile_id(profile_id)
    if not get_profile_dir(value).is_dir():
        raise ValueError(f"Profile 不存在：{value}")
    existing = get_runtime_instance(value)
    if existing:
        return {"already_running": True, **existing, "url": f"http://127.0.0.1:{existing['port']}/"}

    instance_id = uuid.uuid4().hex
    excluded: set[int] = set()
    for _ in range(MAX_ADDITIONAL_PORTS):
        port = reserve_runtime(
            value, instance_id=instance_id, preferred_port=_default_port(),
            owner_pid=os.getpid(), excluded_ports=excluded,
        )
        if not _port_available(port):
            clear_runtime(value, instance_id=instance_id)
            excluded.add(port)
            continue
        try:
            proc = _spawn_runtime(value, port, instance_id)
        except OSError:
            clear_runtime(value, instance_id=instance_id)
            raise
        if _wait_ready(proc, value, port, instance_id):
            url = f"http://127.0.0.1:{port}/"
            try:
                opened = bool(webbrowser.open(url, new=1))
            except (OSError, RuntimeError):
                opened = False
            return {
                "already_running": False, "profile_id": value, "port": port,
                "instance_id": instance_id, "ready": True, "running": True,
                "url": url, "browser_opened": opened,
            }
        # Do not clear a reservation while a supervisor could still own it.
        # The supervisor has its own bounded ready wait and will stop its child.
        if proc.poll() is None:
            raise RuntimeBusyError("目标实例未能通过身份核验；请稍后查看启动日志。")
        clear_runtime(value, instance_id=instance_id)
        if not _port_available(port):
            excluded.add(port)
            continue
        raise RuntimeError("目标实例启动失败，请查看该 Profile 的启动日志。")
    raise RuntimeBusyError("没有可用的本地 Binggo 端口。")


def _port_available(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
