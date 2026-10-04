"""WATCH-only durable scan state; candidates and their resume cursor are atomic.

The existing Profile writer lease serializes actions. No SQLite transaction is
held during scanning. This file is separate from the UI's latest-result snapshot
so a failed/partial Pipeline cannot consume candidates or mark a scan complete.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from src import data_paths
from src.sources.common import normalize_activity_id
from src.watch_feed import valid_feed_offset

STATE_VERSION = 1
WINDOW_SECONDS = 10 * 24 * 3600


def state_path() -> Path:
    return data_paths.get_database_path().parent / "watch_resume.json"


def _integer(value: object) -> bool:
    return type(value) is int and value >= 0


def _window(start: object, end: object) -> bool:
    return _integer(start) and _integer(end) and 0 <= end - start <= WINDOW_SECONDS


def validate_state(state: object) -> dict:
    """Fail closed, including idle state: corrupted data must never mean no resume."""
    def require(condition: bool) -> None:
        if not condition:
            raise ValueError("invalid WATCH state")

    try:
        require(isinstance(state, dict) and type(state["version"]) is int and state["version"] == STATE_VERSION)
        require(type(state["active"]) is bool)
        if not state["active"]:
            require(state["next_window_start"] is None or _integer(state["next_window_start"]))
            return state
        require(isinstance(state["token"], str) and bool(state["token"]))
        require(_window(state["window_start"], state["window_end"]))
        require(_integer(state["observed_at"]) and state["observed_at"] >= state["window_end"])
        require(isinstance(state["pending_links"], list))
        require(all(isinstance(link, str) and normalize_activity_id(link) for link in state["pending_links"]))
        require(isinstance(state["users"], dict))
        for uid, user in state["users"].items():
            require(isinstance(uid, str) and uid.isascii() and uid.isdigit() and int(uid) > 0)
            require(isinstance(user, dict) and _window(user["since_ts"], user["until_ts"]))
            require(type(user["complete"]) is bool)
            require(valid_feed_offset(user["offset"]))
            visited = user["seen_offsets"]
            require(isinstance(visited, list) and all(valid_feed_offset(item) for item in visited))
            require(len(set(visited)) == len(visited))
            if user["complete"]:
                require(user["offset"] == "" and bool(visited))
            else:
                require(user["offset"] not in visited)
                require(bool(user["offset"]) == bool(visited))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("WATCH 续读状态损坏，已停止扫描；未推进检查点") from exc
    return state


def load_state() -> dict | None:
    path = state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeError as exc:
        raise ValueError("WATCH 续读状态损坏，已停止扫描；未推进检查点") from exc
    try:
        state = json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("WATCH 续读状态损坏，已停止扫描；未推进检查点") from exc
    return validate_state(state)


def save_state(state: dict) -> None:
    data_paths._write_json_atomic(state_path(), validate_state(state))


def new_state(start: int, end: int, now: int) -> dict:
    return {
        "version": STATE_VERSION, "active": True, "token": uuid4().hex,
        "window_start": start, "window_end": end, "observed_at": now,
        "users": {}, "pending_links": [],
    }


def new_user(start: int, end: int) -> dict:
    return {
        "since_ts": start, "until_ts": end, "offset": "",
        "seen_offsets": [], "complete": False,
    }


def acknowledge_pipeline(token: str, *, source_complete: bool) -> None:
    """Only called after a successful, non-partial Pipeline, under writer lease.

    Checkpoint first, then retire state. A crash between the two only replays
    durable candidates; it cannot skip them or forget an unscanned page.
    """
    from src.state_store import get_watch_last_synced_at, set_watch_last_synced_at

    state = load_state()
    if state is None or not state["active"] or state["token"] != token:
        raise ValueError("WATCH 续读状态已变化，未提交检查点")
    if source_complete:
        if not all(user["complete"] for user in state["users"].values()):
            raise ValueError("WATCH 仍有未完成用户，未提交检查点")
        end = state["window_end"]
        set_watch_last_synced_at(max(get_watch_last_synced_at() or 0, end))
        # Protect time elapsed during catch-up from the normal ten-day clamp.
        # Successor windows stay <= ten days; old cursors are never reused there.
        save_state({
            "version": STATE_VERSION, "active": False,
            "next_window_start": end if state["observed_at"] > end else None,
        })
    else:
        state["pending_links"] = []
        save_state(state)
