"""Profile-local, UID-scoped durable barrier for favorite mutations."""

from __future__ import annotations

import json
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from sqlmodel import select

from src.data_paths import get_profile_dir
from src.db.models import ParticipationActionRow
from src.db.session import session_scope
from src.sources.common import is_valid_dynamic_id
from src.user_data_io import atomic_write_json
from src.user_data_lock import user_data_lock

_FILENAME = "favorite_confirmed.json"
_LOCK_FILENAME = "favorite_confirmed.lock"
_STATUSES = {"pending", "confirmed", "unknown", "failed"}
_BLOCKING = {"pending", "confirmed", "unknown"}
_thread_lock = threading.RLock()


class FavoriteGuardStateError(RuntimeError):
    """Unreadable or unwritable safety state must stop the mutation."""


def _state_path() -> Path:
    return get_profile_dir() / _FILENAME


def _uid_key(uid: str | int) -> str:
    value = str(uid).strip()
    if not value.isascii() or not value.isdigit() or int(value) <= 0:
        raise FavoriteGuardStateError("收藏防重缺少有效账号 UID，已禁止发送收藏")
    return str(int(value))


def _target_key(dynamic_id: str) -> str:
    value = str(dynamic_id or "").strip()
    if not is_valid_dynamic_id(value):
        raise FavoriteGuardStateError("无法确定收藏目标动态 ID，已禁止发送收藏")
    return value


def _read_state_unlocked() -> dict[str, Any]:
    try:
        raw = json.loads(_state_path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "guards": {}}
    except (OSError, ValueError, TypeError) as exc:
        raise FavoriteGuardStateError("收藏防重记录无法读取，已禁止发送收藏") from exc
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or not isinstance(raw.get("guards"), dict)
    ):
        raise FavoriteGuardStateError("收藏防重记录结构或版本不受支持，已禁止发送收藏")
    for owner, records in raw["guards"].items():
        try:
            valid_owner = _uid_key(owner) == owner
        except FavoriteGuardStateError:
            valid_owner = False
        if not valid_owner or not isinstance(records, dict):
            raise FavoriteGuardStateError("收藏防重记录身份损坏，已禁止发送收藏")
        for target, record in records.items():
            if (
                not isinstance(target, str)
                or not is_valid_dynamic_id(target)
                or not isinstance(record, dict)
                or record.get("status") not in _STATUSES
            ):
                raise FavoriteGuardStateError("收藏防重状态损坏，已禁止发送收藏")
    return raw


def _write_state_unlocked(state: dict[str, Any]) -> None:
    path = _state_path()
    atomic_write_json(path, state)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


@contextmanager
def favorite_mutation_gate() -> Iterator[None]:
    """Serialize favorite state checks and POSTs across threads/processes in one Profile."""
    with _thread_lock:
        path = get_profile_dir() / _LOCK_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a+b") as handle:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                unlock = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows production uses msvcrt
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                unlock = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            try:
                yield
            finally:
                handle.seek(0)
                unlock()


def _legacy_unknown_exists(uid: str, dynamic_id: str) -> bool:
    """Only exact, still-retained evidence of an uncertain favorite qualifies."""
    with session_scope() as session:
        snapshots = session.exec(
            select(ParticipationActionRow.context_snapshot_json, ParticipationActionRow.actions_json).where(
                ParticipationActionRow.uid == uid,
                ParticipationActionRow.dynamic_id == dynamic_id,
            )
        ).all()
    for snapshot_text, actions_text in snapshots:
        try:
            snapshot = json.loads(snapshot_text)
            actions = json.loads(actions_text)
        except (TypeError, ValueError) as exc:
            raise FavoriteGuardStateError("旧参与记录无法判读，已禁止发送收藏") from exc
        if (
            isinstance(snapshot, dict)
            and snapshot.get("result_class") == "unknown"
            and snapshot.get("unknown_action") == "favorite"
        ):
            return True
        # The old code=0 + failed fresh-read path used this exact detail but
        # did not label its action log as unknown. It is still uncertain.
        if isinstance(actions, list) and any(
            isinstance(action, dict)
            and action.get("action") == "favorite"
            and action.get("ok") is False
            and action.get("detail") == "收藏状态未确认，未继续查询"
            for action in actions
        ):
            return True
    return False


def _get_favorite_status_unlocked(dynamic_id: str, *, uid: str | int) -> str | None:
    """Caller holds favorite_mutation_gate; migrate only exact retained unknown evidence."""
    owner, target = _uid_key(uid), _target_key(dynamic_id)
    with user_data_lock():
        state = _read_state_unlocked()
        record = state["guards"].get(owner, {}).get(target)
        if record is not None:
            return record["status"]
        if not _legacy_unknown_exists(owner, target):
            return None
        state["guards"].setdefault(owner, {})[target] = {
            "status": "unknown", "updated_at": int(time.time()),
        }
        _write_state_unlocked(state)
        return "unknown"


def get_favorite_status(dynamic_id: str, *, uid: str | int) -> str | None:
    with favorite_mutation_gate():
        return _get_favorite_status_unlocked(dynamic_id, uid=uid)


def load_favorite_statuses(*, uid: str | int) -> dict[str, str]:
    """Read the durable ledger once for automatic candidate selection."""
    owner = _uid_key(uid)
    with favorite_mutation_gate(), user_data_lock():
        state = _read_state_unlocked()
        return {target: record["status"] for target, record in state["guards"].get(owner, {}).items()}


def _set_favorite_status_unlocked(dynamic_id: str, *, uid: str | int, status: str) -> None:
    """Caller holds favorite_mutation_gate; pending is durable before POST."""
    owner, target = _uid_key(uid), _target_key(dynamic_id)
    if status not in _STATUSES:
        raise FavoriteGuardStateError("收藏防重状态无效，已禁止发送收藏")
    with user_data_lock():
        state = _read_state_unlocked()
        records = state["guards"].setdefault(owner, {})
        previous = records.get(target, {}).get("status")
        if status == "pending" and previous in _BLOCKING:
            raise FavoriteGuardStateError("收藏已有防重记录，禁止重复发送")
        if status in {"unknown", "failed"} and previous != "pending":
            raise FavoriteGuardStateError("收藏没有对应待确认请求，禁止改写防重状态")
        if status == "confirmed" and previous == "unknown":
            raise FavoriteGuardStateError("收藏结果未知，不能自动解除防重保护")
        if previous == status and status != "pending":
            return
        records[target] = {"status": status, "updated_at": int(time.time())}
        _write_state_unlocked(state)
