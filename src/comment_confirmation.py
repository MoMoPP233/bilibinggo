from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from src.bilibili_auth import get_login_uid
from src.comment_breaker import comment_mutation_gate
from src.data_paths import get_profile_dir
from src.sources.common import normalize_activity_id
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

_FILENAME = "comment_confirmed.json"
_STATUSES = {"pending", "confirmed", "unknown", "failed"}
BLOCKING_COMMENT_STATUSES = {"pending", "unknown", "confirmed"}


class CommentGuardStateError(RuntimeError):
    """An unreadable safety ledger must never be treated as an empty ledger."""


def _state_path() -> Path:
    return get_profile_dir() / _FILENAME


def _canonical_dynamic_id(dynamic_id: str) -> str:
    value = str(dynamic_id or "").strip()
    return normalize_activity_id(value) or value


def _read_state_unlocked() -> dict[str, Any]:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 2, "confirmed": {}, "guards": {}}
    except (OSError, ValueError, TypeError) as exc:
        raise CommentGuardStateError("评论防重记录无法读取，已禁止发送评论") from exc
    if (
        not isinstance(raw, dict)
        or type(raw.get("version", 1)) is not int
        or raw.get("version", 1) not in {1, 2}
        or not isinstance(raw.get("confirmed"), dict)
        or not isinstance(raw.get("guards", {}), dict)
        or (raw.get("version") == 2 and "guards" not in raw)
    ):
        raise CommentGuardStateError("评论防重记录结构或版本不受支持，已禁止发送评论")
    for owner, records in raw.get("guards", {}).items():
        if not _uid_key(owner) or not isinstance(records, dict) or any(
            not key.strip()
            or not isinstance(record, dict)
            or not isinstance(record.get("status"), str)
            or record["status"] not in _STATUSES
            for key, record in records.items()
        ):
            raise CommentGuardStateError("评论防重状态损坏，已禁止发送评论")
    return {**raw, "version": 2, "guards": raw.get("guards", {})}


def _uid_key(uid: str | int | None) -> str:
    value = str(uid if uid is not None else (get_login_uid() or "")).strip()
    return str(int(value)) if value.isascii() and value.isdigit() and int(value) > 0 else ""


def _statuses_unlocked(uid: str | int | None) -> dict[str, str]:
    state = _read_state_unlocked()
    owner = _uid_key(uid)
    records = state["guards"].get(owner, {}) if owner else {}
    statuses = {key: record["status"] for key, record in records.items()}
    # V1 had no UID. By explicit compatibility policy its positive evidence
    # remains Profile-wide; do not guess ownership or manufacture unknowns
    # from old logs. All new records are stored only in UID-scoped guards.
    statuses.update({_canonical_dynamic_id(key): "confirmed" for key in state["confirmed"]})
    return statuses


def _get_comment_status_unlocked(dynamic_id: str, *, uid: str | int | None = None) -> str | None:
    return _statuses_unlocked(uid).get(_canonical_dynamic_id(dynamic_id))


def load_comment_statuses(*, uid: str | int | None = None) -> dict[str, str]:
    """Read once for candidate selection; no age-based expiry or log dependency."""
    with comment_mutation_gate(), user_data_lock():
        return _statuses_unlocked(uid)


def get_comment_status(dynamic_id: str, *, uid: str | int | None = None) -> str | None:
    return load_comment_statuses(uid=uid).get(_canonical_dynamic_id(dynamic_id))


def _write_state_unlocked(state: dict[str, Any]) -> None:
    path = _state_path()
    atomic_write_json(path, state)
    # A successful pending write must precede the network call, including
    # flushing its file contents. Reuse the existing atomic replace helper.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _set_comment_status_unlocked(dynamic_id: str, *, uid: str | int, status: str) -> None:
    """Caller holds comment_mutation_gate; never reacquire its OS file lock."""
    key, owner = _canonical_dynamic_id(dynamic_id), _uid_key(uid)
    if not key or not owner or status not in _STATUSES:
        raise CommentGuardStateError("缺少有效评论防重身份或状态，已禁止发送评论")
    with user_data_lock():
        state = _read_state_unlocked()
        records = state["guards"].setdefault(owner, {})
        previous = records.get(key, {}).get("status")
        if previous == "confirmed" or (previous == status and status != "pending"):
            return
        if status == "pending" and previous in BLOCKING_COMMENT_STATUSES:
            raise CommentGuardStateError("评论已有未确认请求，禁止重新发送")
        if status == "failed" and previous != "pending":
            raise CommentGuardStateError("没有对应待确认评论，禁止解除安全屏障")
        records[key] = {"status": status, "updated_at": time.time()}
        _write_state_unlocked(state)


def _is_comment_confirmed_unlocked(dynamic_id: str, *, uid: str | int | None = None) -> bool:
    return _get_comment_status_unlocked(dynamic_id, uid=uid) == "confirmed"


def is_comment_confirmed(dynamic_id: str, *, uid: str | int | None = None) -> bool:
    """Return whether this Profile has durably confirmed a comment for a dynamic."""
    return get_comment_status(dynamic_id, uid=uid) == "confirmed"


def confirm_comment(dynamic_id: str, *, uid: str | int | None = None) -> None:
    """Persist a successful comment by canonical dynamic identity."""
    with comment_mutation_gate():
        if _is_comment_confirmed_unlocked(dynamic_id, uid=uid):
            return
        _set_comment_status_unlocked(dynamic_id, uid=_uid_key(uid), status="confirmed")
