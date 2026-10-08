"""Profile-local audit of explicitly authorized UNKNOWN action attempts.

The original guards remain authoritative. This ledger records each new attempt
before a mutation and never makes an uncertain action automatically retryable.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.data_paths import get_profile_dir
from src.sources.common import is_valid_dynamic_id
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

_FILENAME = "manual_unknown_retries.json"
_ACTIONS = frozenset({"comment", "favorite", "repost"})
_RESULTS = frozenset({"pending", "confirmed", "unknown", "rejected"})
_thread_lock = threading.RLock()


class ManualRetryStateError(RuntimeError):
    pass


def _path() -> Path:
    return get_profile_dir() / _FILENAME


@contextmanager
def _state_gate() -> Iterator[None]:
    """Serialize the shared audit file even for different dynamics/processes."""
    with _thread_lock:
        path = get_profile_dir() / "manual_unknown_retries.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows production
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                handle.seek(0)
                release()


def _key(uid: str | int, dynamic_id: str, action: str) -> str:
    owner = str(uid).strip()
    target = str(dynamic_id).strip()
    if not owner.isascii() or not owner.isdigit() or int(owner) <= 0:
        raise ManualRetryStateError("人工重试缺少有效账号 UID")
    if not is_valid_dynamic_id(target) or action not in _ACTIONS:
        raise ManualRetryStateError("人工重试目标或动作无效")
    return f"{int(owner)}:{target}:{action}"


def _read_unlocked() -> dict[str, Any]:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "entries": {}}
    except (OSError, ValueError, TypeError) as exc:
        raise ManualRetryStateError("人工重试审计记录不可读，已禁止重试") from exc
    if (not isinstance(raw, dict) or type(raw.get("version")) is not int
            or raw.get("version") != 1 or not isinstance(raw.get("entries"), dict)):
        raise ManualRetryStateError("人工重试审计记录结构不受支持，已禁止重试")
    for key, attempts in raw["entries"].items():
        if not isinstance(key, str) or not isinstance(attempts, list):
            raise ManualRetryStateError("人工重试审计记录损坏，已禁止重试")
        for attempt in attempts:
            if (not isinstance(attempt, dict) or not isinstance(attempt.get("id"), str)
                    or attempt.get("status") not in _RESULTS):
                raise ManualRetryStateError("人工重试审计记录损坏，已禁止重试")
    return raw


def _write_unlocked(state: dict[str, Any]) -> None:
    path = _path()
    atomic_write_json(path, state)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def generation(uid: str | int, dynamic_id: str, action: str) -> int:
    key = _key(uid, dynamic_id, action)
    # A never-retried action has generation zero. A cancelled UI prompt must
    # not create an audit or lock file merely to inspect that fact.
    if not _path().exists():
        return 0
    with _state_gate(), user_data_lock():
        return len(_read_unlocked()["entries"].get(key, []))


def begin_attempt(
    uid: str | int, dynamic_id: str, action: str, *,
    expected_generation: int, prior_status: str,
) -> str:
    """Caller holds the per-Profile/UID/dynamic participation gate."""
    key = _key(uid, dynamic_id, action)
    if type(expected_generation) is not int or expected_generation < 0:
        raise ManualRetryStateError("人工确认已失效，请重新检查动作状态")
    if prior_status not in {"pending", "unknown"}:
        raise ManualRetryStateError("原动作不处于可人工核对的不确定状态")
    with _state_gate(), user_data_lock():
        state = _read_unlocked()
        attempts = state["entries"].setdefault(key, [])
        if len(attempts) != expected_generation:
            raise ManualRetryStateError("人工确认已被其他尝试使用，请重新检查后确认")
        attempt_id = uuid.uuid4().hex
        attempts.append({
            "id": attempt_id, "prior_status": prior_status,
            "status": "pending", "started_at": int(time.time()),
        })
        _write_unlocked(state)
        return attempt_id


def assert_pending_attempt(uid: str | int, dynamic_id: str, action: str, attempt_id: str) -> None:
    key = _key(uid, dynamic_id, action)
    with _state_gate(), user_data_lock():
        attempts = _read_unlocked()["entries"].get(key, [])
        if not attempts or attempts[-1].get("id") != attempt_id or attempts[-1].get("status") != "pending":
            raise ManualRetryStateError("人工重试授权已失效，已禁止发送请求")


def finish_attempt(uid: str | int, dynamic_id: str, action: str, attempt_id: str, status: str) -> None:
    key = _key(uid, dynamic_id, action)
    if status not in _RESULTS - {"pending"}:
        raise ManualRetryStateError("人工重试结果无效")
    with _state_gate(), user_data_lock():
        state = _read_unlocked()
        attempts = state["entries"].get(key, [])
        if not attempts or attempts[-1].get("id") != attempt_id or attempts[-1].get("status") != "pending":
            raise ManualRetryStateError("人工重试记录已变化，未覆盖原记录")
        attempts[-1]["status"] = status
        attempts[-1]["finished_at"] = int(time.time())
        _write_unlocked(state)
