"""Binggo 启动器：启动本地控制台并打开浏览器。"""

from __future__ import annotations

import os
import hashlib
import subprocess
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from src.restart_control import RESTART_EXIT_CODE, RESTART_SUPERVISED_ENV

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MUTEX_NAME = "Global\\BilibiliBinggoDashboard"
SOURCE_MUTEX_NAME = "Global\\BilibiliBinggoDashboard.Source.8787"
SERVE_FLAG = "--serve"
STARTUP_TIMEOUT_SEC = 45.0
SHUTDOWN_TIMEOUT_SEC = 15.0
CREATE_NO_WINDOW = 0x08000000
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8181 if bool(getattr(sys, "frozen", False)) else 8787
DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
RUNTIME_PROFILE_ID = ""
INSTANCE_ID = ""
OPEN_BROWSER = True
_SERVER_PROC = None
_RUNTIME_LEASE = None

# 非 Windows 文件锁句柄，进程存活期间保持打开
_LOCK_FH = None
# Windows mutex 也必须由 supervisor 父进程持有到退出；仅保存返回值，
# 不把它交给服务子进程或 Profile 运行时。
_WINDOWS_MUTEX_HANDLE = None


def _show_error(message: str) -> None:
    # PyInstaller windowed 模式下 stdout/stderr 可能为 None；错误框仍须可用。
    if sys.stderr is not None:
        print(message, file=sys.stderr)
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                message,
                "Binggo 启动失败",
                0x10,
            )
        except Exception:
            pass
        return
    if sys.platform == "darwin":
        # 经 argv 传文案，避免路径/引号破坏 AppleScript
        try:
            script = (
                "on run argv\n"
                '  display dialog (item 1 of argv) with title "Binggo 启动失败" '
                'buttons {"好"} default button 1 with icon stop\n'
                "end run"
            )
            subprocess.run(
                ["osascript", "-e", script, message[:1200]],
                check=False,
                capture_output=True,
                timeout=30,
            )
        except Exception:
            pass


def _port_open(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.4)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def _port_available(host: str, port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _windows_mutex_name() -> str:
    """The same data root and Profile share one Windows mutex in all run modes."""
    if RUNTIME_PROFILE_ID:
        from src.data_paths import get_data_root

        root_key = os.path.normcase(str(get_data_root().resolve(strict=False))).encode("utf-8")
        digest = hashlib.sha256(root_key).hexdigest()[:20]
        return f"Global\\BilibiliBinggoDashboard.{digest}.{RUNTIME_PROFILE_ID}"
    return MUTEX_NAME if bool(getattr(sys, "frozen", False)) else SOURCE_MUTEX_NAME


def _open_existing_runtime() -> None:
    if not RUNTIME_PROFILE_ID:
        webbrowser.open(DASHBOARD_URL)
        return
    from src.runtime_instances import get_runtime_instance

    existing = get_runtime_instance(RUNTIME_PROFILE_ID)
    if existing and existing["ready"]:
        webbrowser.open(f"http://127.0.0.1:{existing['port']}/", new=1)


def _acquire_windows_mutex() -> bool:
    global _WINDOWS_MUTEX_HANDLE
    if sys.platform != "win32":
        return True
    if _WINDOWS_MUTEX_HANDLE is not None:
        return True
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, _windows_mutex_name())
        error_code = ctypes.get_last_error()
        if not handle:
            # ERROR_ACCESS_DENIED 通常表示同名 Global mutex 已由更高权限
            # 进程持有；与 ERROR_ALREADY_EXISTS 一样按已有实例处理。
            if error_code in {5, 183}:
                _open_existing_runtime()
                return False
            return True
        if error_code == 183:
            close_handle(handle)
            _open_existing_runtime()
            return False
        _WINDOWS_MUTEX_HANDLE = handle
        return True
    except Exception:
        return True


def _acquire_file_lock() -> bool:
    """非 Windows：fcntl 文件锁，避免双开竞态。失败则打开浏览器并退出。"""
    global _LOCK_FH
    if RUNTIME_PROFILE_ID:
        from src.runtime_instances import RuntimeBusyError, acquire_runtime_lock

        global _RUNTIME_LEASE
        try:
            _RUNTIME_LEASE = acquire_runtime_lock(RUNTIME_PROFILE_ID)
            return True
        except RuntimeBusyError:
            _open_existing_runtime()
            return False
    if sys.platform == "win32":
        return True
    try:
        import errno
        import fcntl

        from src.app_paths import data_dir, ensure_user_dirs

        ensure_user_dirs()
        lock_path = data_dir() / ".instance.lock"
        fh = open(lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            fh.close()
            # 仅「已被占用」视为单实例冲突；其它锁错误不阻断启动
            busy = isinstance(exc, BlockingIOError) or getattr(exc, "errno", None) in {
                errno.EAGAIN,
                errno.EWOULDBLOCK,
                errno.EACCES,
            }
            if busy:
                webbrowser.open(DASHBOARD_URL)
                return False
            return True
        _LOCK_FH = fh
        return True
    except Exception:
        return True


def _acquire_single_instance() -> bool:
    if not _acquire_windows_mutex():
        return False
    if not _acquire_file_lock():
        _release_windows_mutex()
        return False
    return True


def _release_windows_mutex() -> None:
    global _WINDOWS_MUTEX_HANDLE
    if _WINDOWS_MUTEX_HANDLE is not None and sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
            close_handle.argtypes = (wintypes.HANDLE,)
            close_handle.restype = wintypes.BOOL
            close_handle(_WINDOWS_MUTEX_HANDLE)
        finally:
            _WINDOWS_MUTEX_HANDLE = None


def _release_single_instance() -> None:
    global _RUNTIME_LEASE, _LOCK_FH
    if _RUNTIME_LEASE is not None:
        _RUNTIME_LEASE.release()
        _RUNTIME_LEASE = None
    if _LOCK_FH is not None:
        try:
            _LOCK_FH.close()
        finally:
            _LOCK_FH = None
    _release_windows_mutex()


def _wait_for_server(timeout_sec: float = STARTUP_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if INSTANCE_ID and RUNTIME_PROFILE_ID:
            from src.runtime_instances import runtime_ready

            if runtime_ready(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID, port=DASHBOARD_PORT):
                return True
        elif _port_open(DASHBOARD_HOST, DASHBOARD_PORT):
            return True
        if _SERVER_PROC is not None and _SERVER_PROC.poll() is not None:
            return False
        time.sleep(0.25)
    return False


def _wait_for_server_stop(timeout_sec: float = SHUTDOWN_TIMEOUT_SEC) -> bool:
    """等待旧服务完全释放端口，再启动目标 Profile 的新服务。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not _port_open(DASHBOARD_HOST, DASHBOARD_PORT):
            return True
        time.sleep(0.1)
    return not _port_open(DASHBOARD_HOST, DASHBOARD_PORT)


def _server_process_args() -> list[str]:
    if bool(getattr(sys, "frozen", False)):
        return [sys.executable, SERVE_FLAG]
    # 源码模式不能执行 ``python --serve``；继续由原 run_dashboard.py
    # 作为服务子进程入口，并通过隐藏参数与 supervisor 父进程区分。
    return [sys.executable, str(ROOT / "scripts" / "run_dashboard.py"), SERVE_FLAG]


def _spawn_server_process() -> subprocess.Popen[bytes]:
    from src.app_paths import bundle_root

    env = os.environ.copy()
    env[RESTART_SUPERVISED_ENV] = "1"
    if RUNTIME_PROFILE_ID:
        from src.runtime_instances import RUNTIME_INSTANCE_ENV, RUNTIME_PORT_ENV, RUNTIME_PROFILE_ENV

        env[RUNTIME_PROFILE_ENV] = RUNTIME_PROFILE_ID
        env[RUNTIME_PORT_ENV] = str(DASHBOARD_PORT)
        env[RUNTIME_INSTANCE_ENV] = INSTANCE_ID
    kwargs: dict = {"cwd": str(bundle_root()), "env": env}
    if sys.platform == "win32" and (bool(getattr(sys, "frozen", False)) or not OPEN_BROWSER):
        kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.Popen(_server_process_args(), **kwargs)


def _stop_process(proc: subprocess.Popen[bytes]) -> int | None:
    exit_code = proc.poll()
    if exit_code is not None:
        return exit_code
    try:
        proc.terminate()
        return proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            return proc.wait(timeout=2)
        except Exception:
            return proc.poll()


def _finish_process_after_interrupt(proc: subprocess.Popen[bytes]) -> int | None:
    """Ctrl+C 后先等待同控制台子进程自行优雅退出，超时才强制兜底。"""

    exit_code = proc.poll()
    if exit_code is not None:
        return exit_code
    try:
        return proc.wait(timeout=SHUTDOWN_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return _stop_process(proc)
    except KeyboardInterrupt:
        # 用户再次按 Ctrl+C 时不再继续等待。
        return _stop_process(proc)


def _startup_failure_message(*, log_path: Path, data_root: Path, exit_code: int | None) -> str:
    code_text = str(exit_code) if exit_code is not None else "仍在运行"
    return (
        "控制台服务启动超时。\n\n"
        f"请查看日志：{log_path}\n"
        f"数据目录：{data_root}\n"
        f"服务进程退出码：{code_text}\n\n"
        f"可尝试关闭占用 {DASHBOARD_PORT} 端口的程序后重试。\n"
        "若控制台曾能打开，可在概览导出诊断包。"
    )


def _supervise_server(*, log_path: Path, data_root: Path) -> int:
    """监督服务子进程；仅专用退出码会触发下一轮启动。"""
    global _SERVER_PROC, RUNTIME_PROFILE_ID, INSTANCE_ID
    browser_opened = not OPEN_BROWSER
    while True:
        proc = _spawn_server_process()
        _SERVER_PROC = proc
        try:
            server_ready = _wait_for_server()
        except KeyboardInterrupt:
            _finish_process_after_interrupt(proc)
            return 0
        if not server_ready:
            exit_code = _stop_process(proc)
            _show_error(
                _startup_failure_message(
                    log_path=log_path,
                    data_root=data_root,
                    exit_code=exit_code,
                )
            )
            return 3 if RUNTIME_PROFILE_ID and _port_open(DASHBOARD_HOST, DASHBOARD_PORT) else 1

        if RUNTIME_PROFILE_ID:
            from src.runtime_instances import mark_runtime_ready

            try:
                mark_runtime_ready(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID)
            except RuntimeError:
                _stop_process(proc)
                raise

        if not browser_opened:
            webbrowser.open(DASHBOARD_URL)
            browser_opened = True

        try:
            # 父 supervisor 必须明确等待当前 --serve 子进程结束；只有
            # Ctrl+C 会中断这次等待，不能在浏览器打开后自行返回。
            exit_code = proc.wait()
        except KeyboardInterrupt:
            _finish_process_after_interrupt(proc)
            return 0

        if exit_code != RESTART_EXIT_CODE:
            # 普通退出或异常崩溃都不自动拉起，避免形成崩溃循环。
            return int(exit_code) if exit_code is not None else 1
        if not _wait_for_server_stop():
            _show_error(
                f"Binggo 已安全退出，但端口 {DASHBOARD_PORT} 未及时释放，无法自动重新启动。"
            )
            return 1
        if RUNTIME_PROFILE_ID:
            from src.data_paths import get_selected_profile_id
            from src.runtime_instances import clear_runtime, claim_runtime

            target = get_selected_profile_id()
            if target != RUNTIME_PROFILE_ID:
                clear_runtime(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID)
                _release_single_instance()
                RUNTIME_PROFILE_ID = target
                INSTANCE_ID = uuid.uuid4().hex
                if not _acquire_single_instance():
                    _show_error("目标账号已经在其它窗口运行，请打开该窗口。")
                    return 1
                claim_runtime(target, instance_id=INSTANCE_ID, port=DASHBOARD_PORT, pid=os.getpid())
        # 专用退出码才表示 Profile 切换；父进程继续持有单实例锁，
        # 下一轮仅重启服务子进程，不重复打开浏览器。


def run_server_mode() -> int:
    from src.data_paths import LEGACY_DATA_ROOT_ENV, ensure_data_root_selected, get_runtime_profile_id

    try:
        if bool(getattr(sys, "frozen", False)):
            appdata = os.environ.get("APPDATA", "").strip()
            if appdata:
                os.environ.setdefault(LEGACY_DATA_ROOT_ENV, str(Path(appdata) / "Binggo"))
        else:
            os.environ.setdefault(LEGACY_DATA_ROOT_ENV, str(ROOT))
        ensure_data_root_selected()
        from src.runtime_instances import acquire_service_lock

        service_lease = acquire_service_lock(get_runtime_profile_id())
    except (OSError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1

    try:
        from src.app_logging import get_logger, setup_logging
        from src.app_paths import ensure_user_dirs
        from src.dashboard_server import get_dashboard_url, run_dashboard_server

        ensure_user_dirs()
        setup_logging(console=False)
        logger = get_logger("launcher")
        logger.info("Binggo 服务进程启动，监听 %s", get_dashboard_url())
        restart_requested = run_dashboard_server()
        return RESTART_EXIT_CODE if restart_requested else 0
    except (OSError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1
    except Exception:
        try:
            get_logger("launcher").exception("Binggo 服务进程异常退出")
        except Exception:
            pass
        return 1
    finally:
        service_lease.release()


def main() -> int:
    global RUNTIME_PROFILE_ID, INSTANCE_ID, DASHBOARD_PORT, DASHBOARD_URL, OPEN_BROWSER
    import argparse
    from src.data_paths import LEGACY_DATA_ROOT_ENV, ensure_data_root_selected

    parser = argparse.ArgumentParser(description="Binggo local runtime")
    parser.add_argument("--profile")
    parser.add_argument("--port", type=int)
    parser.add_argument("--instance-id")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args([value for value in sys.argv[1:] if value != SERVE_FLAG])

    try:
        if bool(getattr(sys, "frozen", False)):
            appdata = os.environ.get("APPDATA", "").strip()
            if appdata:
                os.environ.setdefault(LEGACY_DATA_ROOT_ENV, str(Path(appdata) / "Binggo"))
        else:
            os.environ.setdefault(LEGACY_DATA_ROOT_ENV, str(ROOT))
        ensure_data_root_selected()
    except (OSError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1

    from src.data_paths import get_runtime_profile_id, initialize_data_layout, validate_profile_id
    from src.runtime_instances import (
        RUNTIME_PRIMARY_ENV, RUNTIME_PROFILE_ENV, RuntimeBusyError, claim_runtime, clear_runtime, get_runtime_instance,
        reserve_runtime,
    )

    try:
        RUNTIME_PROFILE_ID = validate_profile_id(args.profile) if args.profile else get_runtime_profile_id()
        INSTANCE_ID = args.instance_id or uuid.uuid4().hex
        if args.instance_id and (len(args.instance_id) != 32 or not all(c in "0123456789abcdef" for c in args.instance_id)):
            raise ValueError("instance_id 无效")
        if args.port is not None and not 1024 <= args.port <= 65535:
            raise ValueError("运行端口无效")
        os.environ[RUNTIME_PROFILE_ENV] = RUNTIME_PROFILE_ID
        OPEN_BROWSER = not args.no_browser
        os.environ[RUNTIME_PRIMARY_ENV] = "0" if args.profile else "1"
        if not _acquire_single_instance():
            return 0
        try:
            initialize_data_layout()
        except BaseException:
            _release_single_instance()
            raise
    except (OSError, ValueError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1

    try:
        excluded_ports: set[int] = set()
        preferred_port = args.port or DASHBOARD_PORT
        while True:
            try:
                if args.port is None:
                    DASHBOARD_PORT = reserve_runtime(
                        RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID,
                        preferred_port=preferred_port, owner_pid=os.getpid(),
                        excluded_ports=excluded_ports,
                    )
                else:
                    DASHBOARD_PORT = args.port
                DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"
                if not _port_available(DASHBOARD_HOST, DASHBOARD_PORT):
                    if args.port is not None:
                        raise RuntimeBusyError(f"端口 {DASHBOARD_PORT} 已被占用")
                    clear_runtime(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID)
                    excluded_ports.add(DASHBOARD_PORT)
                    continue
                claim_runtime(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID, port=DASHBOARD_PORT, pid=os.getpid())
                break
            except RuntimeBusyError as exc:
                existing = get_runtime_instance(RUNTIME_PROFILE_ID)
                if existing and existing["ready"]:
                    webbrowser.open(f"http://127.0.0.1:{existing['port']}/", new=1)
                    return 0
                _show_error(str(exc))
                return 1

        from src.app_logging import log_file
        from src.app_paths import ensure_user_dirs, runtime_label, user_home

        ensure_user_dirs()
        home = user_home()
        log_path = log_file()
        print(f"Binggo 运行模式: {runtime_label()}")
        print(f"数据目录: {home}")
        print(f"日志文件: {log_path}")
        print(f"控制台: {DASHBOARD_URL}")
        return _supervise_server(log_path=Path(log_path), data_root=home)
    except (OSError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1
    finally:
        if RUNTIME_PROFILE_ID and INSTANCE_ID:
            clear_runtime(RUNTIME_PROFILE_ID, instance_id=INSTANCE_ID)
        _release_single_instance()


if __name__ == "__main__":
    if SERVE_FLAG in sys.argv:
        raise SystemExit(run_server_mode())
    raise SystemExit(main())
