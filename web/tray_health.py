"""Small, read-only health snapshot for this runtime's supervisor tray."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from web.auto_config import get_night_continue_enabled, is_night_quiet
from web.auto_remote_state import risk_pause_state
from web.auto_scheduler import CN_TZ, auto_scheduler
from web.job_runner import runner

_SCAN_ACTIONS = frozenset({
    "refresh_all", "refresh_source", "refresh_watch", "refresh_status",
    "following_feed_scan", "winner_deep_scan",
})
_TICK_MAX_AGE_SECONDS = 120


def _recent_tick(value: object, now: datetime) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        tick = datetime.fromisoformat(value)
    except ValueError:
        return False
    if tick.tzinfo is None:
        tick = tick.replace(tzinfo=CN_TZ)
    age = (now - tick).total_seconds()
    return -5 <= age <= _TICK_MAX_AGE_SECONDS


def current_tray_health() -> dict[str, Any]:
    """No registry, SQLite, remote, LLM or scheduler recomputation."""
    now = datetime.now(CN_TZ)
    scheduler = auto_scheduler.tray_snapshot()
    job = runner.get_status()
    risk = risk_pause_state()
    risk_status = str(risk.get("state_status") or "")
    risk_label = (
        "unknown" if risk_status in {"invalid", "unreadable"}
        else "paused" if risk.get("paused") else "unpaused"
    )
    scanning = bool(scheduler.get("scan_running")) or (
        job.state == "running" and job.action in _SCAN_ACTIONS
    )
    state = str(scheduler.get("state") or "")
    running = (state == "running" and scheduler.get("thread_alive")
               and _recent_tick(scheduler.get("last_tick_at"), now))
    if scanning:
        status = "scanning"
    elif state in {"idle", "stopped"}:
        status = "stopped"
    elif not running or risk_label == "unknown":
        status = "unavailable"
    elif risk_label == "paused":
        status = "risk_paused"
    elif is_night_quiet(now, config=get_night_continue_enabled()):
        status = "night_quiet"
    else:
        status = "normal"

    next_scan = scheduler.get("next_scan_unix")
    if status in {"stopped", "risk_paused", "unavailable"} or not isinstance(next_scan, int) or next_scan <= int(now.timestamp()):
        next_scan = None
    last_scan = scheduler.get("last_successful_scan_at")
    if isinstance(last_scan, str):
        try:
            datetime.fromisoformat(last_scan)
        except ValueError:
            last_scan = None
    else:
        last_scan = None
    return {
        "status": status,
        "risk": risk_label,
        "last_scan_at": last_scan,
        "next_scan_unix": next_scan,
    }
