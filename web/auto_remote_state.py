"""自动远程任务风控状态（per-Profile 本地 JSON，0 远程）。

只做自动调度层的总熔断：
- cleanup 自己的 maintenance_risk_paused 独立保留，不与这里合并。
- 任意自动远程任务（refresh batch / participate / cleanup 等）命中明确平台风控
  （-352 / -509 / HTTP 429 / rate-limit / risk-control / 风控 / 限流）时写入固定冷却。
- 冷却固定 6 小时（AUTO_REMOTE_RISK_COOLDOWN_SECONDS）。
- 冷却结束只恢复“调度资格”：程序不主动探测、不补跑；等下一次正常 Scheduler 任务。
- Profile 之间互相隔离；重启后剩余冷却仍在；无人工暂停/恢复概念。
- 不升级数据库 Schema（保持 v10）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from web.auto_config import AUTO_REMOTE_RISK_COOLDOWN_SECONDS

_PLATFORM_RISK_MARKERS = (
    "-352",
    "-509",
    "429",
    "too many",
    "rate-limit",
    "rate limit",
    "risk-control",
    "risk control",
    "风控",
    "限流",
)

_FILE_NAME = "auto_remote_state.json"


def matches_platform_risk(message: object) -> bool:
    """与现有 cleanup / DS 风控识别保持一致；不自行扩大范围（不含 -799）。"""
    lowered = str(message or "").lower()
    return any(marker in lowered for marker in _PLATFORM_RISK_MARKERS)


def _state_path() -> Path:
    from src.data_paths import get_profile_dir

    return get_profile_dir() / _FILE_NAME


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_FILE_NAME}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def risk_pause_state(now_ts: int | None = None) -> dict[str, Any]:
    """读取当前 Profile 的风控状态视图（只读本地，0 远程）。

    paused 为“当前是否仍在冷却中”：
    - paused_until 已过时 paused 视为过期（expired=True），但仍只读不联网。
    """
    state = _read_state()
    now = int(time.time()) if now_ts is None else int(now_ts)
    paused = bool(state.get("paused"))
    paused_until = state.get("paused_until")
    paused_until_int = paused_until if isinstance(paused_until, int) else None
    expired = paused and paused_until_int is not None and now >= paused_until_int
    effective_paused = paused and not expired
    return {
        "paused": effective_paused,
        "expired": bool(expired) and paused,
        "paused_at": state.get("paused_at"),
        "paused_until": paused_until_int,
        "trigger_stage": str(state.get("trigger_stage") or ""),
        "code": str(state.get("code") or ""),
        "reason": str(state.get("reason") or ""),
    }


def is_risk_paused(now_ts: int | None = None) -> bool:
    return bool(risk_pause_state(now_ts=now_ts).get("paused"))


def record_auto_remote_risk(
    *,
    trigger_stage: str,
    reason: object = "",
    code: str = "",
    now_ts: int | None = None,
    cooldown_seconds: int = AUTO_REMOTE_RISK_COOLDOWN_SECONDS,
) -> dict[str, Any]:
    """命中明确风控后写入固定冷却（per-Profile 本地，0 远程）。"""
    now = int(time.time()) if now_ts is None else int(now_ts)
    state = _read_state()
    state.update(
        {
            "paused": True,
            "paused_at": now,
            "paused_until": now + int(cooldown_seconds),
            "trigger_stage": str(trigger_stage or "").strip(),
            "code": str(code or "").strip(),
            "reason": str(reason or "").strip(),
        }
    )
    _write_state(state)
    return risk_pause_state(now_ts=now)


def clear_expired_risk_pause(now_ts: int | None = None) -> bool:
    """冷却已结束则本地清除风控（0 远程）；返回是否真的清除。

    只恢复“下一次正常调度资格”，绝不主动联网、绝不补跑。
    """
    state = risk_pause_state(now_ts=now_ts)
    if not state.get("expired"):
        return False
    now = int(time.time()) if now_ts is None else int(now_ts)
    _write_state(
        {
            "paused": False,
            "paused_at": None,
            "paused_until": None,
            "trigger_stage": "",
            "code": "",
            "reason": "",
        }
    )
    return True


def _clear_for_tests() -> None:
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass
