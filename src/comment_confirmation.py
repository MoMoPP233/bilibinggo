from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from src.comment_breaker import comment_mutation_gate
from src.data_paths import get_profile_dir
from src.sources.common import normalize_activity_id
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

_FILENAME = "comment_confirmed.json"
_thread_lock = threading.RLock()


def _state_path() -> Path:
    return get_profile_dir() / _FILENAME


def _canonical_dynamic_id(dynamic_id: str) -> str:
    value = str(dynamic_id or "").strip()
    return normalize_activity_id(value) or value


def _read_state_unlocked() -> dict[str, Any]:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
        return {"version": 1, "confirmed": {}}
    if not isinstance(raw, dict) or not isinstance(raw.get("confirmed"), dict):
        return {"version": 1, "confirmed": {}}
    return {"version": 1, "confirmed": dict(raw["confirmed"])}


def _is_comment_confirmed_unlocked(dynamic_id: str) -> bool:
    key = _canonical_dynamic_id(dynamic_id)
    if not key:
        return False
    return key in _read_state_unlocked()["confirmed"]


def is_comment_confirmed(dynamic_id: str) -> bool:
    """Return whether this Profile has durably confirmed a comment for a dynamic."""
    with _thread_lock, comment_mutation_gate(), user_data_lock():
        return _is_comment_confirmed_unlocked(dynamic_id)


def confirm_comment(dynamic_id: str) -> None:
    """Persist a successful comment by canonical dynamic identity."""
    key = _canonical_dynamic_id(dynamic_id)
    if not key:
        return
    with _thread_lock, comment_mutation_gate(), user_data_lock():
        state = _read_state_unlocked()
        confirmed = state["confirmed"]
        if key in confirmed:
            return
        confirmed[key] = time.time()
        atomic_write_json(_state_path(), state)

