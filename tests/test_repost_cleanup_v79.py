from __future__ import annotations

import time

from src.repost_cleanup import CandidateAssessment, load_persisted_candidates, scan_expired_reposts
from src.repost_history import RepostImportRecord, list_repost_assessments, upsert_repost_assessment, upsert_repost_records


UID = "12345"
REPOST_ID = "2000000000000000001"
ORIGINAL_ID = "1000000000000000001"
RECHECK = 30 * 24 * 60 * 60


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _seed() -> None:
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=REPOST_ID,
                original_dynamic_id=ORIGINAL_ID,
                reposted_at=100,
                source="history_import",
            )
        ],
        seen_at=100,
    )


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._verify_owned_repost_detail",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])


def test_manual_review_reopens_after_reliable_lottery_recheck_time(isolated_home, monkeypatch) -> None:
    _seed()
    _login(monkeypatch)
    calls: list[int] = []

    now = int(time.time())
    lottery_time = now - 1_000

    def assess(*args, **kwargs):
        calls.append(kwargs["now_ts"])
        return CandidateAssessment(
            "manual_review",
            "中奖名单暂不完整",
            "notice_winners_incomplete",
            "互动抽奖",
            lottery_time,
            lottery_time_reliable=True,
            classification_source="public_classifier",
        )

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    first = scan_expired_reposts(now_ts=now, client_factory=lambda: _FakeClient())
    stored = list_repost_assessments(UID)[ORIGINAL_ID]
    assert first["evaluated_originals"] == 1
    assert stored.assessment_status == "refreshable"
    assert stored.eligible_after == lottery_time + RECHECK
    assert load_persisted_candidates(UID)[0]["level"] == "deferred"

    before_due = scan_expired_reposts(now_ts=lottery_time + RECHECK - 1, client_factory=lambda: _FakeClient())
    assert before_due["evaluated_originals"] == 0
    assert len(calls) == 1

    after_due = scan_expired_reposts(now_ts=lottery_time + RECHECK, client_factory=lambda: _FakeClient())
    assert after_due["evaluated_originals"] == 1
    assert len(calls) == 2


def test_manual_review_without_reliable_time_uses_existing_defer_slot(isolated_home, monkeypatch) -> None:
    _seed()
    _login(monkeypatch)
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: CandidateAssessment(
            "manual_review",
            "开奖时间未知",
            "notice_time_missing",
            "互动抽奖",
            lottery_time_reliable=False,
            classification_source="public_classifier",
        ),
    )
    now = int(time.time())
    first = scan_expired_reposts(now_ts=now, client_factory=lambda: _FakeClient())
    stored = list_repost_assessments(UID)[ORIGINAL_ID]
    assert first["evaluated_originals"] == 1
    assert stored.assessment_status == "refreshable"
    assert stored.lottery_time is None
    assert stored.eligible_after == now + RECHECK
    assert load_persisted_candidates(UID)[0]["level"] == "deferred"


def test_legacy_manual_final_is_migrated_without_losing_reason_or_source(isolated_home, monkeypatch) -> None:
    _seed()
    _login(monkeypatch)
    now = int(time.time())
    upsert_repost_assessment(
        UID,
        ORIGINAL_ID,
        assessment_level="manual_review",
        assessment_status="final",
        reason_code="forward_lottery_manual",
        reason="用户已人工确认，请后续重新检查",
        classification_source="activities",
        evaluated_at=now,
        assessed_at=now,
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("未到重新评估时间")),
    )

    result = scan_expired_reposts(now_ts=now + RECHECK - 1, client_factory=lambda: _FakeClient())
    stored = list_repost_assessments(UID)[ORIGINAL_ID]
    assert result["evaluated_originals"] == 0
    assert stored.assessment_status == "refreshable"
    assert stored.reason_code == "forward_lottery_manual"
    assert stored.reason == "用户已人工确认，请后续重新检查"
    assert stored.classification_source == "activities"
