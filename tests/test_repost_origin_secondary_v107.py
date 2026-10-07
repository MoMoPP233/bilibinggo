"""V107 repost-origin discovery: offline, one level, no live requests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src import ds9_contribution_audit, following_feed as ff
from src import participation_guard
from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.classify_fetch_context import ClassifyFetchContext
from src.pipeline.classify_step import ClassifyOutcome
from src.repost_origin_discovery import discover_origin
from src.sources import ds9_topics as ds9
from src.sources.common import commit_source_checkpoint, opus_link

A = "2000000000000000001"
B = "2000000000000000002"
C = "2000000000000000003"
D = "2000000000000000004"
UID = "1000000000000000001"
TOPIC = "1261901"


def _item(dynamic_id: str, *, orig: dict | None = None, forward: bool = False) -> dict:
    item = {
        "id_str": dynamic_id,
        "type": "DYNAMIC_TYPE_FORWARD" if forward or orig else "DYNAMIC_TYPE_DRAW",
        "modules": {"module_author": {"mid": 9999}},
    }
    if orig is not None:
        item["orig"] = orig
    return item


class _Client:
    def __init__(self, *, detail: dict | None = None, feed: list[dict] | None = None) -> None:
        self.detail = detail or {}
        self.feed = feed or []
        self.detail_calls: list[tuple[str, int]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def request_json(self, _url, params=None, **kwargs):
        dynamic_id = str((params or {}).get("id") or "")
        self.detail_calls.append((dynamic_id, kwargs["retries"]))
        result = self.detail[dynamic_id]
        if isinstance(result, Exception):
            raise result
        return result

    def get_json(self, *_args, **_kwargs):
        return {"code": 0, "data": {"items": self.feed}}

    def get_topic_feed(self, topic_id, *, offset="", **_kwargs):
        assert topic_id == TOPIC
        assert not offset
        return {
            "code": 0,
            "data": {"topic_card_list": {
                "items": [{"dynamic_card_item": item, "topic_type": "DYNAMIC"} for item in self.feed],
                "has_more": False,
                "offset": "",
            }},
        }


def _detail(item: dict) -> dict:
    return {"code": 0, "data": {"item": item}}


def test_embedded_origin_uses_no_detail_and_only_one_level() -> None:
    nested = _item(B, orig=_item(C), forward=True)
    client = _Client()
    found = discover_origin(_item(A, orig=nested), A, client=client)
    assert found.original_id == B
    assert client.detail_calls == []


def test_missing_origin_fallback_is_round_cached_zero_retry() -> None:
    client = _Client(detail={A: _detail(_item(A, orig=_item(B)))})
    cache = {}
    for _ in range(40):
        assert discover_origin(_item(A, forward=True), A, client=client, cache=cache).original_id == B
    assert client.detail_calls == [(A, 0)]


@pytest.mark.parametrize("code", [-352, -509])
def test_formal_risk_stops_origin_discovery(code: int) -> None:
    client = _Client(detail={A: {"code": code, "message": "risk"}})
    with pytest.raises(RuntimeError, match=str(code)):
        discover_origin(_item(A, forward=True), A, client=client)
    assert client.detail_calls == [(A, 0)]


def test_http_429_stops_origin_discovery() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail")
    response = httpx.Response(429, request=request)
    client = _Client(detail={A: httpx.HTTPStatusError("rate", request=request, response=response)})
    with pytest.raises(httpx.HTTPStatusError):
        discover_origin(_item(A, forward=True), A, client=client)


def test_network_is_retryable_but_deleted_is_terminal() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail")
    client = _Client(detail={A: httpx.ReadTimeout("timeout", request=request)})
    result = discover_origin(_item(A, forward=True), A, client=client)
    assert result.retryable and result.original_id is None
    client.detail[A] = {"code": 4101131, "message": "动态不存在"}
    result = discover_origin(_item(A, forward=True), A, client=client)
    assert not result.retryable and result.original_id is None


@pytest.mark.parametrize("code", [-315, -412])
def test_non_formal_codes_remain_local_retryable(code: int) -> None:
    client = _Client(detail={A: {"code": code, "message": "ordinary failure"}})
    result = discover_origin(_item(A, forward=True), A, client=client)
    assert result.retryable and result.reason == f"origin_detail_code_{code}"


@pytest.fixture
def source_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_home: Path) -> Path:
    target = tmp_path / "profile" / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: target)
    return target


def test_ds9_topic_origin_provenance_and_checkpoint(source_state: Path, monkeypatch) -> None:
    client = _Client(feed=[_item(A, orig=_item(B))])
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)
    result = ds9.check_update()
    assert result.activity_links == [opus_link(A), opus_link(B)]
    assert result.activity_source_context[B] == {
        "topic_id": TOPIC,
        "topic_name": "抽奖！！！",
        "source_id": "DS-9",
        "discovery_kind": "repost_origin",
        "via_dynamic_id": A,
    }
    assert client.detail_calls == []
    assert any(
        observation["dynamic_id"] == B and observation["topic_id"] == TOPIC
        for observation in result.activity_source_observations
    )
    before = {row["topic_id"]: row for row in ds9.list_topics()}[TOPIC]
    assert before["fp"] is None
    monkeypatch.setattr(
        ds9_contribution_audit, "list_unique_valid",
        lambda: [{"topic_id": TOPIC, "dynamic_id": B}],
    )
    commit_source_checkpoint(result)
    after = {row["topic_id"]: row for row in ds9.list_topics()}[TOPIC]
    assert after["fp"] is not None
    assert after["last_success_unique_ids"] == [B]
    assert after["last_unique_added"] is None  # first successful scan is baseline
    assert B not in after.get("outer_ids", [])


def test_ds9_partial_keeps_checkpoint_and_wrapper(source_state: Path, monkeypatch) -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail")
    client = _Client(feed=[_item(A, forward=True)], detail={A: httpx.ReadTimeout("timeout", request=request)})
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)
    result = ds9.check_update()
    assert result.activity_links == [opus_link(A)]
    assert result.source_complete is False
    assert result.source_incomplete_containers == [opus_link(A)]
    commit_source_checkpoint(result)
    assert {row["topic_id"]: row for row in ds9.list_topics()}[TOPIC]["fp"] is None


def test_unavailable_orig_is_terminal_and_outer_remains(source_state: Path, monkeypatch) -> None:
    client = _Client(
        feed=[_item(A, forward=True)],
        detail={A: _detail(_item(A, forward=True))},
    )
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)
    result = ds9.check_update()
    assert result.activity_links == [opus_link(A)]
    assert result.source_complete is True
    assert result.source_incomplete_containers == []
    assert client.detail_calls == [(A, 0)]


def test_origin_formal_risk_stops_ds9_topics(source_state: Path, monkeypatch) -> None:
    client = _Client(feed=[_item(A, forward=True)], detail={A: {"code": -352, "message": "risk"}})
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)
    with pytest.raises(RuntimeError, match="-352"):
        ds9.check_update()
    assert client.detail_calls == [(A, 0)]
    assert {row["topic_id"]: row for row in ds9.list_topics()}[TOPIC]["fp"] is None


def test_following_preserves_outer_cursor_and_dedupes_shared_origin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ff, "_state_path", lambda: tmp_path / "profile" / "following_feed_state.json")
    monkeypatch.setattr(ff, "get_login_uid", lambda: int(UID))
    client = _Client(feed=[_item(A, orig=_item(B)), _item(C, orig=_item(B))])
    captured = {}

    def fake_pipeline(urls, **kwargs):
        captured["urls"] = urls
        captured["kwargs"] = kwargs
        return SimpleNamespace(persisted_count=len(urls), new_link_count=len(urls), skip_reasons={}, partial=False)

    monkeypatch.setattr(ff, "run_new_links_pipeline", fake_pipeline)
    result = ff.scan_following_feed(client_factory=lambda: client)
    assert result["status"] == "success"
    assert captured["urls"] == [opus_link(A), opus_link(B), opus_link(C)]
    assert captured["kwargs"]["source_context_by_dynamic_id"][B]["via_dynamic_id"] == A
    assert client.detail_calls == []
    assert B not in str((tmp_path / "profile" / "following_feed_state.json").read_text())


def test_following_retryable_origin_blocks_checkpoint(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ff, "_state_path", lambda: tmp_path / "profile" / "following_feed_state.json")
    monkeypatch.setattr(ff, "get_login_uid", lambda: int(UID))
    request = httpx.Request("GET", "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail")
    client = _Client(feed=[_item(A, forward=True)], detail={A: httpx.ReadTimeout("timeout", request=request)})
    monkeypatch.setattr(ff, "run_new_links_pipeline", lambda urls, **kwargs: SimpleNamespace(
        persisted_count=0, new_link_count=len(urls), skip_reasons={}, partial=False,
    ))
    result = ff.scan_following_feed(client_factory=lambda: client)
    # V107 origin uncertainty is source-incomplete, while retaining V16's
    # checkpoint safety and its older pipeline-body retry contract.
    assert result["status"] == "incomplete"
    assert result["stop_reason"] == "pipeline_partial"
    state = json.loads((tmp_path / "profile" / "following_feed_state.json").read_text())
    assert state["last_status"] == "incomplete"
    assert state["last_seen_dynamic_id"] is None
    assert client.detail_calls == [(A, 0)]


def test_outer_content_never_inherits_orig_text() -> None:
    outer = _item(A, orig=_item(B))
    outer["modules"]["module_dynamic"] = {"desc": {"text": "普通转发"}}
    outer["orig"]["modules"]["module_dynamic"] = {"desc": {"text": "原动态抽奖"}}
    ctx = ClassifyFetchContext(_Client(), A, outer_only_content=True)
    ctx._detail_item = outer
    text = ctx.resolve_classify_content()
    assert "普通转发" in text
    assert "原动态抽奖" not in text


@pytest.mark.parametrize("lottery_ids,expected", [({B}, [B]), ({A}, [A]), ({A, B}, [A, B]), (set(), [])])
def test_pipeline_classifies_outer_and_inner_independently(
    monkeypatch, isolated_home: Path, lottery_ids: set[str], expected: list[str],
) -> None:
    details = {A: _item(A, orig=_item(B)), B: _item(B)}
    seen: list[str] = []
    rows: list[dict] = []
    known: set[str] = set()

    class Activity:
        skipped = False

        def __init__(self, dynamic_id: str):
            self.dynamic_id = dynamic_id

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id}

    def classify(_client, dynamic_id, *, outer_only_content=False):
        seen.append(dynamic_id)
        assert outer_only_content is True
        if dynamic_id not in lottery_ids:
            return ClassifyOutcome(dynamic_id, "非抽奖活动", True, "非抽奖活动", detail_item=details[dynamic_id])
        return ClassifyOutcome(dynamic_id, "转发抽奖", False, detail_item=details[dynamic_id])

    monkeypatch.setattr(pipeline, "BilibiliClient", lambda: _Client())
    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: set(known))
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr("src.activity_store.known_activity_aliases", lambda: set())
    monkeypatch.setattr(participation_guard, "load_blocked_guard_ids", lambda _uid: set())
    monkeypatch.setattr(participation_guard, "list_confirmed_guards", lambda _uid: [])
    monkeypatch.setattr(pipeline, "enrich_activity", lambda _client, *, dynamic_id, **_kwargs: Activity(dynamic_id))
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda value: value)
    monkeypatch.setattr(pipeline, "append_activities", lambda values: rows.extend(values) or known.update(row["dynamic_id"] for row in values) or len(values))
    monkeypatch.setattr(pipeline, "_record_terminal_failure", lambda *_args, **_kwargs: None)
    kwargs = {
        "preserve_forward_raw_ids": {A, B},
        "source_context_by_dynamic_id": {
            B: {"source_id": "DS-9", "topic_id": TOPIC, "discovery_kind": "repost_origin", "via_dynamic_id": A},
        },
    }
    result = pipeline.run_new_links_pipeline([opus_link(A), opus_link(B), opus_link(B)], workers=1, **kwargs)
    assert result.persisted_count == len(expected)
    assert seen == [A, B]
    assert [row["dynamic_id"] for row in rows] == expected
    if B in expected:
        assert rows[expected.index(B)]["source_discovery"]["via_dynamic_id"] == A
    # Re-observation uses the same canonical DB keys; already persisted IDs
    # are skipped before classification, while previously non-lottery IDs
    # remain eligible for a later source scan.
    second = pipeline.run_new_links_pipeline([opus_link(A), opus_link(B)], workers=1, **kwargs)
    assert second.persisted_count == 0
    assert seen == [A, B] + [dynamic_id for dynamic_id in (A, B) if dynamic_id not in lottery_ids]
