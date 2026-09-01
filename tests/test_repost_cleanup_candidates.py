from __future__ import annotations

from src.repost_cleanup import (
    CandidateAssessment,
    load_persisted_candidates,
    repost_cleanup_summary,
    scan_expired_reposts,
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


def _import_record(
    repost_id: str = REPOST_ID,
    original_id: str = ORIGINAL_ID,
) -> RepostImportRecord:
    return RepostImportRecord(
        repost_dynamic_id=repost_id,
        original_dynamic_id=original_id,
        reposted_at=100,
        original_author_uid="98765",
        original_author_name="原作者",
        source="history_import",
    )


def _assessment(
    uid: str = UID,
    original_id: str = ORIGINAL_ID,
) -> None:
    upsert_repost_assessment(
        uid,
        original_id,
        assessment_level="safe",
        reason_code="safe_official_notice",
        lottery_type="互动抽奖",
        lottery_time=1000,
        eligible_after=1000 + 3 * 24 * 60 * 60,
        reason="已开奖且当前账号未中奖",
        classification_source="activities",
        evaluated_at=5000,
        assessed_at=5000,
    )


def test_persisted_candidates_reconstruct_from_local_assessment(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=200)
    upsert_repost_records(
        UID,
        [_import_record("2000000000000000002", "1000000000000000002")],
        seen_at=200,
    )
    _assessment()

    candidates = load_persisted_candidates(UID)

    assert [c["repost_dynamic_id"] for c in candidates] == [REPOST_ID]
    assert candidates[0]["original_dynamic_id"] == ORIGINAL_ID
    assert candidates[0]["delete_status"] == "active"
    assert candidates[0]["level"] == "safe"
    assert candidates[0]["lottery_type"] == "互动抽奖"
    assert candidates[0]["reason"] == "已开奖且当前账号未中奖"


def test_persisted_candidates_are_uid_scoped(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=200)
    _assessment()

    assert load_persisted_candidates(UID)
    assert load_persisted_candidates("54321") == []


def test_persisted_candidates_respect_delete_status(isolated_home) -> None:
    upsert_repost_records(
        UID,
        [
            _import_record("2000000000000000001", ORIGINAL_ID),
            _import_record("2000000000000000002", ORIGINAL_ID),
            _import_record("2000000000000000003", ORIGINAL_ID),
        ],
        seen_at=200,
    )
    _assessment()

    assert claim_delete_pending(UID, "2000000000000000001", requested_at=300)
    assert mark_delete_result(UID, "2000000000000000001", status="deleted", updated_at=301)
    assert claim_delete_pending(UID, "2000000000000000002", requested_at=300)
    assert mark_delete_result(UID, "2000000000000000002", status="delete_failed", error="x", updated_at=301)
    assert claim_delete_pending(UID, "2000000000000000003", requested_at=300)
    assert mark_delete_result(UID, "2000000000000000003", status="unknown", error="y", updated_at=301)

    candidates = {
        c["repost_dynamic_id"]: c["delete_status"]
        for c in load_persisted_candidates(UID)
    }

    assert "2000000000000000001" not in candidates
    assert candidates.get("2000000000000000002") == "delete_failed"
    assert candidates.get("2000000000000000003") == "unknown"


def test_scan_expired_reposts_persists_eligible_assessment(isolated_home, monkeypatch) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=200)
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: CandidateAssessment(
            "safe", "ok", "safe_official_notice", "互动抽奖", 1000, 1000,
            classification_source="public_classifier",
        ),
    )

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    result = scan_expired_reposts(client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 1
    assert result["pending_originals"] == 0
    persisted = load_persisted_candidates(UID)
    assert [c["repost_dynamic_id"] for c in persisted] == [REPOST_ID]
    assert persisted[0]["level"] == "safe"


def test_summary_counts_levels_and_pending(isolated_home) -> None:
    upsert_repost_records(
        UID,
        [
            _import_record("2000000000000000001", ORIGINAL_ID),
            _import_record("2000000000000000002", "1000000000000000002"),
        ],
        seen_at=200,
    )
    _assessment()

    summary = repost_cleanup_summary(UID)

    assert summary["history_total"] == 2
    assert summary["safe"] == 1
    assert summary["pending_evaluation"] == 1
    assert summary["manual_review"] == 0
    assert summary["blocked"] == 0
