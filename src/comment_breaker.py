from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.app_logging import get_logger
from src.data_paths import get_profile_dir
from src.sources.common import normalize_activity_id
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

COMMENT_BREAKER_FILENAME = "comment_limit_breaker.json"
COMMENT_BREAKER_LOCK_FILENAME = "comment_limit_breaker.lock"
COMMENT_LIMIT_CODE = 12014
COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS = 60 * 60
PROBE_EXCLUDED_DYNAMIC_IDS_KEY = "probe_excluded_dynamic_ids"

_thread_lock = threading.RLock()
logger = get_logger("comment_breaker")


class CommentLimitActive(RuntimeError):
    def __init__(self, message: str = "评论发送已暂停：已达到当前评论发送上限") -> None:
        self.code = COMMENT_LIMIT_CODE
        super().__init__(message)


def _state_path() -> Path:
    return get_profile_dir() / COMMENT_BREAKER_FILENAME


def _lock_path() -> Path:
    return get_profile_dir() / COMMENT_BREAKER_LOCK_FILENAME


def _read_state_unlocked() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "active": False}
    except (OSError, json.JSONDecodeError, TypeError):
        return {
            "version": 1,
            "active": True,
            "code": COMMENT_LIMIT_CODE,
            "message": "本地评论熔断状态损坏，已安全暂停评论发送",
            "corrupt": True,
        }
    if not isinstance(raw, dict):
        return {
            "version": 1,
            "active": True,
            "code": COMMENT_LIMIT_CODE,
            "message": "本地评论熔断状态损坏，已安全暂停评论发送",
            "corrupt": True,
        }
    return raw


def _state_with_defaults(state: dict[str, Any]) -> dict[str, Any]:
    """Fill recovery fields in memory without rewriting legacy files."""
    normalized = dict(state)
    excluded: list[str] = []
    raw_excluded = normalized.get(PROBE_EXCLUDED_DYNAMIC_IDS_KEY)
    if isinstance(raw_excluded, list):
        for value in raw_excluded:
            canonical = normalize_activity_id(str(value or "").strip()) or str(value or "").strip()
            if canonical and canonical not in excluded:
                excluded.append(canonical)
    normalized[PROBE_EXCLUDED_DYNAMIC_IDS_KEY] = excluded
    if bool(normalized.get("active")):
        triggered_at = normalized.get("triggered_at")
        try:
            triggered_at_int = int(triggered_at)
        except (TypeError, ValueError):
            triggered_at_int = 0
        if "next_probe_at" not in normalized and triggered_at_int:
            normalized["next_probe_at"] = (
                triggered_at_int + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
            )
        normalized.setdefault("probe_count", 0)
        normalized.setdefault("last_probe_at", None)
        normalized.setdefault("last_probe_result", None)
        normalized.setdefault("last_probe_dynamic_id", None)
    else:
        normalized.setdefault("next_probe_at", None)
        normalized.setdefault("probe_count", 0)
        normalized.setdefault("last_probe_at", None)
        normalized.setdefault("last_probe_result", None)
        normalized.setdefault("last_probe_dynamic_id", None)
    return normalized


def _restore_legacy_probe_schedule(state: dict[str, Any]) -> dict[str, Any]:
    """Re-arm only the V51 step-3 terminal 12014 state.

    Unknown, inconclusive, and in-flight states intentionally remain terminal
    because their comment mutation outcome is not safely repeatable.
    """
    normalized = dict(state)
    try:
        probe_count = int(normalized.get("probe_count") or 0)
        code = int(normalized.get("code") or 0)
    except (TypeError, ValueError):
        return normalized
    if (
        normalized.get("active")
        and code == COMMENT_LIMIT_CODE
        and str(normalized.get("last_probe_result") or "") == "12014"
        and probe_count >= 2
        and normalized.get("next_probe_at") is None
    ):
        try:
            normalized["next_probe_at"] = int(normalized["last_probe_at"]) + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
        except (TypeError, ValueError):
            pass
    return normalized


def _exclude_probe_dynamic(state: dict[str, Any], dynamic_id: str | None) -> None:
    canonical = normalize_activity_id(str(dynamic_id or "").strip()) or str(dynamic_id or "").strip()
    if not canonical:
        return
    excluded = state.setdefault(PROBE_EXCLUDED_DYNAMIC_IDS_KEY, [])
    if canonical not in excluded:
        excluded.append(canonical)


def load_comment_breaker() -> dict[str, Any]:
    with _thread_lock, user_data_lock():
        raw = _read_state_unlocked()
        state = _restore_legacy_probe_schedule(_state_with_defaults(raw))
        if state.get("next_probe_at") != raw.get("next_probe_at") and state.get("last_probe_result") == "12014":
            _write_state_unlocked(state)
        return state


def is_comment_breaker_active() -> bool:
    return bool(load_comment_breaker().get("active"))


def _write_state_unlocked(state: dict[str, Any]) -> None:
    atomic_write_json(_state_path(), state)


@contextmanager
def comment_mutation_gate() -> Iterator[None]:
    """Serialize comment POSTs per Profile across threads and processes."""
    with _thread_lock:
        path = _lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on Linux CI only
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                handle.seek(0)
                unlock()


def trigger_comment_limit(code: int = COMMENT_LIMIT_CODE, message: str = "") -> dict[str, Any]:
    with comment_mutation_gate():
        return _trigger_comment_limit_unlocked(code=code, message=message)


def _trigger_comment_limit_unlocked(
    *, code: int = COMMENT_LIMIT_CODE, message: str = ""
) -> dict[str, Any]:
    now = int(time.time())
    current = _state_with_defaults(_read_state_unlocked())
    already_active = bool(current.get("active"))
    triggered_at = current.get("triggered_at") if already_active else now
    try:
        triggered_at = int(triggered_at)
    except (TypeError, ValueError):
        triggered_at = now
    state = {
        "version": 1,
        "active": True,
        "code": int(code),
        "message": str(message or "已达到当前评论发送上限"),
        "triggered_at": triggered_at,
        "last_seen_at": now,
        # Preserve an explicit null after the second failed recovery probe;
        # a newly triggered breaker always gets its initial one-hour schedule.
        "next_probe_at": (
            current.get("next_probe_at")
            if already_active
            else triggered_at + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
        ),
        "probe_count": int(current.get("probe_count") or 0),
        "last_probe_at": current.get("last_probe_at"),
        "last_probe_result": current.get("last_probe_result"),
        "last_probe_dynamic_id": current.get("last_probe_dynamic_id"),
        PROBE_EXCLUDED_DYNAMIC_IDS_KEY: [] if not already_active else list(current.get(PROBE_EXCLUDED_DYNAMIC_IDS_KEY) or []),
    }
    with user_data_lock():
        _write_state_unlocked(state)
    if not already_active:
        logger.info("评论暂停：B站返回12014，1小时后自动探测")
    return dict(state)


def clear_comment_breaker() -> dict[str, Any]:
    with comment_mutation_gate():
        with user_data_lock():
            current = _restore_legacy_probe_schedule(_state_with_defaults(_read_state_unlocked()))
            state = {
                "version": 1,
                "active": False,
                "cleared_at": int(time.time()),
                "next_probe_at": None,
                "probe_count": int(current.get("probe_count") or 0),
                "last_probe_at": current.get("last_probe_at"),
                "last_probe_result": current.get("last_probe_result"),
                "last_probe_dynamic_id": current.get("last_probe_dynamic_id"),
                PROBE_EXCLUDED_DYNAMIC_IDS_KEY: [],
            }
            _write_state_unlocked(state)
    return dict(state)


def claim_comment_probe(dynamic_id: str, *, now: int | None = None) -> dict[str, Any] | None:
    """Atomically claim the one permitted recovery probe for a candidate."""
    canonical = normalize_activity_id(str(dynamic_id or "").strip()) or str(dynamic_id or "").strip()
    if not canonical:
        return None
    stamp = int(time.time() if now is None else now)
    with comment_mutation_gate():
        with user_data_lock():
            state = _restore_legacy_probe_schedule(_state_with_defaults(_read_state_unlocked()))
            try:
                code = int(state.get("code") or 0)
            except (TypeError, ValueError):
                return None
            if not state.get("active") or code != COMMENT_LIMIT_CODE:
                return None
            if canonical in set(state.get(PROBE_EXCLUDED_DYNAMIC_IDS_KEY) or []):
                return None
            try:
                next_probe_at = int(state.get("next_probe_at"))
            except (TypeError, ValueError):
                return None
            if stamp < next_probe_at:
                return None
            state["probe_count"] = int(state.get("probe_count") or 0) + 1
            state["last_probe_at"] = stamp
            state["last_probe_result"] = "in_flight"
            state["last_probe_dynamic_id"] = canonical
            state["next_probe_at"] = None
            _write_state_unlocked(state)
            return dict(state)


def comment_probe_due(*, now: int | None = None) -> bool:
    """Read-only eligibility check; does not claim or change breaker state."""
    stamp = int(time.time() if now is None else now)
    with _thread_lock, user_data_lock():
        state = _restore_legacy_probe_schedule(_state_with_defaults(_read_state_unlocked()))
        try:
            next_probe_at = int(state.get("next_probe_at"))
            code = int(state.get("code") or 0)
        except (TypeError, ValueError):
            return False
        return bool(state.get("active")) and code == COMMENT_LIMIT_CODE and stamp >= next_probe_at


def record_comment_probe_result(
    result: str,
    *,
    now: int | None = None,
    last_seen: bool = False,
) -> dict[str, Any]:
    """Persist one probe result and schedule the next 12014 recovery probe."""
    stamp = int(time.time() if now is None else now)
    with comment_mutation_gate():
        with user_data_lock():
            state = _restore_legacy_probe_schedule(_state_with_defaults(_read_state_unlocked()))
            state["last_probe_result"] = str(result)
            if str(result) in {"unknown", "inconclusive"}:
                _exclude_probe_dynamic(state, state.get("last_probe_dynamic_id"))
            if str(result) == "12014":
                # Every next probe is relative to the last real probe, never
                # to the original breaker trigger or a nominal probe count.
                first_probe_at = state.get("last_probe_at")
                try:
                    state["next_probe_at"] = int(first_probe_at) + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
                except (TypeError, ValueError):
                    state["next_probe_at"] = stamp + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
            elif str(result) in {"unknown", "inconclusive"}:
                # The current dynamic is excluded, but the Profile may safely
                # test another activity after the recovery interval.
                last_probe_at = state.get("last_probe_at")
                try:
                    state["next_probe_at"] = int(last_probe_at) + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
                except (TypeError, ValueError):
                    state["next_probe_at"] = stamp + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
            else:
                # Other terminal results remain paused and must not resend the
                # mutation whose outcome could not be established.
                state["next_probe_at"] = None
            if last_seen:
                state["last_seen_at"] = stamp
            _write_state_unlocked(state)
            return dict(state)


def recover_stale_comment_probe(*, now: int | None = None) -> dict[str, Any]:
    """Convert a persisted crash-time in-flight claim into a safe unknown probe.

    This is startup-only recovery. Normal scheduler ticks must leave an active
    in-flight mutation untouched because the comment POST may still be running.
    """
    stamp = int(time.time() if now is None else now)
    with comment_mutation_gate():
        with user_data_lock():
            state = _restore_legacy_probe_schedule(_state_with_defaults(_read_state_unlocked()))
            try:
                code = int(state.get("code") or 0)
            except (TypeError, ValueError):
                return dict(state)
            if (
                state.get("active")
                and code == COMMENT_LIMIT_CODE
                and state.get("last_probe_result") == "in_flight"
                and state.get("next_probe_at") is None
            ):
                try:
                    last_probe_at = int(state.get("last_probe_at"))
                except (TypeError, ValueError):
                    return dict(state)
                dynamic_id = state.get("last_probe_dynamic_id")
                canonical = normalize_activity_id(str(dynamic_id or "").strip()) or str(dynamic_id or "").strip()
                if not canonical:
                    return dict(state)
                _exclude_probe_dynamic(state, canonical)
                state["last_probe_result"] = "unknown"
                state["next_probe_at"] = last_probe_at + COMMENT_RECOVERY_PROBE_INTERVAL_SECONDS
                _write_state_unlocked(state)
            return dict(state)


def comment_breaker_snapshot() -> dict[str, Any]:
    state = load_comment_breaker()
    return {
        "active": bool(state.get("active")),
        "code": state.get("code"),
        "message": str(state.get("message") or ""),
        "triggered_at": state.get("triggered_at"),
        "last_seen_at": state.get("last_seen_at"),
        "next_probe_at": state.get("next_probe_at"),
        "probe_count": state.get("probe_count", 0),
        "last_probe_at": state.get("last_probe_at"),
        "last_probe_result": state.get("last_probe_result"),
        "last_probe_dynamic_id": state.get("last_probe_dynamic_id"),
        PROBE_EXCLUDED_DYNAMIC_IDS_KEY: list(state.get(PROBE_EXCLUDED_DYNAMIC_IDS_KEY) or []),
    }

