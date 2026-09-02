"""当前 Profile 内独立、持久的转发保护及跨进程参与互斥。"""

from __future__ import annotations

import errno
import hashlib
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import Session, select

from src.app_logging import get_logger
from src.db.engine import db_path
from src.db.models import ParticipationGuardRow, RepostHistoryRow
from src.db.session import session_scope

logger = get_logger("guard")

REPOST_STATUSES = frozenset({"pending", "confirmed", "unknown", "suspected"})
BLOCKED_REPOST_STATUSES = frozenset({"pending", "unknown", "suspected"})
TRUSTED_HISTORY_IDENTITY_SOURCES = frozenset(
    {"space_feed", "legacy_space_feed", "remote_detail"}
)


@dataclass(frozen=True)
class ParticipationGuardRecord:
    uid: str
    dynamic_id: str
    repost_status: str
    updated_at: int


class ParticipationGuardBlocked(RuntimeError):
    def __init__(self, uid: str, dynamic_id: str, repost_status: str):
        self.uid = uid
        self.dynamic_id = dynamic_id
        self.repost_status = repost_status
        super().__init__(f"活动 {dynamic_id} 已有转发保护状态 {repost_status}，不能重复提交")


class ParticipationBusyError(RuntimeError):
    """另一个线程或进程正在参与当前账号的同一活动。"""


def _key(uid: str, dynamic_id: str) -> tuple[str, str]:
    if uid is None or dynamic_id is None:
        raise ValueError("参与保护的 UID 或 dynamic_id 无效")
    uid, dynamic_id = str(uid).strip(), str(dynamic_id).strip()
    if not uid or not dynamic_id or len(uid) > 64 or len(dynamic_id) > 32:
        raise ValueError("参与保护的 UID 或 dynamic_id 无效")
    return uid, dynamic_id


def _record(row: ParticipationGuardRow) -> ParticipationGuardRecord:
    if row.repost_status not in REPOST_STATUSES:
        raise RuntimeError(f"未知参与保护状态: {row.repost_status}")
    return ParticipationGuardRecord(row.uid, row.dynamic_id, row.repost_status, row.updated_at)


def get_guard(uid: str, dynamic_id: str) -> ParticipationGuardRecord | None:
    uid, dynamic_id = _key(uid, dynamic_id)
    with session_scope() as session:
        row = session.get(ParticipationGuardRow, (uid, dynamic_id))
        return _record(row) if row is not None else None


def record_pending(uid: str, dynamic_id: str) -> None:
    """POST 之前提交 pending；唯一键争用或任意旧状态都不能重复占用。"""
    uid, dynamic_id = _key(uid, dynamic_id)
    with session_scope() as session:
        result = session.execute(
            insert(ParticipationGuardRow)
            .values(uid=uid, dynamic_id=dynamic_id, repost_status="pending", updated_at=int(time.time()))
            .on_conflict_do_nothing(index_elements=["uid", "dynamic_id"])
        )
        if result.rowcount != 1:
            row = session.get(ParticipationGuardRow, (uid, dynamic_id))
            if row is None:
                raise RuntimeError("参与保护占用结果无法确认")
            raise ParticipationGuardBlocked(uid, dynamic_id, _record(row).repost_status)


def confirm_repost(uid: str, dynamic_id: str) -> None:
    uid, dynamic_id = _key(uid, dynamic_id)
    confirmed_at = int(time.time())
    with session_scope() as session:
        result = session.execute(
            insert(ParticipationGuardRow)
            .values(
                uid=uid,
                dynamic_id=dynamic_id,
                repost_status="confirmed",
                updated_at=confirmed_at,
            )
            .on_conflict_do_update(
                index_elements=["uid", "dynamic_id"],
                set_={"repost_status": "confirmed", "updated_at": confirmed_at},
                where=ParticipationGuardRow.repost_status != "confirmed",
            )
        )
        newly_confirmed = result.rowcount == 1
    # 只有 confirmed 才标记“个人空间增量同步待执行”；pending/unknown/suspected 不标记。
    # 已经是 confirmed 的幂等重复调用不是新的转发事件，不刷新 dirty 水位。
    # 真实 Bilibili UID 均为数字；非数字 uid（测试/异常数据）不进入转发账本维护。
    # 维护标记是 best-effort：任何失败都不能影响参与确认（guard 已先落库）。
    try:
        from src.repost_history import mark_sync_needed

        if newly_confirmed and str(uid).isdigit():
            mark_sync_needed(uid, occurred_at=confirmed_at)
    except Exception:
        logger.exception("标记转发历史待同步失败（不影响参与确认）")


def mark_repost_unknown(uid: str, dynamic_id: str) -> None:
    uid, dynamic_id = _key(uid, dynamic_id)
    with session_scope() as session:
        session.execute(
            update(ParticipationGuardRow)
            .where(
                ParticipationGuardRow.uid == uid,
                ParticipationGuardRow.dynamic_id == dynamic_id,
                ParticipationGuardRow.repost_status == "pending",
            )
            .values(repost_status="unknown", updated_at=int(time.time()))
        )


def mark_repost_suspected(uid: str, dynamic_id: str) -> None:
    uid, dynamic_id = _key(uid, dynamic_id)
    with session_scope() as session:
        session.execute(
            insert(ParticipationGuardRow)
            .values(uid=uid, dynamic_id=dynamic_id, repost_status="suspected", updated_at=int(time.time()))
            .on_conflict_do_nothing(index_elements=["uid", "dynamic_id"])
        )


def load_blocked_guard_ids(uid: str) -> set[str]:
    uid, _ = _key(uid, "listing")
    with session_scope() as session:
        rows = session.exec(
            select(ParticipationGuardRow)
            .where(ParticipationGuardRow.uid == uid)
        ).all()
        return {
            record.dynamic_id for row in rows
            if (record := _record(row)).repost_status in BLOCKED_REPOST_STATUSES
        }


def list_confirmed_guards(uid: str) -> list[ParticipationGuardRecord]:
    """只读列出当前 UID 已 confirmed 的转发保护记录（含 confirmed 写入时间 updated_at）。"""
    uid, _ = _key(uid, "listing")
    with session_scope() as session:
        rows = session.exec(
            select(ParticipationGuardRow)
            .where(
                ParticipationGuardRow.uid == uid,
                ParticipationGuardRow.repost_status == "confirmed",
            )
        ).all()
        return [_record(row) for row in rows]


def _trusted_history_original_ids(session: Session, uid: str) -> set[str]:
    rows = session.exec(
        select(RepostHistoryRow.original_dynamic_id)
        .where(
            RepostHistoryRow.uid == uid,
            RepostHistoryRow.identity_ok.is_(True),
            RepostHistoryRow.identity_source.in_(TRUSTED_HISTORY_IDENTITY_SOURCES),
        )
        .distinct()
    ).all()
    return {str(dynamic_id) for dynamic_id in rows}


def list_confirmed_guards_needing_history_sync(
    uid: str,
) -> list[ParticipationGuardRecord]:
    """只列出尚无可信本地 history 证明的 confirmed guard。"""
    uid, _ = _key(uid, "listing")
    with session_scope() as session:
        proven_ids = _trusted_history_original_ids(session, uid)
        rows = session.exec(
            select(ParticipationGuardRow)
            .where(
                ParticipationGuardRow.uid == uid,
                ParticipationGuardRow.repost_status == "confirmed",
            )
        ).all()
        return [
            _record(row) for row in rows
            if row.dynamic_id not in proven_ids
        ]


def reconcile_uncertain_guards_from_history(
    uid: str,
    *,
    reconciled_at: int | None = None,
) -> dict[str, int]:
    """用当前 UID 的可信转发账本单向确认 uncertain guard；全程纯本地。"""
    uid, _ = _key(uid, "listing")
    now = int(time.time()) if reconciled_at is None else int(reconciled_at)
    if now <= 0:
        raise ValueError("历史参与确认时间无效")
    with session_scope() as session:
        checked = int(
            session.exec(
                select(func.count())
                .select_from(ParticipationGuardRow)
                .where(
                    ParticipationGuardRow.uid == uid,
                    ParticipationGuardRow.repost_status.in_(BLOCKED_REPOST_STATUSES),
                )
            ).one()
        )
        resolved = 0
        trusted_originals = (
            select(RepostHistoryRow.original_dynamic_id)
            .where(
                RepostHistoryRow.uid == uid,
                RepostHistoryRow.identity_ok.is_(True),
                RepostHistoryRow.identity_source.in_(TRUSTED_HISTORY_IDENTITY_SOURCES),
            )
            .distinct()
        )
        result = session.execute(
            update(ParticipationGuardRow)
            .where(
                ParticipationGuardRow.uid == uid,
                ParticipationGuardRow.repost_status.in_(BLOCKED_REPOST_STATUSES),
                ParticipationGuardRow.dynamic_id.in_(trusted_originals),
            )
            .values(repost_status="confirmed", updated_at=now)
        )
        resolved = int(result.rowcount or 0)
        remaining = int(
            session.exec(
                select(func.count())
                .select_from(ParticipationGuardRow)
                .where(
                    ParticipationGuardRow.uid == uid,
                    ParticipationGuardRow.repost_status.in_(BLOCKED_REPOST_STATUSES),
                )
            ).one()
        )
    return {"checked": checked, "resolved": resolved, "remaining": remaining}


@contextmanager
def participation_gate(uid: str, dynamic_id: str) -> Iterator[None]:
    """不阻塞的 OS 文件锁；只锁同库/同 UID/同动态，不持有 SQLite 事务。"""
    uid, dynamic_id = _key(uid, dynamic_id)
    database = db_path().resolve()
    key = "\0".join((os.path.normcase(str(database)), uid, dynamic_id))
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    directory = database.parent / ".participation_locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{digest}.lock"
    # 不删除锁文件：删除后重建可能使两个进程锁住不同 inode。
    with path.open("a+b") as handle:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK) or getattr(exc, "winerror", None) in (32, 33):
                raise ParticipationBusyError(f"活动 {dynamic_id} 正在参与，请勿重复提交") from exc
            raise
        try:
            yield
        finally:
            handle.seek(0)
            release()
