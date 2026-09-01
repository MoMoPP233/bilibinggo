from __future__ import annotations

import pytest

from src.pipeline.classify_step import ClassifyOutcome
from src.repost_cleanup import (
    ASSESSMENT_BUDGET_PER_ROUND,
    load_persisted_candidates,
    repost_cleanup_summary,
    scan_expired_reposts,
)
from src.repost_history import (
    RepostImportRecord,
    set_repost_identity,
    upsert_repost_assessment,
    upsert_repost_records,
)

UID = "12345"
ORIGINAL_ID = "1000000000000000001"
REPOST_ID = "2000000000000000001"
NOW_TS = 1_800_000_000
LOTTERY_TS = NOW_TS - 10 * 24 * 60 * 60


def _import(original_id: str = ORIGINAL_ID, repost_id: str = REPOST_ID) -> RepostImportRecord:
    return RepostImportRecord(
        repost_dynamic_id=repost_id,
        original_dynamic_id=original_id,
        reposted_at=100,
        source="history_import",
    )


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


def _notice_ok(*, lottery_time: int = LOTTERY_TS) -> dict:
    return {
        "lottery_id": "1",
        "status": 1,
        "lottery_time": lottery_time,
        "first_prize": 1,
        "second_prize": 0,
        "third_prize": 0,
        "lottery_result": {
            "first_prize_result": [{"uid": 99999}],
            "second_prize_result": [],
            "third_prize_result": [],
        },
    }


def test_activities_reuse_yields_safe(isolated_home, monkeypatch) -> None:
    upsert_repost_records(UID, [_import()], seen_at=100)
    _login(monkeypatch)
    monkeypatch.setattr(
        "src.repost_cleanup.load_activities",
        lambda: [
            {
                "dynamic_id": ORIGINAL_ID,
                "lottery_type": "互动抽奖",
                "status_classified": True,
                "business_id": ORIGINAL_ID,
                "business_type": 1,
            }
        ],
    )
    monkeypatch.setattr(
        "src.repost_cleanup._fetch_dynamic_item_strict",
        lambda *args, **kwargs: {"type": "DYNAMIC_TYPE_NOTE"},
    )
    monkeypatch.setattr(
        "src.repost_cleanup._fetch_notice_strict",
        lambda *args, **kwargs: _notice_ok(),
    )

    result = scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())

    assert result["evaluated_originals"] == 1
    assert load_persisted_candidates(UID)[0]["level"] == "safe"


def test_public_classifier_forward_lottery_is_manual_review(isolated_home, monkeypatch) -> None:
    upsert_repost_records(UID, [_import()], seen_at=100)
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr(
        "src.repost_cleanup.classify_for_cleanup",
        lambda *args, **kwargs: (
            ClassifyOutcome(ORIGINAL_ID, "转发抽奖", False, classify_content="转发抽奖文案"),
            None,
        ),
    )

    result = scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())

    candidate = load_persisted_candidates(UID)[0]
    assert candidate["level"] == "manual_review"
    assert candidate["reason_code"] == "forward_lottery_manual"
    assert result["message"].startswith("历史抽奖评估完成")


def test_non_lottery_is_excluded_not_blocked(isolated_home, monkeypatch) -> None:
    upsert_repost_records(UID, [_import()], seen_at=100)
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr(
        "src.repost_cleanup.classify_for_cleanup",
        lambda *args, **kwargs: (
            ClassifyOutcome(ORIGINAL_ID, "非抽奖活动", True, "非抽奖活动"),
            None,
        ),
    )

    scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())

    summary = repost_cleanup_summary(UID)
    assert summary["candidates"] == []
    assert summary["blocked"] == 0
    assert summary["excluded"] == 1
    assert summary["pending_evaluation"] == 0


def test_assessment_uses_budget_and_is_incremental(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr("src.repost_cleanup.ASSESSMENT_BUDGET_PER_ROUND", 2)
    upsert_repost_records(
        UID,
        [
            _import(original_id=oid, repost_id=f"200000000000000000{n}")
            for n, oid in enumerate(
                ("1000000000000000001", "1000000000000000002", "1000000000000000003"), 1
            )
        ],
        seen_at=100,
    )
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])
    monkeypatch.setattr(
        "src.repost_cleanup.classify_for_cleanup",
        lambda *args, **kwargs: (
            ClassifyOutcome(kwargs["original_id"], "转发抽奖", False),
            None,
        ),
    )

    first = scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())
    assert first["evaluated_originals"] == 2
    assert first["pending_originals"] == 1

    second = scan_expired_reposts(now_ts=NOW_TS + 1, client_factory=lambda: _FakeClient())
    assert second["evaluated_originals"] == 1
    assert second["pending_originals"] == 0
    assert second["message"].startswith("历史抽奖评估完成")


def test_classifier_risk_code_stops_round_and_keeps_results(isolated_home, monkeypatch) -> None:
    upsert_repost_records(UID, [_import()], seen_at=100)
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def risk_classifier(*args, **kwargs):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr("src.repost_cleanup.classify_for_cleanup", risk_classifier)

    result = scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())

    assert result["rate_limited"] is True
    assert "已经完成的结果已保存" in result["message"]
    assert result["evaluated_originals"] == 0


def test_identity_untrusted_gets_remote_verify_and_failure_is_blocked(isolated_home, monkeypatch) -> None:
    upsert_repost_records(
        UID,
        [
            _import(repost_id="2000000000000000001", original_id="1000000000000000001"),
            _import(repost_id="2000000000000000002", original_id="1000000000000000002"),
        ],
        seen_at=100,
    )
    set_repost_identity(UID, "2000000000000000001", ok=False, source="remote_detail", error="old failure")
    set_repost_identity(UID, "2000000000000000002", ok=False, source="remote_detail", error="old failure")
    upsert_repost_assessment(
        UID,
        "1000000000000000001",
        assessment_level="safe",
        reason_code="safe_official_notice",
        assessed_at=100,
    )
    upsert_repost_assessment(
        UID,
        "1000000000000000002",
        assessment_level="safe",
        reason_code="safe_official_notice",
        assessed_at=100,
    )
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.load_activities", lambda: [])

    def verify(client, *, uid, repost_dynamic_id, original_dynamic_id):
        if repost_dynamic_id == "2000000000000000001":
            return None
        raise RuntimeError("目标动态不属于当前账号")

    monkeypatch.setattr("src.repost_cleanup._verify_owned_repost_detail", verify)

    scan_expired_reposts(now_ts=NOW_TS, client_factory=lambda: _FakeClient())

    by_id = {c["repost_dynamic_id"]: c for c in load_persisted_candidates(UID)}
    assert by_id["2000000000000000001"]["level"] == "safe"
    assert by_id["2000000000000000002"]["level"] == "blocked"
