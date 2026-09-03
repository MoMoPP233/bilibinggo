"""扫描时过滤“可靠证明已在扫描开始前结束”的官方抽奖（保守策略，0 额外请求）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import web.actions as actions
from src.lottery_enricher import EnrichedActivity
from src.pipeline.classify_step import ClassifyOutcome
from src.pipeline.refresh_all_pipeline import (
    is_expired_before_scan,
    run_new_links_pipeline,
)

SCAN = 1_700_000_000
DYNAMIC = {"expired": "2000000000000000001", "active": "2000000000000000002", "forward": "2000000000000000003"}


def _info(lottery_type, business_type, lottery_time, *, inferred=False):
    conditions = {}
    if inferred:
        conditions["lottery_time_inferred"] = True
    return {
        "lottery_type": lottery_type,
        "business_type": business_type,
        "lottery_time": lottery_time,
        "conditions": conditions,
    }


def test_official_reliable_time_before_scan_is_expired() -> None:
    assert is_expired_before_scan(_info("互动抽奖", 1, SCAN - 1), SCAN) is True


def test_official_reliable_time_after_scan_is_kept() -> None:
    assert is_expired_before_scan(_info("互动抽奖", 1, SCAN + 1), SCAN) is False


def test_official_time_equal_to_scan_is_kept_conservatively() -> None:
    assert is_expired_before_scan(_info("互动抽奖", 1, SCAN), SCAN) is False


def test_inferred_past_time_is_kept() -> None:
    assert (
        is_expired_before_scan(_info("互动抽奖", 1, SCAN - 100, inferred=True), SCAN)
        is False
    )


def test_llm_guessed_time_is_kept() -> None:
    # 转发抽奖无官方 notice：即使时间在过去也绝不按它过滤。
    assert is_expired_before_scan(_info("转发抽奖", 0, SCAN - 100), SCAN) is False


def test_missing_official_time_is_kept() -> None:
    assert is_expired_before_scan(_info("预约抽奖", 10, None), SCAN) is False


def test_non_official_business_is_kept() -> None:
    assert is_expired_before_scan(_info("预约抽奖", 0, SCAN - 100), SCAN) is False


def test_forward_lottery_is_never_filtered() -> None:
    assert is_expired_before_scan(_info("转发抽奖", 0, SCAN - 9999), SCAN) is False
    assert is_expired_before_scan(_info("转发抽奖", 1, SCAN - 9999), SCAN) is False


def test_explicit_ended_without_reliable_time_is_kept_conservatively() -> None:
    # status!=0 表示官方已开奖，但缺少可靠开奖时间时无法证明 < scan_started_at → 保留。
    activity = _info("互动抽奖", 1, None)
    activity["status_code"] = 1
    activity["winners"] = {"first": []}
    assert is_expired_before_scan(activity, SCAN) is False


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _activity(dynamic_id: str, lottery_type: str, business_type: int, lottery_time, *, inferred=False):
    return EnrichedActivity(
        dynamic_id=dynamic_id,
        source_url=f"https://www.bilibili.com/opus/{dynamic_id}",
        lottery_type=lottery_type,
        enriched_at=SCAN,
        business_id=dynamic_id,
        business_type=business_type,
        draw_status="active",
        lottery_time=lottery_time,
        prizes=[],
        participants=0,
        conditions={"lottery_time_inferred": True} if inferred else {},
        winners=None,
        platform_participated=None,
    )


def test_pipeline_drops_only_reliably_expired_official_and_keeps_dedup_behavior(
    monkeypatch,
) -> None:
    """过滤发生在公共导入层：被过滤项不进入 append_activities，正常项照常入库。"""
    urls = [f"https://www.bilibili.com/opus/{dynamic_id}" for dynamic_id in DYNAMIC.values()]
    type_by_id = {
        DYNAMIC["expired"]: ("互动抽奖", 1, SCAN - 10, False),
        DYNAMIC["active"]: ("互动抽奖", 1, SCAN + 60, False),
        DYNAMIC["forward"]: ("转发抽奖", 0, SCAN - 50, True),
    }

    def fake_classify(client, dynamic_id):
        lottery_type, bt, lt, inferred = type_by_id[str(dynamic_id)]
        return ClassifyOutcome(dynamic_id, lottery_type, False, classify_content="x")

    def fake_enrich(client, *, dynamic_id, lottery_type, participation=None, **kwargs):
        _bt = type_by_id[str(dynamic_id)][1]
        _lt = type_by_id[str(dynamic_id)][2]
        _inf = type_by_id[str(dynamic_id)][3]
        return _activity(str(dynamic_id), lottery_type, _bt, _lt, inferred=_inf)

    appended: list[dict] = []
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set())
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", _FakeClient)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.classify_new_link", fake_classify)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.enrich_activity", fake_enrich)
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.append_activities",
        lambda rows: len(rows) and appended.extend(rows) or len(rows),
    )

    result = run_new_links_pipeline(urls, workers=1, scan_started_at=SCAN)

    assert result.ok is True
    assert result.new_link_count == 3
    assert result.classified_count == 3
    assert result.enriched_count == 3
    assert result.expired_skipped_count == 1
    assert result.persisted_count == 2
    kept_ids = {str(row["dynamic_id"]) for row in appended}
    assert kept_ids == {DYNAMIC["active"], DYNAMIC["forward"]}
    assert DYNAMIC["expired"] not in kept_ids
    assert "已跳过 1 条" in result.message


def test_pipeline_without_scan_started_at_keeps_everything(monkeypatch) -> None:
    urls = [f"https://www.bilibili.com/opus/{DYNAMIC['expired']}"]
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set())
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", _FakeClient)
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.classify_new_link",
        lambda client, dynamic_id: ClassifyOutcome(dynamic_id, "互动抽奖", False),
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.enrich_activity",
        lambda client, *, dynamic_id, lottery_type, participation=None, **kw: _activity(
            str(dynamic_id), lottery_type, 1, SCAN - 10
        ),
    )
    appended: list[dict] = []
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.append_activities",
        lambda rows: len(rows) and appended.extend(rows) or len(rows),
    )

    result = run_new_links_pipeline(urls, workers=1)

    assert result.expired_skipped_count == 0
    assert result.persisted_count == 1


def test_update_all_uses_one_fixed_scan_started_at_for_every_source(monkeypatch) -> None:
    seen: list[int | None] = []
    received: dict[str, object] = {}

    def fake_check(source_id: str):
        def check_update(*, force=False, **kwargs):
            return _check_result(source_id, updated=True)

        return check_update

    def _check_result(source_id: str, *, updated: bool):
        from src.sources.common import CheckResult

        return CheckResult(
            source_id=source_id,
            updated=updated,
            container_url=f"https://example.com/{source_id}",
            container_id=f"{source_id}-id",
            title="t",
            published_at=1,
            previous_container_url=None,
            activity_links=[],
            checked_at=1,
        )

    def fake_pipeline(ds_results, **kwargs):
        seen.append(kwargs.get("scan_started_at"))
        from src.pipeline.refresh_all_pipeline import PipelineResult

        return PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
            classified_count=1, skipped_count=0, enriched_count=1,
            persisted_count=1, message="ok",
        )

    monkeypatch.setattr(actions, "run_refresh_all_pipeline", fake_pipeline)
    monkeypatch.setattr(actions, "commit_source_checkpoint", lambda result: None)
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)
    monkeypatch.setattr(
        actions, "set_last_pipeline_persisted", lambda **kwargs: None
    )
    handlers = [("DS-1", fake_check("DS-1"), lambda r: Path("/tmp/x")),
                ("DS-2", fake_check("DS-2"), lambda r: Path("/tmp/x"))]
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = handlers
    try:
        payload = actions.run_action(
            "update_all_datasources",
            {},
            on_progress=lambda **kwargs: None,
            cancel_event=None,
        )
    finally:
        actions.DS_HANDLERS = original

    assert payload["ok"] is True
    assert len(seen) == 2
    assert seen[0] is not None
    assert seen[0] == seen[1]


def test_single_source_uses_its_own_scan_started_at(monkeypatch) -> None:
    captured: list[int | None] = []

    def fake_pipeline(ds_results, **kwargs):
        captured.append(kwargs.get("scan_started_at"))
        from src.pipeline.refresh_all_pipeline import PipelineResult

        return PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
            classified_count=1, skipped_count=0, enriched_count=1,
            persisted_count=1, message="ok",
        )

    def _check_fake(source_id: str):
        def check_update(*, force=False, **kwargs):
            from src.sources.common import CheckResult

            return CheckResult(
                source_id=source_id, updated=True,
                container_url="https://example.com/x", container_id="x", title="t",
                published_at=1, previous_container_url=None, activity_links=[],
                checked_at=1,
            )

        return check_update

    monkeypatch.setattr(actions, "run_refresh_all_pipeline", fake_pipeline)
    monkeypatch.setattr(actions, "commit_source_checkpoint", lambda result: None)
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)
    monkeypatch.setattr(actions, "set_last_pipeline_persisted", lambda **kwargs: None)
    monkeypatch.setattr(
        actions, "DS_HANDLER_BY_ID",
        {"DS-3": (_check_fake("DS-3"), lambda result: Path("/tmp/x"))},
    )

    payload = actions.run_action(
        "refresh_source", {"source_id": "DS-3"},
        on_progress=lambda **kwargs: None,
        cancel_event=None,
    )

    assert payload["ok"] is True
    assert len(captured) == 1
    assert captured[0] is not None
