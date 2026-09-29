from __future__ import annotations

import time
from typing import Any

from sqlmodel import select

from src.db.models import ActivityFailureRow
from src.db.session import session_scope
from src.sources.common import opus_link


def record_activity_failure(
    dynamic_id: str,
    *,
    source_url: str | None = None,
    reason: str,
    source_id: str | None = None,
    failed_at: int | None = None,
) -> None:
    """Upsert one terminal item-level failure in the current Profile database."""
    did = str(dynamic_id or "").strip()
    if not did:
        return
    now = int(failed_at or time.time())
    url = str(source_url or "").strip() or opus_link(did)
    with session_scope() as session:
        row = session.get(ActivityFailureRow, did)
        if row is None:
            session.add(
                ActivityFailureRow(
                    dynamic_id=did,
                    source_url=url,
                    reason=str(reason or "处理失败").strip() or "处理失败",
                    source_id=str(source_id or "").strip() or None,
                    failed_at=now,
                )
            )
            return
        row.source_url = url
        row.reason = str(reason or "处理失败").strip() or "处理失败"
        row.source_id = str(source_id or "").strip() or row.source_id
        row.failed_at = now


def list_activity_failures(*, query: str | None = None) -> list[dict[str, Any]]:
    keyword = str(query or "").strip().lower()
    result: list[dict[str, Any]] = []
    with session_scope() as session:
        rows = session.exec(
            select(ActivityFailureRow).order_by(ActivityFailureRow.failed_at.desc())
        ).all()
        for row in rows:
            payload = {
                "dynamic_id": row.dynamic_id,
                "source_url": row.source_url or opus_link(row.dynamic_id),
                "reason": row.reason,
                "source_id": row.source_id,
                "failed_at": row.failed_at,
            }
            if keyword and not any(keyword in str(payload.get(key) or "").lower() for key in payload):
                continue
            result.append(payload)
    return result


def delete_activity_failure(dynamic_id: str) -> bool:
    did = str(dynamic_id or "").strip()
    if not did:
        return False
    with session_scope() as session:
        row = session.get(ActivityFailureRow, did)
        if row is None:
            return False
        session.delete(row)
        return True


def resolve_activity_failure(dynamic_id: str) -> bool:
    """Remove stale failure evidence once the same item is successfully persisted."""
    return delete_activity_failure(dynamic_id)
