"""Profile-local manual activity exclusions.

Manual ignores are deliberately separate from invalid-activity tombstones:
the former records a local user choice and must not imply that Bilibili
reported the activity as deleted or otherwise invalid.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Any, Iterable

from src.activity_store import load_activities, remove_activity_ids
from src.data_paths import get_profile_dir
from src.user_data_io import atomic_write_json

_FILE_NAME = "manual_ignored_activities.json"
_LOCK = RLock()


def _path() -> Path:
    return get_profile_dir() / _FILE_NAME


def _read() -> dict[str, Any]:
    try:
        payload = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _records(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = payload.get("activities")
    if not isinstance(raw, dict):
        return {}
    return {
        str(key).strip(): dict(value)
        for key, value in raw.items()
        if str(key).strip() and isinstance(value, dict)
    }


def manual_ignore_ids() -> set[str]:
    """Return canonical IDs and all known wrapper aliases for this Profile."""
    with _LOCK:
        records = _records(_read())
    result: set[str] = set(records)
    for record in records.values():
        aliases = record.get("aliases")
        if isinstance(aliases, list):
            result.update(str(item).strip() for item in aliases if str(item).strip())
    return result


def is_manual_ignored(dynamic_id: str) -> bool:
    return str(dynamic_id or "").strip() in manual_ignore_ids()


def ignore_activity(
    canonical_dynamic_id: str,
    *,
    aliases: Iterable[str] = (),
    reason: str = "manual_ignore",
) -> set[str]:
    """Persist one local ignore tombstone and remove matching local rows."""
    canonical = str(canonical_dynamic_id or "").strip()
    if not canonical:
        raise ValueError("活动 ID 不能为空")
    alias_set = {
        str(value).strip()
        for value in aliases
        if str(value).strip() and str(value).strip() != canonical
    }
    with _LOCK:
        payload = _read()
        records = _records(payload)
        previous = records.get(canonical) or {}
        old_aliases = previous.get("aliases") if isinstance(previous.get("aliases"), list) else []
        alias_set.update(str(value).strip() for value in old_aliases if str(value).strip())
        records[canonical] = {
            "reason": str(reason or "manual_ignore"),
            "aliases": sorted(alias_set),
            "ignored_at": int(previous.get("ignored_at") or time.time()),
        }
        atomic_write_json(_path(), {"version": 1, "activities": records})
    remove_activity_ids({canonical, *alias_set})
    return {canonical, *alias_set}


def ignore_stored_activity(dynamic_id: str) -> set[str]:
    """Ignore a listed activity, retaining canonical and wrapper identities."""
    requested = str(dynamic_id or "").strip()
    if not requested:
        raise ValueError("活动 ID 不能为空")
    canonical = requested
    aliases: set[str] = set()
    for item in load_activities():
        row_id = str(item.get("dynamic_id") or "").strip()
        row_aliases: set[str] = set()
        for key in ("observed_wrapper_dynamic_id", "observed_wrapper_ids"):
            value = item.get(key)
            if isinstance(value, list):
                row_aliases.update(str(entry).strip() for entry in value if str(entry).strip())
            elif value:
                row_aliases.add(str(value).strip())
        if requested != row_id and requested not in row_aliases:
            continue
        canonical = row_id or requested
        aliases.update(row_aliases)
        break
    aliases.add(requested)
    aliases.discard(canonical)
    return ignore_activity(canonical, aliases=aliases)

