"""本地控制台 Web 服务配置与启动。"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Callable

from src.app_paths import is_frozen
from src.restart_control import restart_control

DASHBOARD_HOST = "127.0.0.1"
DEV_DASHBOARD_PORT = 8787
PACKAGED_DASHBOARD_PORT = 8181
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def get_dashboard_port() -> int:
    """源码/开发模式用 8787，Windows 安装包用 8181。"""
    explicit = os.environ.get("BINGGO_RUNTIME_PORT", "").strip()
    if explicit:
        try:
            port = int(explicit)
        except ValueError as exc:
            raise RuntimeError("显式控制台端口无效") from exc
        if not 1024 <= port <= 65535:
            raise RuntimeError("显式控制台端口超出允许范围")
        return port
    return PACKAGED_DASHBOARD_PORT if is_frozen() else DEV_DASHBOARD_PORT


def get_dashboard_url() -> str:
    return f"http://{DASHBOARD_HOST}:{get_dashboard_port()}"


def assert_loopback_host(host: str = DASHBOARD_HOST) -> None:
    if host not in _LOOPBACK_HOSTS:
        raise RuntimeError(
            f"拒绝绑定非 loopback 地址: {host!r}（仅允许 {sorted(_LOOPBACK_HOSTS)}）"
        )


# 模块导入时按当前运行形态解析（开发进程与打包进程各自固定）
DASHBOARD_PORT = get_dashboard_port()
DASHBOARD_URL = get_dashboard_url()


def _ensure_stdio() -> None:
    """无控制台子进程（如 Windows CREATE_NO_WINDOW）下 stdout/stderr 可能为 None。"""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def run_dashboard_server(*, log_level: str = "info", on_registered: Callable[[], None] | None = None) -> bool:
    """Run Uvicorn and report whether the supervisor should restart it.

    ``restart_control.request_restart`` flips ``Server.should_exit`` only after
    FastAPI has sent the successful switch response.  Uvicorn then performs its
    normal graceful shutdown before this function returns ``True``.
    """

    import uvicorn

    assert_loopback_host(DASHBOARD_HOST)
    _ensure_stdio()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    config = uvicorn.Config(
        "web.app:app",
        host=DASHBOARD_HOST,
        port=get_dashboard_port(),
        reload=False,
        log_level=log_level,
    )
    server = uvicorn.Server(config)
    restart_control.register_server(server)
    try:
        if on_registered is not None:
            on_registered()
        server.run()
    finally:
        restart_requested = restart_control.finish_server(server)
    return restart_requested
