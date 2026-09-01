from __future__ import annotations

import time

import pytest

from src.repost_cleanup import (
    defer_candidate,
    load_persisted_candidates,
    repost_cleanup_summary,
    restore_candidate,
)
from src.repost_history import (
    RepostImportRecord,
    claim_delete_pending,
    mark_delete_result,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"
REPOST_ID = "2000000000000000001"
ORIGINAL_ID = "1000000000000000001"


def _seed_repost(uid: str = UID, repost_id: str = REPOST_ID) -> None:
    upsert_repost_records(
        uid,
        [
            RepostImportRecord(
                repost_dynamic_id=repost_id,
                original_dynamic_id=ORIGINAL_ID,
                reposted_at=100,
                source="history_import",
            )
        ],
        seen_at=100,
    )


def _manual_assessment(
    *,
    lottery_time: int | None,
    lottery_time_reliable: bool,
    reason_code: str = "notice_winners_incomplete",
) -> None:
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="manual_review",
        assessment_status="final",
        lottery_time_reliable=lottery_time_reliable,
        reason_code=reason_code,
        lottery_time=lottery_time,
        assessed_at=100,
    )


def _level(uid: str = UID) -> str:
    return load_persisted_candidates(uid)[0]["level"]


def test_manual_review_reliable_time_under_30_days_is_deferred(isolated_home) -> None:
    _seed_repost()
    _manual_assessment(
        lottery_time=int(time.time()) - 10 * 86400,
        lottery_time_reliable=True,
    )

    candidate = load_persisted_candidates(UID)[0]
    assert candidate["level"] == "deferred"
    assert candidate["defer_reason"] == "recent_reliable_lottery"
    assert candidate["reason_code"] == "recent_reliable_lottery"
    assert repost_cleanup_summary(UID)["deferred"] == 1


def test_manual_review_reliable_time_over_30_days_stays_manual(isolated_home) -> None:
    _seed_repost()
    _manual_assessment(
        lottery_time=int(time.time()) - 40 * 86400,
        lottery_time_reliable=True,
    )

    assert _level() == "manual_review"
    assert repost_cleanup_summary(UID)["manual_review"] == 1


@pytest.mark.parametrize(
    "lottery_time, reliable",
    [
        (None, False),
        (None, True),
        (int(time.time()) - 10 * 86400, False),
    ],
)
def test_unreliable_or_missing_time_never_deferred(isolated_home, lottery_time, reliable) -> None:
    _seed_repost()
    _manual_assessment(lottery_time=lottery_time, lottery_time_reliable=reliable)

    assert _level() == "manual_review"
    assert repost_cleanup_summary(UID)["deferred"] == 0


def test_safe_not_affected_by_30_day_rule(isolated_home) -> None:
    _seed_repost()
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="safe",
        assessment_status="final",
        lottery_time_reliable=True,
        reason_code="safe_official_notice",
        lottery_time=int(time.time()) - 1 * 86400,
        assessed_at=100,
    )

    assert _level() == "safe"


def test_user_defer_safe_then_restore(isolated_home) -> None:
    _seed_repost()
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="safe",
        assessment_status="final",
        lottery_time_reliable=True,
        reason_code="safe_official_notice",
        lottery_time=int(time.time()) - 10 * 86400,
        assessed_at=100,
    )

    defer_candidate(UID, REPOST_ID)
    candidate = load_persisted_candidates(UID)[0]
    assert candidate["level"] == "deferred"
    assert candidate["defer_reason"] == "user"

    restore_candidate(UID, REPOST_ID)
    assert _level() == "safe"


def test_user_defer_manual_restore_reapplies_30_day_rule(isolated_home) -> None:
    _seed_repost()
    _manual_assessment(
        lottery_time=int(time.time()) - 10 * 86400,
        lottery_time_reliable=True,
    )
    defer_candidate(UID, REPOST_ID)
    assert load_persisted_candidates(UID)[0]["defer_reason"] == "user"

    restore_candidate(UID, REPOST_ID)
    candidate = load_persisted_candidates(UID)[0]
    assert candidate["level"] == "deferred"
    assert candidate["defer_reason"] == "recent_reliable_lottery"


def test_user_defer_manual_unreliable_restore_returns_manual(isolated_home) -> None:
    _seed_repost()
    _manual_assessment(lottery_time=None, lottery_time_reliable=False)
    defer_candidate(UID, REPOST_ID)
    restore_candidate(UID, REPOST_ID)

    assert _level() == "manual_review"


def test_user_defer_is_profile_scoped(isolated_home) -> None:
    _seed_repost()
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="safe",
        assessment_status="final",
        lottery_time_reliable=True,
        reason_code="safe_official_notice",
        assessed_at=100,
    )
    defer_candidate(UID, REPOST_ID)

    assert load_persisted_candidates(UID)[0]["level"] == "deferred"
    assert load_persisted_candidates("54321") == []


def test_deferred_delete_is_rejected(isolated_home, monkeypatch) -> None:
    from src.repost_cleanup import delete_reposts

    _seed_repost()
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="safe",
        assessment_status="final",
        lottery_time_reliable=True,
        reason_code="safe_official_notice",
        assessed_at=100,
    )
    defer_candidate(UID, REPOST_ID)
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assert_frozen_runtime",
        lambda *, profile_id, database_path, uid: "csrf-token",
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    class _FakeDbPath:
        def resolve(self) -> str:
            return "db-1"

    monkeypatch.setattr("src.repost_cleanup.db_path", lambda: _FakeDbPath())
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("src.repost_cleanup.claim_delete_pending", lambda *args, **kwargs: True)
    monkeypatch.setattr("src.repost_cleanup.mark_delete_result", lambda *args, **kwargs: True)

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def post_json(self, *args, **kwargs):
            raise AssertionError("deferred candidate must not reach delete POST")

    result = delete_reposts([REPOST_ID], client_factory=lambda: _FakeClient())

    assert result["skipped_count"] == 1
    assert "已暂缓" in result["items"][0]["message"]


def test_summary_includes_deleted_tombstones_and_checkpoint(isolated_home) -> None:
    _seed_repost()
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="safe",
        assessment_status="final",
        lottery_time_reliable=True,
        reason_code="safe_official_notice",
        lottery_time=int(time.time()) - 10 * 86400,
        assessed_at=100,
        evaluated_at=200,
    )
    claim_delete_pending(UID, REPOST_ID, requested_at=300)
    mark_delete_result(UID, REPOST_ID, status="deleted", deleted_at=301, updated_at=301)

    summary = repost_cleanup_summary(UID, show_deleted=True)

    assert summary["deleted"] == 1
    assert len(summary["deleted_candidates"]) == 1
    assert summary["deleted_candidates"][0]["repost_dynamic_id"] == REPOST_ID
    assert summary["deleted_candidates"][0]["deleted_at"] == 301
    assert summary["deleted_candidates"][0]["level"] == "deleted"
    assert summary["history_total"] == 0
    assert summary["last_evaluated_at"] == 200
    assert "last_synced_at" in summary
    assert "full_scan_completed" in summary
