"""Profile-local bounded audit evidence for DS9 non-lottery classifications."""

from __future__ import annotations

import csv
import hashlib
import io
import time
from datetime import datetime
from typing import Any

from sqlmodel import delete, select

from src.db.models import Ds9RejectionAuditRow
from src.db.session import session_scope

MAX_AUDIT_SAMPLES = 100
AUDIT_CSV_FIELDS = (
    "dynamic_id",
    "topic_id",
    "topic_name",
    "canonical_url",
    "classifier_text",
    "classification_result",
    "classification_reason",
    "first_seen_at",
    "last_seen_at",
)


def _row_payload(row: Ds9RejectionAuditRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "dynamic_id": row.dynamic_id,
        "canonical_url": row.canonical_url,
        "source_topic_id": row.source_topic_id,
        "source_topic_name": row.source_topic_name,
        "classifier_text": row.classifier_text,
        "classification_result": row.classification_result,
        "classification_reason": row.classification_reason,
        "text_hash": row.text_hash,
        "first_seen_at": row.first_seen_at,
        "last_seen_at": row.last_seen_at,
    }


def record_rejection(
    *,
    dynamic_id: str,
    canonical_url: str,
    source_topic_id: str,
    source_topic_name: str,
    classifier_text: str,
    classification_reason: str = "非抽奖活动",
    seen_at: int | None = None,
) -> None:
    """Upsert one non-lottery sample and retain only the newest 100 rows."""
    dynamic_id = str(dynamic_id or "").strip()
    if not dynamic_id:
        return
    text = str(classifier_text or "").strip() or "(无正文)"
    stamp = int(seen_at if seen_at is not None else time.time())
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    with session_scope() as session:
        row = session.exec(
            select(Ds9RejectionAuditRow).where(
                Ds9RejectionAuditRow.dynamic_id == dynamic_id
            )
        ).first()
        if row is None:
            session.add(
                Ds9RejectionAuditRow(
                    dynamic_id=dynamic_id,
                    canonical_url=str(canonical_url or ""),
                    source_topic_id=str(source_topic_id or ""),
                    source_topic_name=str(source_topic_name or ""),
                    classifier_text=text,
                    classification_result="非抽奖活动",
                    classification_reason=str(classification_reason or "非抽奖活动"),
                    text_hash=text_hash,
                    first_seen_at=stamp,
                    last_seen_at=stamp,
                )
            )
        else:
            row.canonical_url = str(canonical_url or row.canonical_url or "")
            row.source_topic_id = str(source_topic_id or row.source_topic_id or "")
            row.source_topic_name = str(source_topic_name or row.source_topic_name or "")
            row.classifier_text = text
            row.classification_result = "非抽奖活动"
            row.classification_reason = str(classification_reason or "非抽奖活动")
            row.text_hash = text_hash
            row.last_seen_at = stamp
        session.flush()
        rows = session.exec(
            select(Ds9RejectionAuditRow).order_by(
                Ds9RejectionAuditRow.last_seen_at.desc(),
                Ds9RejectionAuditRow.id.desc(),
            )
        ).all()
        for stale in rows[MAX_AUDIT_SAMPLES:]:
            session.delete(stale)


def list_rejections(*, source_topic_id: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as session:
        statement = select(Ds9RejectionAuditRow).order_by(
            Ds9RejectionAuditRow.last_seen_at.desc(),
            Ds9RejectionAuditRow.id.desc(),
        )
        if source_topic_id:
            statement = statement.where(
                Ds9RejectionAuditRow.source_topic_id == str(source_topic_id)
            )
        return [_row_payload(row) for row in session.exec(statement).all()]


def clear_rejections() -> int:
    with session_scope() as session:
        rows = session.exec(select(Ds9RejectionAuditRow)).all()
        count = len(rows)
        session.exec(delete(Ds9RejectionAuditRow))
        return count


def export_rejections_csv(
    *,
    source_topic_id: str | None = None,
    now: datetime | None = None,
) -> tuple[str, bytes]:
    """Export the current Profile's bounded audit rows as an Excel-safe CSV."""
    rows = list_rejections(source_topic_id=source_topic_id)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=list(AUDIT_CSV_FIELDS),
        extrasaction="ignore",
        lineterminator="\r\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "dynamic_id": row.get("dynamic_id", ""),
                "topic_id": row.get("source_topic_id", ""),
                "topic_name": row.get("source_topic_name", ""),
                "canonical_url": row.get("canonical_url", ""),
                "classifier_text": row.get("classifier_text", ""),
                "classification_result": row.get("classification_result", ""),
                "classification_reason": row.get("classification_reason", ""),
                "first_seen_at": row.get("first_seen_at", ""),
                "last_seen_at": row.get("last_seen_at", ""),
            }
        )
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    filename = f"binggo-ds9-rejection-audit-{stamp}.csv"
    # UTF-8 BOM keeps Chinese headers and classifier text readable in Windows Excel.
    return filename, ("\ufeff" + output.getvalue()).encode("utf-8")
