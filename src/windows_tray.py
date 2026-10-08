"""One optional Windows notification icon per supervised Profile runtime."""

from __future__ import annotations

import logging
import sys
import threading
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)
_TOOLTIP_MAX_UNITS = 127  # NOTIFYICONDATA.szTip includes one terminating UTF-16 unit.
_CN_TZ = timezone(timedelta(hours=8))


def _truncate_utf16(value: str, max_units: int = _TOOLTIP_MAX_UNITS) -> str:
    result: list[str] = []
    used = 0
    for char in value:
        units = len(char.encode("utf-16-le")) // 2
        if used + units > max_units:
            break
        result.append(char)
        used += units
    return "".join(result)


def _safe_piece(value: str) -> str:
    return " ".join("".join(char if char.isprintable() and char != "·" else " " for char in value).split())


def _hour_minute(value: object, *, unix: bool = False) -> str | None:
    try:
        if unix:
            when = datetime.fromtimestamp(value, _CN_TZ) if type(value) is int else None
        else:
            when = datetime.fromisoformat(value) if isinstance(value, str) else None
            if when is not None and when.tzinfo is None:
                when = when.replace(tzinfo=_CN_TZ)
        return when.astimezone(_CN_TZ).strftime("%H:%M") if when else None
    except (ValueError, OverflowError, OSError, TypeError):
        return None


def health_tooltip(profile_id: str, health: dict[str, Any] | None) -> str:
    """Keep account, state, next scan and risk within Windows' UTF-16 limit."""
    from src.profile_manager import get_profile_metadata

    try:
        nickname = get_profile_metadata(profile_id).get("nickname", "")
    except (OSError, ValueError):
        nickname = ""
    nickname = _truncate_utf16(_safe_piece(nickname or profile_id), 24)
    labels = {
        "normal": "正常挂机", "scanning": "正在扫描", "stopped": "调度已停止",
        "night_quiet": "夜间静默", "risk_paused": "风控暂停",
        "unavailable": "状态暂不可用",
    }
    view = health if isinstance(health, dict) else {}
    state = labels.get(view.get("status"), "状态暂不可用")
    risk = {"paused": "已暂停", "unpaused": "未暂停"}.get(view.get("risk"), "未知")
    next_scan = _hour_minute(view.get("next_scan_unix"), unix=True) or "未知"
    core = (
        f"Binggo·{nickname}[{profile_id}] · 状态:{state}"
        f" · 下次扫描:{next_scan} · 风控:{risk}"
    )
    last_scan = _hour_minute(view.get("last_scan_at"))
    optional = f" · 上次扫描:{last_scan}" if last_scan else ""
    if len((core + optional).encode("utf-16-le")) // 2 <= _TOOLTIP_MAX_UNITS:
        core += optional
    return _truncate_utf16(core)


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
                _truncate_utf16(profile_title(self.profile_id)), menu,
            )
            self._thread = threading.Thread(
                target=self._run, name=f"binggo-tray-{self.profile_id}", daemon=True,
            )
            self._thread.start()
            self._setup_done.wait(3.0)
            if self._ready.is_set() and self._icon is not None and self._icon.visible:
                return True
            self.stop()
            _logger.warning("Windows 托盘未能及时就绪")
            return False
        except Exception:
            _logger.exception("Windows 托盘初始化失败")
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
            _logger.exception("Windows 托盘消息循环异常")
            self._setup_done.set()

    def is_healthy(self) -> bool:
        """Observable pystray state, not Windows' user-controlled overflow setting."""
        icon, thread = self._icon, self._thread
        try:
            return bool(self._ready.is_set() and thread and thread.is_alive()
                        and icon is not None and icon.visible)
        except Exception:
            return False

    def update_title(self, title: str) -> bool:
        """A tooltip update failure is not an icon lifecycle failure."""
        icon = self._icon
        if icon is None:
            return False
        try:
            icon.title = _truncate_utf16(title)
            return True
        except Exception:
            _logger.exception("Windows 托盘状态文字更新失败")
            return False

    def stop(self) -> bool:
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                _logger.exception("移除 Windows 托盘图标失败")
        self._icon = None
        self._ready.clear()
        thread = self._thread
        if thread is not None and thread.ident is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        return thread is None or not thread.is_alive()
