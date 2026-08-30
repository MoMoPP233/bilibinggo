"""Binggo 启动器：启动本地控制台并打开浏览器。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

from src.restart_control import RESTART_EXIT_CODE, RESTART_SUPERVISED_ENV

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MUTEX_NAME = "Global\\BilibiliBinggoDashboard"
SERVE_FLAG = "--serve"
STARTUP_TIMEOUT_SEC = 45.0
SHUTDOWN_TIMEOUT_SEC = 15.0
CREATE_NO_WINDOW = 0x08000000
DASHBOARD_HOST = "127.0.0.1"
DASHBOARD_PORT = 8181 if bool(getattr(sys, "frozen", False)) else 8787
DASHBOARD_URL = f"http://{DASHBOARD_HOST}:{DASHBOARD_PORT}"

# 非 Windows 文件锁句柄，进程存活期间保持打开
_LOCK_FH = None


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
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _acquire_windows_mutex() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateMutexW(None, False, MUTEX_NAME)
        already_exists = kernel32.GetLastError() == 183
        if already_exists:
            webbrowser.open(DASHBOARD_URL)
            return False
        return True
    except Exception:
        return True


def _acquire_file_lock() -> bool:
    """非 Windows：fcntl 文件锁，避免双开竞态。失败则打开浏览器并退出。"""
    global _LOCK_FH
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
    return _acquire_file_lock()


def _wait_for_server(timeout_sec: float = STARTUP_TIMEOUT_SEC) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if _port_open(DASHBOARD_HOST, DASHBOARD_PORT):
            return True
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
    kwargs: dict = {"cwd": str(bundle_root()), "env": env}
    if sys.platform == "win32" and bool(getattr(sys, "frozen", False)):
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
    browser_opened = False
    while True:
        proc = _spawn_server_process()
        if not _wait_for_server():
            exit_code = _stop_process(proc)
            _show_error(
                _startup_failure_message(
                    log_path=log_path,
                    data_root=data_root,
                    exit_code=exit_code,
                )
            )
            return 1

        if not browser_opened:
            webbrowser.open(DASHBOARD_URL)
            browser_opened = True

        try:
            while proc.poll() is None:
                time.sleep(0.5)
        except KeyboardInterrupt:
            _stop_process(proc)
            return 0

        exit_code = proc.poll()
        if exit_code != RESTART_EXIT_CODE:
            # 普通退出或异常崩溃都不自动拉起，避免形成崩溃循环。
            return int(exit_code) if exit_code is not None else 1
        if not _wait_for_server_stop():
            _show_error(
                f"Binggo 已安全退出，但端口 {DASHBOARD_PORT} 未及时释放，无法自动重新启动。"
            )
            return 1
        # 专用退出码才表示 Profile 切换；父进程继续持有单实例锁，
        # 下一轮仅重启服务子进程，不重复打开浏览器。


def run_server_mode() -> int:
    from src.data_paths import LEGACY_DATA_ROOT_ENV, ensure_data_root_selected

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

    try:
        from src.app_logging import get_logger, setup_logging
        from src.app_paths import ensure_user_dirs
        from src.dashboard_server import run_dashboard_server

        ensure_user_dirs()
        setup_logging(console=False)
        logger = get_logger("launcher")
        logger.info("Binggo 服务进程启动，监听 %s", DASHBOARD_URL)
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


def main() -> int:
    # Windows mutex 必须早于首次目录选择与任何 Profile 初始化，避免双开
    # 分别选择 A/B 后互相覆盖 locator。非 Windows 文件锁也沿用同一入口。
    if not _acquire_single_instance():
        return 0

    from src.data_paths import LEGACY_DATA_ROOT_ENV, ensure_data_root_selected

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

    if not _port_available(DASHBOARD_HOST, DASHBOARD_PORT):
        webbrowser.open(DASHBOARD_URL)
        _show_error(
            f"控制台已在运行中。\n\n若页面打不开，请关闭占用 {DASHBOARD_PORT} 端口的程序后重试。\n\n{DASHBOARD_URL}"
        )
        return 0

    try:
        from src.app_logging import log_file
        from src.app_paths import ensure_user_dirs, runtime_label, user_home

        ensure_user_dirs()
    except (OSError, RuntimeError) as exc:
        _show_error(str(exc))
        return 1

    home = user_home()
    # supervisor 会跨 Profile 存活，不能长期持有初始 Profile 的日志句柄；
    # 真正的服务子进程会在每轮启动时为其固定 runtime_profile 配置日志。
    log_path = log_file()
    print(f"Binggo 运行模式: {runtime_label()}")
    print(f"数据目录: {home}")
    print(f"日志文件: {log_path}")
    print(f"控制台: {DASHBOARD_URL}")

    return _supervise_server(log_path=Path(log_path), data_root=home)


if __name__ == "__main__":
    if SERVE_FLAG in sys.argv:
        raise SystemExit(run_server_mode())
    raise SystemExit(main())
