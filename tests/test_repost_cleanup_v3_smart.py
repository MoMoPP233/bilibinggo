from __future__ import annotations

import pytest

from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import (
    CandidateAssessment,
    load_persisted_candidates,
    scan_expired_reposts,
)
from src.repost_history import (
    RepostImportRecord,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )


def _seed(originals: list[str], monkeypatch, *, risk_after: int | None = None) -> None:
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=f"200000000000000000{n}",
                original_dynamic_id=original,
                reposted_at=100 + n,
                source="history_import",
            )
            for n, original in enumerate(originals, 1)
        ],
        seen_at=100,
    )
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    calls = {"n": 0}

    def classifier(*args, **kwargs):
        calls["n"] += 1
        if risk_after is not None and calls["n"] > risk_after:
            raise RuntimeError("API error -352: 风控校验失败")
        original_id = args[1]
        return ClassifyOutcome(original_id, "转发抽奖", False), None

    monkeypatch.setattr("src.repost_cleanup.classify_for_cleanup", classifier)
    return calls


def test_one_click_runs_multiple_batches_within_job_budget(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BUDGET_PER_ROUND", 2)
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 5)
    originals = [f"100000000000000000{n}" for n in range(1, 6)]
    _seed(originals, monkeypatch)

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 5
    assert result["pending_originals"] == 0
    assert result["budget"] == 2
    assert result["job_budget"] == 5


def test_job_budget_hard_cap_and_continuation(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BUDGET_PER_ROUND", 2)
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 2)
    originals = [f"100000000000000000{n}" for n in range(1, 6)]
    _seed(originals, monkeypatch)

    first = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())
    assert first["evaluated_originals"] == 2
    assert first["pending_originals"] == 3

    second = scan_expired_reposts(now_ts=1_800_000_001, client_factory=lambda: _FakeClient())
    assert second["evaluated_originals"] == 2
    assert second["pending_originals"] == 1

    third = scan_expired_reposts(now_ts=1_800_000_002, client_factory=lambda: _FakeClient())
    assert third["evaluated_originals"] == 1
    assert third["pending_originals"] == 0


def test_existing_assessment_is_not_reevaluated(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    originals = [f"100000000000000000{n}" for n in range(1, 4)]
    calls = _seed(originals, monkeypatch)
    upsert_repost_assessment(
        UID,
        originals[0],
        assessment_level="manual_review",
        assessment_status="final",
        reason_code="forward_lottery_manual",
        assessed_at=100,
    )

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 2
    assert calls["n"] == 2
    assert result["pending_originals"] == 0


def test_same_original_deduplicated_across_reposts(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    calls = _seed(["1000000000000000001", "1000000000000000001"], monkeypatch)

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 1
    assert calls["n"] == 1


def test_retryable_unknown_is_retried_but_final_is_not(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    originals = [f"100000000000000000{n}" for n in range(1, 3)]
    calls = _seed(originals, monkeypatch)
    upsert_repost_assessment(
        UID,
        originals[0],
        assessment_level="blocked",
        assessment_status="retryable_unknown",
        reason_code="remote_unknown",
        assessed_at=100,
    )

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 2
    assert calls["n"] == 2


def test_permanent_blocked_is_not_reevaluated(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    originals = [f"100000000000000000{n}" for n in range(1, 3)]
    calls = _seed(originals, monkeypatch)
    upsert_repost_assessment(
        UID,
        originals[0],
        assessment_level="blocked",
        assessment_status="final",
        reason_code="original_unreadable",
        assessed_at=100,
    )

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 1
    assert calls["n"] == 1


def test_rate_limit_stops_entire_job_across_batches(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BUDGET_PER_ROUND", 2)
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    originals = [f"100000000000000000{n}" for n in range(1, 6)]
    _seed(originals, monkeypatch, risk_after=1)

    result = scan_expired_reposts(now_ts=1_800_000_000, client_factory=lambda: _FakeClient())

    assert result["rate_limited"] is True
    assert result["evaluated_originals"] == 1
    assert "已经完成的结果已保存" in result["message"]


def test_cancel_preserves_completed_results(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BUDGET_PER_ROUND", 1)
    monkeypatch.setattr("src.repost_cleanup.MAX_ASSESSMENTS_PER_JOB", 10)
    originals = [f"100000000000000000{n}" for n in range(1, 4)]
    _seed(originals, monkeypatch)

    def cancel_check() -> bool:
        # 第一条落库后取消：验证已完成结果被保留。
        return len(load_persisted_candidates(UID)) > 0

    with pytest.raises(RuntimeError, match="任务已取消"):
        scan_expired_reposts(
            now_ts=1_800_000_000,
            client_factory=lambda: _FakeClient(),
            cancel_check=cancel_check,
        )

    persisted = load_persisted_candidates(UID)
    assert len(persisted) == 1
    assert persisted[0]["level"] == "manual_review"
