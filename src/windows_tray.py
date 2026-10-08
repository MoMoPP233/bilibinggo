"""One optional Windows notification icon per supervised Profile runtime."""

from __future__ import annotations

import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path

_logger = logging.getLogger(__name__)


def icon_path() -> Path:
    from src.app_paths import install_root

    return install_root() / "packaging" / "assets" / "app-icon.png"


def profile_title(profile_id: str) -> str:
    from src.profile_manager import get_profile_metadata

    try:
        nickname = get_profile_metadata(profile_id).get("nickname", "").strip()
    except (OSError, ValueError):
        nickname = ""
    return f"Binggo - {nickname} [{profile_id}]" if nickname else f"Binggo - {profile_id}"


class WindowsTray:
    """The tray message loop lives in its own daemon thread on Windows."""

    def __init__(self, profile_id: str, instance_id: str,
                 open_ui: Callable[[], None], request_exit: Callable[[], None]) -> None:
        self.profile_id = profile_id
        self.instance_id = instance_id
        self._open_ui = open_ui
        self._request_exit = request_exit
        self._icon = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._setup_done = threading.Event()

    def start(self) -> bool:
        if sys.platform != "win32":
            return False
        try:
            import pystray
            from PIL import Image

            with Image.open(icon_path()) as image:
                image = image.copy()
            menu = pystray.Menu(
                pystray.MenuItem("打开界面", lambda _icon, _item: self._open_ui(), default=True),
                pystray.MenuItem("退出 Binggo", lambda _icon, _item: self._request_exit()),
            )
            self._icon = pystray.Icon(
                f"binggo-{self.profile_id}-{self.instance_id}", image,
                profile_title(self.profile_id)[:127], menu,
            )
            self._thread = threading.Thread(
                target=self._run, name=f"binggo-tray-{self.profile_id}", daemon=True,
            )
            self._thread.start()
            self._setup_done.wait(3.0)
            if self._ready.is_set() and self._icon is not None and self._icon.visible:
                return True
            self.stop()
            _logger.warning("Windows 托盘未能及时就绪；Dashboard 继续运行")
            return False
        except Exception:
            _logger.exception("Windows 托盘初始化失败；Dashboard 继续运行")
            self.stop()
            return False

    def _run(self) -> None:
        icon = self._icon

        def setup(ready_icon) -> None:
            try:
                ready_icon.visible = True
                if not ready_icon.visible:
                    raise RuntimeError("Windows 托盘图标未显示")
                self._ready.set()
            except Exception:
                _logger.exception("Windows 托盘图标显示失败")
            finally:
                self._setup_done.set()

        try:
            icon.run(setup=setup)
        except Exception:
            _logger.exception("Windows 托盘消息循环异常；Dashboard 继续运行")
            self._setup_done.set()

    def stop(self) -> None:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                _logger.exception("移除 Windows 托盘图标失败")
        self._icon = None
