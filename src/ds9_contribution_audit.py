"""Profile-local, bounded DS9 page contribution observations.

This module is deliberately a side channel.  It never reads Bilibili and its
writer is called defensively by the existing pipeline so audit failures cannot
change activity persistence or source checkpoints.
"""

from __future__ import annotations

import csv
import io
import time
import uuid
from datetime import datetime
from typing import Any, Iterable, Mapping

from sqlmodel import select

from src.db.models import Ds9ContributionCandidateRow, Ds9ContributionPageRow
from src.db.session import session_scope

RETENTION_SECONDS = 7 * 24 * 60 * 60
AUDIT_SOURCE_ID = "DS-9"


def _stamp(value: object, default: int) -> int:
    try:
        return int(value or default)
    except (TypeError, ValueError):
        return default


def record_round(
    *,
    observations: Iterable[Mapping[str, object]],
    page_stats: Iterable[Mapping[str, object]],
    now: int | None = None,
    scan_id: str | None = None,
) -> str:
    """Persist one refresh round and prune rows older than seven days.

    The caller supplies only values already produced by the source/pipeline;
    this function performs no classification and no network activity.
    """
    current = int(now if now is not None else time.time())
    round_id = str(scan_id or f"ds9-{current}-{uuid.uuid4().hex[:10]}")
    cutoff = current - RETENTION_SECONDS
    with session_scope() as session:
        for page in page_stats:
            session.add(
                Ds9ContributionPageRow(
                    scan_id=round_id,
                    source_id=str(page.get("source_id") or AUDIT_SOURCE_ID),
                    topic_id=str(page.get("topic_id") or ""),
                    topic_name=str(page.get("topic_name") or ""),
                    page_index=_stamp(page.get("page_index"), 0),
                    scanned_at=_stamp(page.get("scanned_at"), current),
                    discovered_count=_stamp(page.get("discovered_count"), 0),
                    batch_duplicate_count=_stamp(page.get("batch_duplicate_count"), 0),
                    existing_count=_stamp(page.get("existing_count"), 0),
                    new_candidate_count=_stamp(page.get("new_candidate_count"), 0),
                    valid_lottery_count=_stamp(page.get("valid_lottery_count"), 0),
                    overlap_other_source_count=_stamp(page.get("overlap_other_source_count"), 0),
                    ds9_unique_valid_count=_stamp(page.get("ds9_unique_valid_count"), 0),
                    non_lottery_count=_stamp(page.get("non_lottery_count"), 0),
                    expired_count=_stamp(page.get("expired_count"), 0),
                    failed_count=_stamp(page.get("failed_count"), 0),
                    unprocessed_count=_stamp(page.get("unprocessed_count"), 0),
                )
            )
        for item in observations:
            session.add(
                Ds9ContributionCandidateRow(
                    scan_id=round_id,
                    source_id=str(item.get("source_id") or AUDIT_SOURCE_ID),
                    topic_id=str(item.get("topic_id") or ""),
                    topic_name=str(item.get("topic_name") or ""),
                    page_index=(
                        _stamp(item.get("page_index"), 0)
                        if item.get("page_index") is not None
                        else None
                    ),
                    raw_dynamic_id=str(item.get("raw_dynamic_id") or item.get("dynamic_id") or ""),
                    canonical_dynamic_id=str(item.get("canonical_dynamic_id") or ""),
                    outcome=str(item.get("outcome") or "unprocessed"),
                    valid_lottery=bool(item.get("valid_lottery")),
                    persisted=bool(item.get("persisted")),
                    scanned_at=_stamp(item.get("scanned_at"), current),
                )
            )
        session.flush()
        old_pages = session.exec(
            select(Ds9ContributionPageRow).where(Ds9ContributionPageRow.scanned_at < cutoff)
        ).all()
        old_candidates = session.exec(
            select(Ds9ContributionCandidateRow).where(
                Ds9ContributionCandidateRow.scanned_at < cutoff
            )
        ).all()
        for row in (*old_pages, *old_candidates):
            session.delete(row)
    return round_id


def _candidate_payload(row: Ds9ContributionCandidateRow) -> dict[str, Any]:
    return {
        "scan_id": row.scan_id,
        "source_id": row.source_id,
        "topic_id": row.topic_id,
        "topic_name": row.topic_name,
        "page_index": row.page_index,
        "raw_dynamic_id": row.raw_dynamic_id,
        "canonical_dynamic_id": row.canonical_dynamic_id,
        "outcome": row.outcome,
        "valid_lottery": bool(row.valid_lottery),
        "persisted": bool(row.persisted),
        "scanned_at": row.scanned_at,
    }


def list_page_contributions(*, since: int | None = None) -> list[dict[str, Any]]:
    cutoff = int(since if since is not None else time.time() - RETENTION_SECONDS)
    with session_scope() as session:
        rows = session.exec(
            select(Ds9ContributionPageRow)
            .where(Ds9ContributionPageRow.scanned_at >= cutoff)
            .order_by(Ds9ContributionPageRow.scanned_at.desc(), Ds9ContributionPageRow.id.desc())
        ).all()
        return [row.model_dump() for row in rows]


def list_unique_valid(*, since: int | None = None) -> list[dict[str, Any]]:
    """Return current DS9-only valid activities, removing later overlaps."""
    cutoff = int(since if since is not None else time.time() - RETENTION_SECONDS)
    with session_scope() as session:
        rows = session.exec(
            select(Ds9ContributionCandidateRow)
            .where(
                Ds9ContributionCandidateRow.scanned_at >= cutoff,
                Ds9ContributionCandidateRow.valid_lottery == True,  # noqa: E712
            )
            .order_by(
                Ds9ContributionCandidateRow.scanned_at.asc(),
                Ds9ContributionCandidateRow.id.asc(),
            )
        ).all()
        all_rows = session.exec(
            select(Ds9ContributionCandidateRow).where(
                Ds9ContributionCandidateRow.scanned_at >= cutoff
            )
        ).all()
        other_ids = {
            row.canonical_dynamic_id
            for row in all_rows
            if row.source_id != AUDIT_SOURCE_ID and row.canonical_dynamic_id
        }
        seen: set[str] = set()
        output: list[dict[str, Any]] = []
        for row in rows:
            canonical = row.canonical_dynamic_id or row.raw_dynamic_id
            if row.source_id != AUDIT_SOURCE_ID or not canonical:
                continue
            if canonical in other_ids or canonical in seen:
                continue
            seen.add(canonical)
            payload = _candidate_payload(row)
            payload.update(
                {
                    "dynamic_id": canonical,
                    "canonical_url": f"https://www.bilibili.com/opus/{canonical}",
                    "label": "DS9 独有",
                }
            )
            output.append(payload)
        return output


CONTRIBUTION_CSV_FIELDS = (
    "scan_id", "scanned_at", "source_id", "topic_id", "topic_name", "page_index",
    "discovered_count", "batch_duplicate_count", "existing_count", "new_candidate_count",
    "valid_lottery_count", "overlap_other_source_count", "ds9_unique_valid_count",
    "non_lottery_count", "expired_count", "failed_count", "unprocessed_count",
)


def export_contributions_csv(*, now: datetime | None = None) -> tuple[str, bytes]:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(CONTRIBUTION_CSV_FIELDS), lineterminator="\r\n")
    writer.writeheader()
    for row in list_page_contributions():
        writer.writerow({key: row.get(key, "") for key in CONTRIBUTION_CSV_FIELDS})
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M")
    filename = f"binggo-ds9-page-contribution-{stamp}.csv"
    return filename, ("\ufeff" + output.getvalue()).encode("utf-8")
