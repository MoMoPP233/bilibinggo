"""DS-3 partial source recovery, exercised through the real source and pipeline."""

from __future__ import annotations

from collections import Counter
from unittest.mock import MagicMock

import httpx
import pytest

from src.db.snapshots import load_ds_check_dict
from src.lottery_api import OpusReadError, OpusRiskControlError
from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.classify_step import ClassifyOutcome
from src.platform_risk import risk_code_of
from src.sources import ds3_gongjuren as ds3
from src.sources.common import CheckResult, commit_source_checkpoint, opus_link
from src.state_store import get_last_container, set_last_container
from tests.test_ds3_article_expansion import (
    CONTAINER as A, HISTORICAL_CONTAINER as C, OTHER_CONTAINER as B,
    LEAF_A, LEAF_B, LEAF_C, _article, _rich,
)
from web import actions


CV = ds3.article_url(999001)


@pytest.fixture
def ds3_case(isolated_home, monkeypatch):
    monkeypatch.setattr(ds3, "OUTPUT_PATH", isolated_home / "data" / "output" / "ds3_latest.json")
    clients: list[MagicMock] = []
    detail_requests: list[tuple[int, str]] = []
    failures: dict[int, Exception] = {0: OpusReadError("合集 B 暂时读取失败")}
    present: list[str] = [A, B, C]

    def new_client():
        client = MagicMock()
        client.__enter__.return_value = client
        client.__exit__.return_value = False
        client.get_latest_article.return_value = {"id": 999001, "title": "最新专栏", "publish_time": 1700000000}
        client.get_article_detail.side_effect = lambda cv: {
            "title": "专栏详情", "publish_time": 1700000000,
            "opus": {"content": {"paragraphs": [{"text": {"nodes": [
                {"node_type": 4, "link": {"biz_id": item, "link_type": 39, "link": opus_link(item)}}
                for item in present
            ]}}]}},
        }
        clients.append(client)
        return client

    def fetch(client, dynamic_id):
        scan_index = clients.index(client)
        detail_requests.append((scan_index, dynamic_id))
        if dynamic_id == B and scan_index in failures:
            raise failures[scan_index]
        leaves = {A: LEAF_A, B: LEAF_B, C: LEAF_C}
        return _article(_rich(leaves[dynamic_id]))

    monkeypatch.setattr(ds3, "BilibiliClient", new_client)
    monkeypatch.setattr(ds3, "fetch_opus_detail_item_strict", fetch)
    monkeypatch.setattr(actions, "DS_HANDLER_BY_ID", {"DS-3": (ds3.check_update, ds3.save_result)})
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)

    # Keep the real run_refresh_all_pipeline, classification and canonical DB
    # dedup path. Only remote classification/enrichment and activity writer are
    # replaced with local deterministic fakes.
    persisted: set[str] = set()
    classified: list[str] = []
    offline_client = MagicMock()
    offline_client.__enter__.return_value = offline_client
    offline_client.__exit__.return_value = False
    monkeypatch.setattr(pipeline, "BilibiliClient", lambda: offline_client)
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set(persisted))
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})

    def classify(client, dynamic_id):
        classified.append(dynamic_id)
        return ClassifyOutcome(dynamic_id, "互动抽奖", False)

    class Activity:
        skipped = False

        def __init__(self, dynamic_id):
            self.dynamic_id = dynamic_id

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id, "lottery_type": "互动抽奖"}

    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    monkeypatch.setattr(pipeline, "enrich_activity", lambda *a, **kw: Activity(kw["dynamic_id"]))
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda row: row)

    def append(rows):
        new_ids = {row["dynamic_id"] for row in rows} - persisted
        persisted.update(new_ids)
        return len(new_ids)

    monkeypatch.setattr(pipeline, "append_activities", append)
    return {"clients": clients, "requests": detail_requests, "failures": failures,
            "present": present, "persisted": persisted, "classified": classified}


def run_source():
    return actions.run_action("refresh_source", {"source_id": "DS-3"})


def test_partial_pipeline_restart_retry_only_b_then_commit(ds3_case):
    first = run_source()
    assert first["ok"] and first["result"]["pipeline"]["partial"] is False
    assert first["result"]["source"]["source_complete"] is False
    assert "部分合集读取失败" in first["log"]
    assert "下次将继续补读" in first["log"]
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C)]
    assert ds3_case["persisted"] == {LEAF_A, LEAF_C}
    assert get_last_container("DS-3") is None

    durable = load_ds_check_dict("DS-3")
    assert durable["source_complete"] is False
    assert durable["source_incomplete_containers"] == [B]
    assert set(durable["ds3_expanded_containers"]) == {A, C}
    assert durable["activity_links"] == [opus_link(LEAF_A), opus_link(LEAF_C)]

    # The next action constructs a NEW Bilibili client; it reads the Profile
    # snapshot from SQLite. No state is carried in a DS-3 Python instance.
    second = run_source()
    assert len(ds3_case["clients"]) == 2
    assert second["result"]["source"]["source_complete"] is True
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C), (1, B)]
    assert ds3_case["persisted"] == {LEAF_A, LEAF_B, LEAF_C}
    assert Counter(ds3_case["classified"]) == {LEAF_A: 1, LEAF_B: 1, LEAF_C: 1}
    assert get_last_container("DS-3") == CV
    assert load_ds_check_dict("DS-3")["source_complete"] is True

    # A complete unchanged cv still takes the old fast path.
    third = run_source()
    assert third["result"]["pipeline_skipped"] is True
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C), (1, B)]
    assert ds3_case["clients"][-1].get_article_detail.call_count == 0


def test_missing_failed_container_closes_incomplete_on_next_cv_read(ds3_case):
    run_source()
    ds3_case["present"].remove(B)
    second = run_source()
    assert second["result"]["source"]["source_complete"] is True
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C)]
    assert get_last_container("DS-3") == CV
    assert load_ds_check_dict("DS-3")["source_incomplete_containers"] == []


def test_pending_snapshot_overrides_same_cv_checkpoint(ds3_case):
    set_last_container("DS-3", CV, container_id="999001")
    first = ds3.check_update(force=True)
    ds3.save_result(first)
    assert first.source_complete is False
    assert first.updated is True
    assert get_last_container("DS-3") == CV  # old complete value, not a new commit
    second = run_source()
    assert second["result"]["source"]["source_complete"] is True
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C), (1, B)]


def test_old_profile_without_v85_fields_keeps_unchanged_fast_path(ds3_case, monkeypatch):
    from src.db.snapshots import save_ds_check_dict

    set_last_container("DS-3", CV, container_id="999001")
    save_ds_check_dict({"source_id": "DS-3", "updated": True, "container_url": CV,
                        "activity_links": [opus_link(LEAF_A)], "checked_at": 1})
    result = ds3.check_update(force=False)
    assert result.updated is False
    assert result.source_complete is True
    assert ds3_case["requests"] == []


def test_common_checkpoint_guard_rejects_incomplete_result(ds3_case):
    result = ds3.check_update(force=True)
    assert not result.source_complete
    commit_source_checkpoint(result)
    assert get_last_container("DS-3") is None


@pytest.mark.parametrize("failure", [
    OpusReadError("opus/detail ConnectError"),
    OpusReadError("opus/detail Timeout"),
    OpusReadError("opus/detail TLS failed"),
    OpusReadError("opus/detail business unreadable"),
])
def test_ordinary_read_failures_are_partial_not_formal_risk(ds3_case, failure):
    ds3_case["failures"][0] = failure
    first = run_source()
    assert first["result"]["source"]["source_complete"] is False
    assert ds3_case["persisted"] == {LEAF_A, LEAF_C}
    assert risk_code_of(failure) is None
    assert get_last_container("DS-3") is None


@pytest.mark.parametrize("code", [-315, -412])
def test_other_business_codes_remain_ordinary_partial(ds3_case, code):
    error = OpusReadError(f"opus/detail API error {code}: 暂不可读")
    ds3_case["failures"][0] = error
    assert risk_code_of(error) is None
    result = run_source()
    assert result["result"]["source"]["source_complete"] is False
    assert ds3_case["requests"] == [(0, A), (0, B), (0, C)]


@pytest.mark.parametrize("risk_code", [-352, -509, 429])
def test_formal_risk_stops_after_b_preserves_a_snapshot_and_checkpoint(ds3_case, risk_code):
    if risk_code == 429:
        request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail")
        response = httpx.Response(429, request=request)
        try:
            raise httpx.HTTPStatusError("429", request=request, response=response)
        except httpx.HTTPStatusError as cause:
            try:
                raise OpusRiskControlError("opus/detail HTTP 429: 访问过于频繁") from cause
            except OpusRiskControlError as error:
                risk = error
    else:
        risk = OpusRiskControlError(f"opus/detail API error {risk_code}: 限流")
    ds3_case["failures"][0] = risk

    with pytest.raises(OpusRiskControlError) as caught:
        run_source()
    assert risk_code_of(caught.value) == risk_code
    assert ds3_case["requests"] == [(0, A), (0, B)]  # C never sent
    assert ds3_case["persisted"] == set()  # no later classification HTTP
    assert get_last_container("DS-3") is None
    durable = load_ds_check_dict("DS-3")
    assert durable["source_complete"] is False
    assert durable["activity_links"] == [opus_link(LEAF_A)]
    assert durable["source_incomplete_containers"] == [B, C]


def test_snapshot_write_failure_does_not_hide_formal_risk(ds3_case, monkeypatch):
    ds3_case["failures"][0] = OpusRiskControlError("opus/detail API error -352: 限流")
    monkeypatch.setattr(ds3, "save_result", lambda result: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OpusRiskControlError) as caught:
        run_source()
    assert risk_code_of(caught.value) == -352
    assert isinstance(caught.value.__cause__, OSError)
    assert ds3_case["requests"] == [(0, A), (0, B)]
    assert get_last_container("DS-3") is None


def test_refresh_all_keeps_ds3_http_429_risk_code(ds3_case, monkeypatch):
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail")
    response = httpx.Response(429, request=request)
    try:
        raise httpx.HTTPStatusError("429", request=request, response=response)
    except httpx.HTTPStatusError as cause:
        try:
            raise OpusRiskControlError("opus/detail HTTP 429: 访问过于频繁") from cause
        except OpusRiskControlError as error:
            ds3_case["failures"][0] = error
    monkeypatch.setattr(actions, "DS_HANDLERS", [("DS-3", ds3.check_update, ds3.save_result)])
    with pytest.raises(OpusRiskControlError) as caught:
        actions.run_action("refresh_all")
    assert risk_code_of(caught.value) == 429
    assert ds3_case["requests"] == [(0, A), (0, B)]
    assert load_ds_check_dict("DS-3")["activity_links"] == [opus_link(LEAF_A)]
    assert get_last_container("DS-3") is None


def test_pipeline_partial_still_blocks_complete_source_checkpoint(ds3_case, monkeypatch):
    from src.pipeline.refresh_all_pipeline import PipelineResult

    ds3_case["failures"].clear()
    def partial_pipeline(*args, **kwargs):
        return PipelineResult(ok=True, pipeline_skipped=False, raw_link_count=3,
                              new_link_count=3, classified_count=2, skipped_count=0,
                              enriched_count=2, persisted_count=2, partial=True,
                              message="LLM 服务异常")
    monkeypatch.setattr(actions, "run_refresh_all_pipeline", partial_pipeline)
    result = run_source()
    assert result["result"]["source"]["source_complete"] is True
    assert get_last_container("DS-3") is None
    assert "未推进数据源检查点" in result["log"]


def test_refresh_all_only_ds3_incomplete_blocks_its_checkpoint(ds3_case, monkeypatch):
    other = CheckResult(source_id="DS-2", updated=True, container_url="https://example.test/cv2",
                        container_id="2", title="other", published_at=1,
                        previous_container_url=None, activity_links=[], checked_at=1)
    monkeypatch.setattr(actions, "DS_HANDLERS", [
        ("DS-2", lambda *, force: other, lambda result: None),
        ("DS-3", ds3.check_update, ds3.save_result),
    ])
    result = actions.run_action("refresh_all")
    assert result["ok"]
    assert get_last_container("DS-2") == other.container_url
    assert get_last_container("DS-3") is None
    assert "下次将继续补读" in result["log"]
    assert {item["source_id"]: item["source_complete"] for item in result["result"]["sources"]} == {
        "DS-2": True, "DS-3": False,
    }
