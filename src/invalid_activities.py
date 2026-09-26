"""Profile-local permanent exclusions for activities that must never re-enter."""

from __future__ import annotations

import time
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from src.activity_store import remove_activity_ids
from src.data_paths import get_profile_dir
from src.user_data_io import atomic_write_json

_FILE_NAME = "invalid_activities.json"
_LOCK = RLock()


def _path() -> Path:
    return get_profile_dir() / _FILE_NAME


def _read() -> dict[str, Any]:
    try:
        import json

        payload = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("activities")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key): dict(value)
        for key, value in raw.items()
        if str(key).strip() and isinstance(value, dict)
    }


def invalid_activity_ids() -> set[str]:
    """Return canonical IDs and observed wrapper aliases for this Profile."""
    with _LOCK:
        records = _records(_read())
    result: set[str] = set(records)
    for record in records.values():
        aliases = record.get("aliases")
        if isinstance(aliases, list):
            result.update(str(item).strip() for item in aliases if str(item).strip())
    return result


def is_invalid_activity(dynamic_id: str) -> bool:
    return str(dynamic_id or "").strip() in invalid_activity_ids()


def invalidate_activity(
    canonical_dynamic_id: str,
    *,
    reason: str,
    aliases: Iterable[str] = (),
) -> None:
    """Persist a Profile-local tombstone and remove all known local rows."""
    canonical = str(canonical_dynamic_id or "").strip()
    if not canonical:
        return
    alias_set = {
        str(value).strip() for value in aliases if str(value).strip() and str(value).strip() != canonical
    }
    with _LOCK:
        payload = _read()
        records = _records(payload)
        previous = records.get(canonical) or {}
        old_aliases = previous.get("aliases") if isinstance(previous.get("aliases"), list) else []
        alias_set.update(str(value).strip() for value in old_aliases if str(value).strip())
        records[canonical] = {
            "reason": str(reason or "invalid_activity"),
            "aliases": sorted(alias_set),
            "invalidated_at": int(previous.get("invalidated_at") or time.time()),
        }
        atomic_write_json(_path(), {"version": 1, "activities": records})
    remove_activity_ids({canonical, *alias_set})


def invalidate_stored_activity(canonical_dynamic_id: str, *, reason: str) -> None:
    """Invalidate a stored row while retaining any wrapper IDs recorded with it."""
    from src.activity_store import load_activities

    aliases: set[str] = set()
    canonical = str(canonical_dynamic_id or "").strip()
    for item in load_activities():
        if str(item.get("dynamic_id") or "").strip() != canonical:
            continue
        for key in ("observed_wrapper_dynamic_id", "observed_wrapper_ids"):
            value = item.get(key)
            if isinstance(value, list):
                aliases.update(str(entry).strip() for entry in value if str(entry).strip())
            elif value:
                aliases.add(str(value).strip())
        break
    invalidate_activity(canonical, reason=reason, aliases=aliases)


def is_self_follow_failure(detail: str) -> bool:
    """Recognize only the explicit self-follow business error."""
    text = str(detail or "").strip().lower()
    if "22001" not in text:
        return False
    return any(token in text for token in ("不能关注自己", "无法关注自己", "cannot follow yourself"))
