"""当前 runtime Profile 内独立的个人转发历史与删除状态账本。"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import case, delete, func, or_, update
from sqlalchemy.dialects.sqlite import insert
from sqlmodel import select

from src.db.models import RepostAssessmentRow, RepostHistoryRow, RepostSyncCheckpointRow
from src.db.session import session_scope

REPOST_SOURCES = frozenset({"binggo", "history_import"})
ASSESSMENT_LEVELS = frozenset({"safe", "manual_review", "blocked", "excluded"})
ASSESSMENT_STATUSES = frozenset({"final", "refreshable", "retryable_unknown"})
IDENTITY_SOURCES = frozenset({"space_feed", "legacy_space_feed", "remote_detail"})
CLASSIFICATION_SOURCES = frozenset({"activities", "public_classifier", "legacy"})
DELETE_STATUSES = frozenset(
    {"active", "delete_pending", "deleted", "delete_failed", "unknown"}
)
DELETE_RESULT_STATUSES = frozenset({"deleted", "delete_failed", "unknown"})
DELETE_CLAIMABLE_STATUSES = frozenset({"active", "delete_failed"})


@dataclass(frozen=True)
class RepostImportRecord:
    repost_dynamic_id: str
    original_dynamic_id: str
    reposted_at: int
    original_author_uid: str | None = None
    original_author_name: str | None = None
    source: str = "history_import"


@dataclass(frozen=True)
class RepostHistoryRecord:
    uid: str
    repost_dynamic_id: str
    original_dynamic_id: str
    reposted_at: int
    original_author_uid: str | None
    original_author_name: str | None
    source: str
    delete_status: str
    delete_requested_at: int | None
    deleted_at: int | None
    last_seen_at: int | None
    last_error: str | None
    updated_at: int
    identity_source: str | None = None
    identity_checked_at: int | None = None
    identity_ok: bool | None = None
    identity_error: str | None = None
    cleanup_defer_reason: str | None = None
    cleanup_deferred_at: int | None = None
    cleanup_deferred_until: int | None = None


@dataclass(frozen=True)
class RepostSyncCheckpoint:
    uid: str
    head_dynamic_id: str | None
    head_published_at: int | None
    full_scan_completed: bool
    last_synced_at: int | None
    updated_at: int


@dataclass(frozen=True)
class RepostAssessmentRecord:
    uid: str
    original_dynamic_id: str
    assessed_at: int
    assessment_level: str = "safe"
    assessment_status: str = "final"
    lottery_time_reliable: bool = False
    reason_code: str | None = None
    lottery_type: str | None = None
    lottery_time: int | None = None
    eligible_after: int | None = None
    reason: str | None = None
    classification_source: str = "activities"
    summary: str | None = None
    evaluated_at: int | None = None
    remote_checked_at: int | None = None
    updated_at: int = 0


def _uid(value: object) -> str:
    if value is None:
        raise ValueError("转发历史 UID 无效")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 64 or not normalized.isdigit():
        raise ValueError("转发历史 UID 无效")
    return normalized


def _dynamic_id(value: object, *, label: str) -> str:
    if value is None:
        raise ValueError(f"{label} 无效")
    normalized = str(value).strip()
    if not normalized or len(normalized) > 32 or not normalized.isdigit():
        raise ValueError(f"{label} 无效")
    return normalized


def _timestamp(value: object, *, label: str, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{label} 无效")
    try:
        normalized = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 无效") from exc
    if normalized <= 0:
        raise ValueError(f"{label} 无效")
    return normalized


def _optional_text(value: object, *, max_length: int | None = None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if max_length is not None and len(normalized) > max_length:
        raise ValueError("转发历史文本字段过长")
    return normalized


def _value(record: object, field: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(field, default)
    return getattr(record, field, default)


def _history_record(row: RepostHistoryRow) -> RepostHistoryRecord:
    if row.source not in REPOST_SOURCES or row.delete_status not in DELETE_STATUSES:
        raise RuntimeError("转发历史包含未知状态")
    return RepostHistoryRecord(
        uid=row.uid,
        repost_dynamic_id=row.repost_dynamic_id,
        original_dynamic_id=row.original_dynamic_id,
        reposted_at=row.reposted_at,
        original_author_uid=row.original_author_uid,
        original_author_name=row.original_author_name,
        source=row.source,
        delete_status=row.delete_status,
        delete_requested_at=row.delete_requested_at,
        deleted_at=row.deleted_at,
        last_seen_at=row.last_seen_at,
        last_error=row.last_error,
        identity_source=row.identity_source,
        identity_checked_at=row.identity_checked_at,
        identity_ok=row.identity_ok,
        identity_error=row.identity_error,
        cleanup_defer_reason=row.cleanup_defer_reason,
        cleanup_deferred_at=row.cleanup_deferred_at,
        cleanup_deferred_until=row.cleanup_deferred_until,
        updated_at=row.updated_at,
    )


def _checkpoint(row: RepostSyncCheckpointRow) -> RepostSyncCheckpoint:
    return RepostSyncCheckpoint(
        uid=row.uid,
        head_dynamic_id=row.head_dynamic_id,
        head_published_at=row.head_published_at,
        full_scan_completed=bool(row.full_scan_completed),
        last_synced_at=row.last_synced_at,
        updated_at=row.updated_at,
    )


def upsert_repost_records(
    uid: str,
    records: Iterable[RepostImportRecord | RepostHistoryRecord | Mapping[str, object]],
    *,
    seen_at: int | None = None,
) -> int:
    """幂等导入空间转发；发现同一 repost 对应另一个 original 时失败关闭。"""
    scoped_uid = _uid(uid)
    now = int(time.time()) if seen_at is None else int(_timestamp(seen_at, label="扫描时间"))
    prepared: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for record in records:
        record_uid = _value(record, "uid")
        if record_uid is not None and _uid(record_uid) != scoped_uid:
            raise ValueError("转发历史记录不属于当前 UID")
        repost_id = _dynamic_id(_value(record, "repost_dynamic_id"), label="转发动态 ID")
        original_id = _dynamic_id(_value(record, "original_dynamic_id"), label="原动态 ID")
        if repost_id == original_id:
            raise ValueError("转发动态 ID 不能等于原动态 ID")
        if repost_id in seen_ids:
            raise ValueError(f"本批次包含重复转发动态 ID：{repost_id}")
        seen_ids.add(repost_id)
        source = str(_value(record, "source", "history_import")).strip()
        if source not in REPOST_SOURCES:
            raise ValueError("转发历史来源无效")
        author_uid_raw = _value(record, "original_author_uid")
        author_uid = _uid(author_uid_raw) if author_uid_raw not in (None, "") else None
        trusted_identity = source == "history_import"
        prepared.append(
            {
                "uid": scoped_uid,
                "repost_dynamic_id": repost_id,
                "original_dynamic_id": original_id,
                "reposted_at": int(
                    _timestamp(_value(record, "reposted_at"), label="转发时间")
                ),
                "original_author_uid": author_uid,
                "original_author_name": _optional_text(
                    _value(record, "original_author_name"), max_length=256
                ),
                "source": source,
                "delete_status": "active",
                "delete_requested_at": None,
                "deleted_at": None,
                "last_seen_at": now,
                "last_error": None,
                "identity_source": "space_feed" if trusted_identity else None,
                "identity_checked_at": now if trusted_identity else None,
                "identity_ok": True if trusted_identity else None,
                "identity_error": None,
                "updated_at": now,
            }
        )
    if not prepared:
        return 0

    inserted_count = 0
    with session_scope() as session:
        for values in prepared:
            key = (scoped_uid, str(values["repost_dynamic_id"]))
            if session.get(RepostHistoryRow, key) is None:
                inserted_count += 1
            statement = insert(RepostHistoryRow).values(**values)
            excluded = statement.excluded
            result = session.execute(
                statement.on_conflict_do_update(
                    index_elements=["uid", "repost_dynamic_id"],
                    set_={
                        "original_author_uid": case(
                            (excluded.original_author_uid.is_not(None), excluded.original_author_uid),
                            else_=RepostHistoryRow.original_author_uid,
                        ),
                        "original_author_name": case(
                            (
                                excluded.original_author_name.is_not(None),
                                excluded.original_author_name,
                            ),
                            else_=RepostHistoryRow.original_author_name,
                        ),
                        "source": case(
                            (RepostHistoryRow.source == "binggo", RepostHistoryRow.source),
                            else_=excluded.source,
                        ),
                        "last_seen_at": case(
                            (
                                or_(
                                    RepostHistoryRow.last_seen_at.is_(None),
                                    RepostHistoryRow.last_seen_at < now,
                                ),
                                now,
                            ),
                            else_=RepostHistoryRow.last_seen_at,
                        ),
                        "updated_at": now,
                    },
                    where=(
                        (RepostHistoryRow.original_dynamic_id == excluded.original_dynamic_id)
                        & (RepostHistoryRow.reposted_at == excluded.reposted_at)
                    ),
                )
            )
            if result.rowcount != 1:
                raise RuntimeError(
                    f"转发动态 {values['repost_dynamic_id']} 的原动态或发布时间发生冲突，已停止导入"
                )
    return inserted_count


def get_repost(uid: str, repost_id: str) -> RepostHistoryRecord | None:
    key = (_uid(uid), _dynamic_id(repost_id, label="转发动态 ID"))
    with session_scope() as session:
        row = session.get(RepostHistoryRow, key)
        return _history_record(row) if row is not None else None


def list_repost_history(
    uid: str,
    *,
    page: int = 1,
    page_size: int = 50,
    status: str | None = None,
) -> tuple[list[RepostHistoryRecord], int]:
    scoped_uid = _uid(uid)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError("页码无效")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 200:
        raise ValueError("每页数量无效")
    if status is not None and status not in DELETE_STATUSES:
        raise ValueError("删除状态无效")
    filters = [RepostHistoryRow.uid == scoped_uid]
    if status is not None:
        filters.append(RepostHistoryRow.delete_status == status)
    with session_scope() as session:
        total = int(
            session.exec(
                select(func.count()).select_from(RepostHistoryRow).where(*filters)
            ).one()
        )
        rows = session.exec(
            select(RepostHistoryRow)
            .where(*filters)
            .order_by(
                RepostHistoryRow.reposted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        return [_history_record(row) for row in rows], total


def list_active_reposts(uid: str) -> list[RepostHistoryRecord]:
    scoped_uid = _uid(uid)
    with session_scope() as session:
        rows = session.exec(
            select(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.delete_status == "active",
            )
            .order_by(
                RepostHistoryRow.reposted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
        ).all()
        return [_history_record(row) for row in rows]


def get_checkpoint(uid: str) -> RepostSyncCheckpoint | None:
    scoped_uid = _uid(uid)
    with session_scope() as session:
        row = session.get(RepostSyncCheckpointRow, scoped_uid)
        return _checkpoint(row) if row is not None else None


def save_checkpoint(
    uid: str,
    *,
    head_dynamic_id: str | None,
    head_published_at: int | None,
    full_scan_completed: bool,
    last_synced_at: int | None = None,
) -> RepostSyncCheckpoint:
    scoped_uid = _uid(uid)
    if not isinstance(full_scan_completed, bool):
        raise ValueError("完整扫描状态无效")
    if (head_dynamic_id is None) != (head_published_at is None):
        raise ValueError("扫描锚点 ID 与时间必须同时存在或同时为空")
    normalized_head = (
        _dynamic_id(head_dynamic_id, label="扫描锚点动态 ID")
        if head_dynamic_id is not None
        else None
    )
    normalized_published = (
        int(_timestamp(head_published_at, label="扫描锚点发布时间"))
        if head_published_at is not None
        else None
    )
    synced_at = int(time.time()) if last_synced_at is None else int(
        _timestamp(last_synced_at, label="同步时间")
    )
    values = {
        "uid": scoped_uid,
        "head_dynamic_id": normalized_head,
        "head_published_at": normalized_published,
        "full_scan_completed": full_scan_completed,
        "last_synced_at": synced_at,
        "updated_at": synced_at,
    }
    with session_scope() as session:
        session.execute(
            insert(RepostSyncCheckpointRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["uid"],
                set_={key: value for key, value in values.items() if key != "uid"},
            )
        )
    checkpoint = get_checkpoint(scoped_uid)
    if checkpoint is None:
        raise RuntimeError("转发历史扫描锚点保存结果无法确认")
    return checkpoint


def claim_delete_pending(
    uid: str,
    repost_id: str,
    *,
    requested_at: int | None = None,
) -> bool:
    """仅 active/delete_failed 可原子进入 pending；并发请求只会有一个成功。"""
    scoped_uid = _uid(uid)
    normalized_id = _dynamic_id(repost_id, label="转发动态 ID")
    now = int(time.time()) if requested_at is None else int(
        _timestamp(requested_at, label="删除请求时间")
    )
    with session_scope() as session:
        result = session.execute(
            update(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.repost_dynamic_id == normalized_id,
                RepostHistoryRow.delete_status.in_(DELETE_CLAIMABLE_STATUSES),
            )
            .values(
                delete_status="delete_pending",
                delete_requested_at=now,
                deleted_at=None,
                last_error=None,
                updated_at=now,
            )
        )
        return result.rowcount == 1


def mark_delete_result(
    uid: str,
    repost_id: str,
    *,
    status: str,
    error: str | None = None,
    deleted_at: int | None = None,
    updated_at: int | None = None,
) -> bool:
    """只允许已持久化 pending 的记录接受一次明确或不确定结果。"""
    if status not in DELETE_RESULT_STATUSES:
        raise ValueError("删除结果状态无效")
    scoped_uid = _uid(uid)
    normalized_id = _dynamic_id(repost_id, label="转发动态 ID")
    now = int(time.time()) if updated_at is None else int(
        _timestamp(updated_at, label="删除结果时间")
    )
    if status == "deleted":
        finished_at = now if deleted_at is None else int(
            _timestamp(deleted_at, label="删除完成时间")
        )
        normalized_error = None
    else:
        if deleted_at is not None:
            raise ValueError("未确认删除时不能记录删除完成时间")
        finished_at = None
        normalized_error = _optional_text(error)
    with session_scope() as session:
        result = session.execute(
            update(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.repost_dynamic_id == normalized_id,
                RepostHistoryRow.delete_status == "delete_pending",
            )
            .values(
                delete_status=status,
                deleted_at=finished_at,
                last_error=normalized_error,
                updated_at=now,
            )
        )
        return result.rowcount == 1


def reconcile_last_seen(
    uid: str,
    repost_ids: Iterable[str],
    *,
    seen_at: int | None = None,
) -> int:
    """只记录本轮确实看见的 repost；不凭 feed 缺席推断已删除或重置 unknown。"""
    scoped_uid = _uid(uid)
    normalized_ids = {
        _dynamic_id(repost_id, label="转发动态 ID") for repost_id in repost_ids
    }
    if not normalized_ids:
        return 0
    now = int(time.time()) if seen_at is None else int(_timestamp(seen_at, label="扫描时间"))
    changed = 0
    with session_scope() as session:
        for start in range(0, len(normalized_ids), 400):
            chunk = list(normalized_ids)[start : start + 400]
            result = session.execute(
                update(RepostHistoryRow)
                .where(
                    RepostHistoryRow.uid == scoped_uid,
                    RepostHistoryRow.repost_dynamic_id.in_(chunk),
                )
                .values(last_seen_at=now, updated_at=now)
            )
            changed += int(result.rowcount or 0)
    return changed


def _assessment_record(row: RepostAssessmentRow) -> RepostAssessmentRecord:
    return RepostAssessmentRecord(
        uid=row.uid,
        original_dynamic_id=row.original_dynamic_id,
        assessment_level=row.assessment_level,
        assessment_status=row.assessment_status,
        lottery_time_reliable=row.lottery_time_reliable,
        reason_code=row.reason_code,
        lottery_type=row.lottery_type,
        lottery_time=row.lottery_time,
        eligible_after=row.eligible_after,
        reason=row.reason,
        classification_source=row.classification_source,
        summary=row.summary,
        evaluated_at=row.evaluated_at,
        remote_checked_at=row.remote_checked_at,
        assessed_at=row.assessed_at,
        updated_at=row.updated_at,
    )


def upsert_repost_assessment(
    uid: str,
    original_dynamic_id: str,
    *,
    assessment_level: str,
    assessment_status: str = "final",
    lottery_time_reliable: bool = False,
    reason_code: str | None = None,
    lottery_type: str | None = None,
    lottery_time: int | None = None,
    eligible_after: int | None = None,
    reason: str | None = None,
    classification_source: str = "activities",
    summary: str | None = None,
    evaluated_at: int | None = None,
    remote_checked_at: int | None = None,
    assessed_at: int | None = None,
) -> None:
    """保存原动态最近一次清理评估结果（safe/manual_review/blocked/excluded）。"""
    if assessment_level not in ASSESSMENT_LEVELS:
        raise ValueError("评估等级无效")
    if assessment_status not in ASSESSMENT_STATUSES:
        raise ValueError("评估生命周期状态无效")
    if not isinstance(lottery_time_reliable, bool):
        raise ValueError("开奖时间可靠性标记无效")
    if classification_source not in CLASSIFICATION_SOURCES:
        raise ValueError("评估来源无效")
    scoped_uid = _uid(uid)
    original_id = _dynamic_id(original_dynamic_id, label="原动态 ID")
    now = int(time.time()) if assessed_at is None else int(
        _timestamp(assessed_at, label="评估时间")
    )
    values = {
        "uid": scoped_uid,
        "original_dynamic_id": original_id,
        "assessment_level": assessment_level,
        "assessment_status": assessment_status,
        "lottery_time_reliable": lottery_time_reliable,
        "reason_code": _optional_text(reason_code, max_length=32),
        "lottery_type": _optional_text(lottery_type, max_length=16),
        "lottery_time": (
            int(_timestamp(lottery_time, label="开奖时间"))
            if lottery_time is not None
            else None
        ),
        "eligible_after": (
            int(_timestamp(eligible_after, label="可清理时间"))
            if eligible_after is not None
            else None
        ),
        "reason": _optional_text(reason, max_length=256),
        "classification_source": classification_source,
        "summary": _optional_text(summary, max_length=512),
        "evaluated_at": (
            int(_timestamp(evaluated_at, label="评估完成时间"))
            if evaluated_at is not None
            else now
        ),
        "remote_checked_at": (
            int(_timestamp(remote_checked_at, label="远程确认时间"))
            if remote_checked_at is not None
            else None
        ),
        "assessed_at": now,
        "updated_at": now,
    }
    with session_scope() as session:
        session.execute(
            insert(RepostAssessmentRow)
            .values(**values)
            .on_conflict_do_update(
                index_elements=["uid", "original_dynamic_id"],
                set_={
                    key: value
                    for key, value in values.items()
                    if key not in {"uid", "original_dynamic_id"}
                },
            )
        )


def set_repost_identity(
    uid: str,
    repost_id: str,
    *,
    ok: bool,
    source: str,
    error: str | None = None,
    checked_at: int | None = None,
) -> None:
    """记录逐条转发动态的身份/关系验证结果。"""
    if not isinstance(ok, bool):
        raise ValueError("身份验证结果无效")
    if source not in IDENTITY_SOURCES:
        raise ValueError("身份验证来源无效")
    scoped_uid = _uid(uid)
    normalized_id = _dynamic_id(repost_id, label="转发动态 ID")
    now = int(time.time()) if checked_at is None else int(
        _timestamp(checked_at, label="身份验证时间")
    )
    with session_scope() as session:
        session.execute(
            update(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.repost_dynamic_id == normalized_id,
            )
            .values(
                identity_ok=ok,
                identity_source=source,
                identity_checked_at=now,
                identity_error=_optional_text(error, max_length=512),
                updated_at=now,
            )
        )


def defer_repost(
    uid: str,
    repost_id: str,
    *,
    reason: str = "user",
    deferred_at: int | None = None,
    deferred_until: int | None = None,
) -> None:
    """用户主动“暂不删除”：per-repost 持久化，不触碰 assessment/original。"""
    if reason != "user":
        raise ValueError("暂缓原因无效")
    scoped_uid = _uid(uid)
    normalized_id = _dynamic_id(repost_id, label="转发动态 ID")
    now = int(time.time()) if deferred_at is None else int(
        _timestamp(deferred_at, label="暂缓时间")
    )
    until = (
        int(_timestamp(deferred_until, label="暂缓截止时间"))
        if deferred_until is not None
        else None
    )
    with session_scope() as session:
        row = session.get(RepostHistoryRow, (scoped_uid, normalized_id))
        if row is None or row.delete_status not in {"active", "delete_failed"}:
            raise ValueError("只有仍存在的候选转发才能暂不删除")
        row.cleanup_defer_reason = reason
        row.cleanup_deferred_at = now
        row.cleanup_deferred_until = until
        row.updated_at = now


def restore_repost(uid: str, repost_id: str) -> None:
    """恢复用户暂缓的转发；不请求远程，仅清除 per-repost 暂缓标记。"""
    scoped_uid = _uid(uid)
    normalized_id = _dynamic_id(repost_id, label="转发动态 ID")
    now = int(time.time())
    with session_scope() as session:
        row = session.get(RepostHistoryRow, (scoped_uid, normalized_id))
        if row is None:
            raise ValueError("转发历史中不存在该目标")
        row.cleanup_defer_reason = None
        row.cleanup_deferred_at = None
        row.cleanup_deferred_until = None
        row.updated_at = now


def remove_repost_assessment(uid: str, original_dynamic_id: str) -> None:
    """清除某原动态的旧评估，避免把不再安全的内容继续当作候选。"""
    scoped_uid = _uid(uid)
    original_id = _dynamic_id(original_dynamic_id, label="原动态 ID")
    with session_scope() as session:
        session.execute(
            delete(RepostAssessmentRow).where(
                RepostAssessmentRow.uid == scoped_uid,
                RepostAssessmentRow.original_dynamic_id == original_id,
            )
        )


def list_repost_assessments(uid: str) -> dict[str, RepostAssessmentRecord]:
    scoped_uid = _uid(uid)
    with session_scope() as session:
        rows = session.exec(
            select(RepostAssessmentRow).where(RepostAssessmentRow.uid == scoped_uid)
        ).all()
        return {
            row.original_dynamic_id: _assessment_record(row) for row in rows
        }


def list_reposts_by_originals(
    uid: str,
    original_ids: Iterable[str],
) -> list[RepostHistoryRecord]:
    """读取指向指定原动态的转发；只保留可展示/可重新申领的状态。"""
    scoped_uid = _uid(uid)
    normalized = {
        _dynamic_id(original_id, label="原动态 ID") for original_id in original_ids
    }
    if not normalized:
        return []
    with session_scope() as session:
        rows = session.exec(
            select(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.original_dynamic_id.in_(sorted(normalized)),
                RepostHistoryRow.delete_status.in_(
                    {"active", "delete_failed", "unknown"}
                ),
            )
            .order_by(
                RepostHistoryRow.reposted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
        ).all()
        return [_history_record(row) for row in rows]


def list_evaluable_reposts(uid: str) -> list[RepostHistoryRecord]:
    """删除仍可能被评估的转发（active/delete_failed/unknown）。"""
    scoped_uid = _uid(uid)
    with session_scope() as session:
        rows = session.exec(
            select(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.delete_status.in_(
                    {"active", "delete_failed", "unknown"}
                ),
            )
            .order_by(
                RepostHistoryRow.reposted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
        ).all()
        return [_history_record(row) for row in rows]


def list_reposts_needing_identity(uid: str) -> list[RepostHistoryRecord]:
    """只有身份证据缺失或冲突的转发才需要远程 detail 重新验证。"""
    scoped_uid = _uid(uid)
    with session_scope() as session:
        rows = session.exec(
            select(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.delete_status.in_(
                    {"active", "delete_failed", "unknown"}
                ),
                or_(
                    RepostHistoryRow.identity_ok.is_(None),
                    RepostHistoryRow.identity_ok.is_(False),
                    RepostHistoryRow.identity_source.is_(None),
                ),
            )
            .order_by(
                RepostHistoryRow.reposted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
        ).all()
        return [_history_record(row) for row in rows]


def list_deleted_reposts(uid: str) -> list[RepostHistoryRecord]:
    """已删除 tombstone：保留审计信息，绝不物理删除。"""
    scoped_uid = _uid(uid)
    with session_scope() as session:
        rows = session.exec(
            select(RepostHistoryRow)
            .where(
                RepostHistoryRow.uid == scoped_uid,
                RepostHistoryRow.delete_status == "deleted",
            )
            .order_by(
                RepostHistoryRow.deleted_at.desc(),
                RepostHistoryRow.repost_dynamic_id.desc(),
            )
        ).all()
        return [_history_record(row) for row in rows]
