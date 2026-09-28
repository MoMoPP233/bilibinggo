from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.data_paths import get_profile_dir
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

COMMENT_BREAKER_FILENAME = "comment_limit_breaker.json"
COMMENT_BREAKER_LOCK_FILENAME = "comment_limit_breaker.lock"
COMMENT_LIMIT_CODE = 12014

_thread_lock = threading.RLock()


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


def load_comment_breaker() -> dict[str, Any]:
    with _thread_lock, user_data_lock():
        return dict(_read_state_unlocked())


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
    current = _read_state_unlocked()
    state = {
        "version": 1,
        "active": True,
        "code": int(code),
        "message": str(message or "已达到当前评论发送上限"),
        "triggered_at": current.get("triggered_at") or now,
        "last_seen_at": now,
    }
    with user_data_lock():
        _write_state_unlocked(state)
    return dict(state)


def clear_comment_breaker() -> dict[str, Any]:
    state = {
        "version": 1,
        "active": False,
        "cleared_at": int(time.time()),
    }
    with comment_mutation_gate():
        with user_data_lock():
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
    }

