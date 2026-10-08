"""Profile-local runtime ownership and the small localhost instance directory.

The OS lock, not a PID or a JSON file, is the authority for a running runtime.
Registry entries are only discovery hints and are checked against that lock and
the loopback ready identity before their URL is offered to a browser.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from src.data_paths import get_data_root, get_profile_dir, validate_profile_id
from src.profile_writer_lease import _ProfileWriterLease

RUNTIME_PROFILE_ENV = "BINGGO_RUNTIME_PROFILE_ID"
RUNTIME_PORT_ENV = "BINGGO_RUNTIME_PORT"
RUNTIME_INSTANCE_ENV = "BINGGO_RUNTIME_INSTANCE_ID"
RUNTIME_PRIMARY_ENV = "BINGGO_RUNTIME_PRIMARY"
STARTING_TTL_SECONDS = 60
MAX_ADDITIONAL_PORTS = 20


class RuntimeBusyError(RuntimeError):
    pass


def runtime_lock_path(profile_id: str) -> Path:
    value = validate_profile_id(profile_id)
    return get_data_root() / "shared" / "runtime-locks" / f"{value}.lock"


def service_lock_path(profile_id: str) -> Path:
    value = validate_profile_id(profile_id)
    return get_data_root() / "shared" / "runtime-locks" / f"{value}.service.lock"


def service_lock_held(profile_id: str) -> bool:
    lease = _ProfileWriterLease(service_lock_path(profile_id), owner="service:probe")
    if not lease.acquire():
        return True
    lease.release()
    return False


def acquire_service_lock(profile_id: str) -> _ProfileWriterLease:
    lease = _ProfileWriterLease(service_lock_path(profile_id), owner="service")
    if not lease.acquire():
        raise RuntimeBusyError("该账号的服务进程仍在运行，已阻止重复启动。")
    return lease


def acquire_runtime_lock(profile_id: str) -> _ProfileWriterLease:
    lease = _ProfileWriterLease(runtime_lock_path(profile_id), owner="runtime")
    if not lease.acquire():
        raise RuntimeBusyError("账号正在运行，请打开已有窗口。")
    if service_lock_held(profile_id):
        lease.release()
        raise RuntimeBusyError("该账号的服务进程仍在运行，请先退出该账号。")
    return lease


def runtime_lock_held(profile_id: str) -> bool:
    """Probe the OS lock; a leftover lock file is not evidence of a live runtime."""
    lease = _ProfileWriterLease(runtime_lock_path(profile_id), owner="runtime:probe")
    if not lease.acquire():
        return True
    lease.release()
    return False


def _registry_path() -> Path:
    return get_data_root() / "shared" / "runtime-registry.json"


def _registry_lock() -> _ProfileWriterLease:
    lease = _ProfileWriterLease(get_data_root() / "shared" / "runtime-registry.lock")
    deadline = time.monotonic() + 3.0
    while not lease.acquire():
        if time.monotonic() >= deadline:
            raise RuntimeBusyError("运行实例登记表繁忙，请稍后重试。")
        time.sleep(0.025)
    return lease


def _read_registry() -> dict[str, dict[str, Any]]:
    try:
        raw = json.loads(_registry_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeBusyError("运行实例登记表损坏，已停止启动以避免重复实例。") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("instances", {}), dict):
        raise RuntimeBusyError("运行实例登记表格式无效。")
    return {key: value for key, value in raw.get("instances", {}).items() if isinstance(value, dict)}


def _write_registry(instances: dict[str, dict[str, Any]]) -> None:
    path = _registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".runtime-registry-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"instances": instances}, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _ready_identity(entry: dict[str, Any], *, timeout: float = 0.35) -> bool:
    try:
        port = int(entry["port"])
        if not 1 <= port <= 65535:
            return False
        request = urllib.request.Request(f"http://127.0.0.1:{port}/api/runtime/ready")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=timeout) as response:
            data = json.load(response)
        return (
            data.get("ready") is True
            and data.get("profile_id") == entry.get("profile_id")
            and data.get("instance_id") == entry.get("instance_id")
            and data.get("port") == port
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError, UnicodeError, urllib.error.URLError):
        return False


def _entry_live(entry: dict[str, Any]) -> bool:
    profile_id = str(entry.get("profile_id") or "")
    try:
        validate_profile_id(profile_id)
    except ValueError:
        return False
    try:
        owner_alive = _pid_alive(int(entry.get("pid") or 0))
    except ValueError:
        owner_alive = False
    if not owner_alive:
        if entry.get("status") == "ready" and service_lock_held(profile_id):
            return _ready_identity(entry)
        return False
    if entry.get("status") == "ready":
        # The common supervisor-owned path needs no network here. A service
        # orphan is rare and must prove its identity before retaining its entry.
        if runtime_lock_held(profile_id):
            return True
        return service_lock_held(profile_id) and _ready_identity(entry)
    # The API reserves before the new supervisor acquires its OS lock. This
    # bounded interval prevents concurrent clicks from spawning two parents.
    return (
        entry.get("status") == "starting"
        and time.time() - float(entry.get("started_at") or 0) < STARTING_TTL_SECONDS
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel.GetExitCodeProcess
        get_exit_code.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = open_process(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # Access denied is ambiguous; retaining a recent reservation is safer.
            return ctypes.get_last_error() == 5
        try:
            exit_code = wintypes.DWORD()
            return bool(get_exit_code(handle, ctypes.byref(exit_code))) and exit_code.value == 259
        finally:
            close_handle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except ProcessLookupError:
        return False


def _prune(instances: dict[str, dict[str, Any]]) -> bool:
    changed = False
    for profile_id, entry in list(instances.items()):
        if not _entry_live(entry):
            del instances[profile_id]
            changed = True
    return changed


def _drop_unverified_target(instances: dict[str, dict[str, Any]], profile_id: str) -> None:
    entry = instances.get(profile_id)
    if entry and entry.get("status") == "ready" and not _ready_identity(entry):
        # A retained OS lock might belong to the new launcher, not this stale
        # JSON record. Its random instance ID must agree with the ready API.
        del instances[profile_id]


def list_runtime_instances() -> dict[str, dict[str, Any]]:
    lease = _registry_lock()
    try:
        instances = _read_registry()
        if _prune(instances):
            _write_registry(instances)
    finally:
        lease.release()
    result: dict[str, dict[str, Any]] = {}
    for profile_id, entry in instances.items():
        ready = (runtime_lock_held(profile_id) or service_lock_held(profile_id)) and _ready_identity(entry)
        result[profile_id] = {
            "profile_id": profile_id,
            "port": int(entry["port"]),
            "instance_id": str(entry["instance_id"]),
            "started_at": float(entry["started_at"]),
            "ready": ready,
            "running": True,
        }
    return result


def get_runtime_instance(profile_id: str) -> dict[str, Any] | None:
    return list_runtime_instances().get(validate_profile_id(profile_id))


def reserve_runtime(
    profile_id: str, *, instance_id: str, preferred_port: int, owner_pid: int,
    excluded_ports: set[int] | None = None,
) -> int:
    value = validate_profile_id(profile_id)
    if not get_profile_dir(value).is_dir():
        raise ValueError(f"Profile 不存在：{value}")
    excluded = excluded_ports or set()
    lease = _registry_lock()
    try:
        instances = _read_registry()
        _prune(instances)
        _drop_unverified_target(instances, value)
        if value in instances:
            raise RuntimeBusyError("账号正在运行，请打开已有窗口。")
        used = {int(entry.get("port") or 0) for entry in instances.values()}
        port = next(
            (candidate for candidate in range(preferred_port, preferred_port + MAX_ADDITIONAL_PORTS)
             if candidate not in used and candidate not in excluded),
            None,
        )
        if port is None:
            raise RuntimeBusyError("没有可用的本地 Binggo 端口。")
        instances[value] = {
            "profile_id": value, "port": port, "pid": owner_pid,
            "instance_id": instance_id, "started_at": time.time(), "status": "starting",
        }
        _write_registry(instances)
        return port
    finally:
        lease.release()


def claim_runtime(profile_id: str, *, instance_id: str, port: int, pid: int) -> None:
    value = validate_profile_id(profile_id)
    lease = _registry_lock()
    try:
        instances = _read_registry()
        entry = instances.get(value)
        if entry and entry.get("instance_id") != instance_id:
            _drop_unverified_target(instances, value)
            entry = instances.get(value)
        if entry and (entry.get("instance_id") != instance_id or int(entry.get("port") or 0) != port):
            if _entry_live(entry):
                raise RuntimeBusyError("目标 Profile 已有其它运行实例。")
        instances[value] = {
            "profile_id": value, "port": port, "pid": pid,
            "instance_id": instance_id, "started_at": time.time(), "status": "starting",
        }
        _write_registry(instances)
    finally:
        lease.release()


def mark_runtime_ready(profile_id: str, *, instance_id: str) -> None:
    lease = _registry_lock()
    try:
        instances = _read_registry()
        entry = instances.get(validate_profile_id(profile_id))
        if not entry or entry.get("instance_id") != instance_id:
            raise RuntimeBusyError("实例登记身份不匹配。")
        entry["status"] = "ready"
        _write_registry(instances)
    finally:
        lease.release()


def clear_runtime(profile_id: str, *, instance_id: str) -> None:
    lease = _registry_lock()
    try:
        instances = _read_registry()
        value = validate_profile_id(profile_id)
        if instances.get(value, {}).get("instance_id") == instance_id:
            del instances[value]
            _write_registry(instances)
    finally:
        lease.release()


def runtime_ready(profile_id: str, *, instance_id: str, port: int) -> bool:
    return _ready_identity({"profile_id": profile_id, "instance_id": instance_id, "port": port})
