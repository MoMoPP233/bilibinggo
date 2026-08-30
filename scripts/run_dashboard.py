"""启动本地控制面板。"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

def main() -> int:
    # 保持用户原有命令不变，但正常入口改由 launcher 父进程监督。
    # ``--serve`` 只供 launcher 启动源码服务子进程使用。
    if "--serve" not in sys.argv:
        from binggo_launcher import main as launcher_main

        return launcher_main()

    try:
        import uvicorn  # noqa: F401
    except ImportError:
        print("请先安装依赖: pip install -r requirements.txt", file=sys.stderr)
        return 1

    from src.data_paths import LEGACY_DATA_ROOT_ENV, ensure_data_root_selected

    try:
        os.environ.setdefault(LEGACY_DATA_ROOT_ENV, str(ROOT))
        ensure_data_root_selected()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    from src.app_logging import setup_logging
    from src.app_paths import ensure_user_dirs
    from src.dashboard_server import DASHBOARD_URL, run_dashboard_server

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    ensure_user_dirs()
    log_path = setup_logging()
    print(f"日志文件: {log_path}")
    print(f"控制台地址: {DASHBOARD_URL}")
    try:
        from src.config_health import log_config_health

        log_config_health(force=True)
    except Exception as exc:
        print(f"配置自检跳过: {exc}", file=sys.stderr)
    dist_index = ROOT / "web" / "static" / "dist" / "index.html"
    if not dist_index.exists():
        print(
            "未找到 web/static/dist。开发请另开: cd web/frontend && npm run dev\n"
            "生产请先: cd web/frontend && npm ci && npm run build",
            file=sys.stderr,
        )
    restart_requested = run_dashboard_server()
    if restart_requested:
        from src.restart_control import RESTART_EXIT_CODE

        return RESTART_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
