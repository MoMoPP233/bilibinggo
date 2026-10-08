"""V115: completed Following batches survive a later network failure."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import following_feed as feed
from src.activity_store import known_activity_ids
from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.classify_step import ClassifyOutcome


IDS = [f"40000000000000{number:05d}" for number in range(400)]
OLD_CHECKPOINT = "3999999999999999999"


class FeedClient:
    def __init__(self, ids: list[str]):
        self.ids = ids
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get_json(self, _url, params=None, **_kwargs):
        self.calls.append(dict(params or {}))
        page = int((params or {}).get("page", 1))
        start = (page - 1) * 20
        chunk = self.ids[start:start + 20]
        return {"code": 0, "data": {
            "items": [{"id_str": did} for did in chunk],
            "has_more": start + 20 < len(self.ids),
            "offset": f"after-{page}" if start + 20 < len(self.ids) else "",
        }}


class NoNetworkClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


@pytest.fixture
def harness(isolated_home: Path, monkeypatch):
    monkeypatch.setattr(feed, "_state_path", lambda: isolated_home / "following_feed_state.json")
    monkeypatch.setattr(feed, "get_login_uid", lambda: "999")
    feed._write_state({"last_seen_dynamic_id": OLD_CHECKPOINT,
                       "anchors": [OLD_CHECKPOINT], "last_scan_status": "success",
                       "history_status": "complete"})
    monkeypatch.setattr(pipeline, "BilibiliClient", NoNetworkClient)
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda row: row)

    class Activity:
        skipped = False

        def __init__(self, dynamic_id):
            self.dynamic_id = dynamic_id

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id, "lottery_type": "转发抽奖",
                    "draw_status": "active", "activity_status": "未参加"}

    monkeypatch.setattr(pipeline, "enrich_activity", lambda _client, *, dynamic_id, **_kw: Activity(dynamic_id))
    seen: Counter[str] = Counter()
    fail_at: list[str | None] = [None]

    def classify(_client, dynamic_id, **_kw):
        seen[dynamic_id] += 1
        if dynamic_id == fail_at[0]:
            raise RuntimeError("网络请求失败: SSL EOF")
        return ClassifyOutcome(dynamic_id, "转发抽奖", False,
                               detail_item={"id_str": dynamic_id, "type": "DYNAMIC_TYPE_DRAW"})

    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    return seen, fail_at, isolated_home / "following_feed_state.json"


def test_four_hundred_candidates_commit_in_sixteen_bounded_batches(harness, monkeypatch):
    seen, _, state_path = harness
    client = FeedClient(IDS)
    real = feed.run_new_links_pipeline
    batch_sizes: list[int] = []

    def counted(urls, **kwargs):
        batch_sizes.append(len(urls))
        return real(urls, **kwargs)

    monkeypatch.setattr(feed, "run_new_links_pipeline", counted)
    result = feed.scan_following_feed(client_factory=lambda: client)
    assert result["found"] == 400 and result["added"] == 400
    assert result["new_link_count"] == 400
    assert batch_sizes == [25] * 16
    assert len(known_activity_ids()) == 400
    assert all(count == 1 for count in seen.values())
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["history_status"] == "complete" and state["last_seen_dynamic_id"] == IDS[0]


def test_later_ssl_eof_keeps_prior_batches_and_restart_skips_them(harness):
    seen, fail_at, state_path = harness
    fail_at[0] = IDS[57]
    with pytest.raises(RuntimeError, match="SSL EOF"):
        feed.scan_following_feed(client_factory=lambda: FeedClient(IDS))
    assert known_activity_ids() == set(IDS[:50])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state.get("last_seen_dynamic_id") == OLD_CHECKPOINT
    assert state.get("history_offset") is None

    fail_at[0] = None
    resumed = feed.scan_following_feed(client_factory=lambda: FeedClient(IDS))
    assert resumed["added"] == 350
    assert len(known_activity_ids()) == 400
    assert all(seen[did] == 1 for did in IDS[:50])
    assert seen[IDS[57]] == 2
    assert all(seen[did] >= 1 for did in IDS[50:])


def test_new_head_items_after_failure_do_not_skip_old_tail(harness):
    seen, fail_at, state_path = harness
    fail_at[0] = IDS[57]
    with pytest.raises(RuntimeError, match="SSL EOF"):
        feed.scan_following_feed(client_factory=lambda: FeedClient(IDS))
    fail_at[0] = None
    inserted_at_head = ["5000000000000000000", "5000000000000000001"]
    result = feed.scan_following_feed(client_factory=lambda: FeedClient(inserted_at_head + IDS))
    saved = known_activity_ids()
    assert set(inserted_at_head).issubset(saved)
    assert all(seen[did] == 1 for did in IDS[:50])
    assert IDS[-1] not in saved  # newest 20-page window did not cover the tail
    assert result["coverage_gap"] and not result["catch_up_complete"]
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["history_status"] == "pending" and state["history_offset"]


def test_repeated_raw_id_across_pages_is_classified_once(harness):
    seen, _, _ = harness
    ids = IDS[:25] + [IDS[0]] + IDS[25:55]
    result = feed.scan_following_feed(client_factory=lambda: FeedClient(ids))
    assert result["found"] == 55 and result["added"] == 55
    assert len(known_activity_ids()) == 55
    assert all(seen[did] == 1 for did in IDS[:55])


def test_database_failure_keeps_old_checkpoint_and_durable_earlier_batches(harness, monkeypatch):
    _, _, state_path = harness
    real = pipeline.append_activities
    calls = 0

    def fail_third(rows):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("database unavailable")
        return real(rows)

    monkeypatch.setattr(pipeline, "append_activities", fail_third)
    with pytest.raises(OSError, match="database unavailable"):
        feed.scan_following_feed(client_factory=lambda: FeedClient(IDS))
    assert calls == 3 and known_activity_ids() == set(IDS[:50])
    assert json.loads(state_path.read_text(encoding="utf-8")).get("last_seen_dynamic_id") == OLD_CHECKPOINT


def test_two_temporary_profiles_do_not_share_committed_batches(harness, isolated_home, monkeypatch):
    from src import data_paths
    from src.db.engine import reset_engine_for_tests
    from src.db.schema import init_db

    selected = ["account-1"]
    monkeypatch.setattr(data_paths, "get_database_path", lambda profile_id=None:
                        isolated_home / "profiles" / (profile_id or selected[0]) / "binggo.db")
    monkeypatch.setattr(feed, "_state_path", lambda:
                        isolated_home / "profiles" / selected[0] / "following_feed_state.json")
    for profile_id, ids in (("account-1", IDS[:30]), ("account-2", IDS[30:60])):
        selected[0] = profile_id
        reset_engine_for_tests()
        init_db()
        feed._write_state({"last_seen_dynamic_id": OLD_CHECKPOINT,
                           "anchors": [OLD_CHECKPOINT], "last_scan_status": "success"})
        result = feed.scan_following_feed(client_factory=lambda ids=ids: FeedClient(ids))
        assert result["added"] == 30
        assert known_activity_ids() == set(ids)
    selected[0] = "account-1"
    reset_engine_for_tests()
    assert known_activity_ids() == set(IDS[:30])


def test_shared_breaker_and_canonical_snapshot_cross_batch_boundary(monkeypatch):
    contexts = []
    observations = []

    def fake_pipeline(urls, **kwargs):
        contexts.append(kwargs["batch_context"])
        observations.extend(kwargs["source_observations"])
        return SimpleNamespace(persisted_count=len(urls), new_link_count=len(urls),
                               skip_reasons={}, partial=False)

    monkeypatch.setattr(feed, "run_new_links_pipeline", fake_pipeline)
    ids = IDS[:26]
    result = feed._run_pipeline_batches(
        ids, identities={}, provenance={did: {"discovery_kind": "direct"} for did in ids},
        preserve_ids={ids[24], ids[25]}, page_by_id={did: 1 for did in ids},
        on_progress=None, scan_started_at=123,
    )
    assert result["persisted_count"] == 26
    assert len(contexts) == 2 and contexts[0] is contexts[1]
    assert [item["dynamic_id"] for item in observations] == ids
    assert contexts[0].breaker is contexts[1].breaker


def test_formal_risk_from_later_batch_propagates_without_cursor_commit(harness, monkeypatch):
    seen, fail_at, state_path = harness
    fail_at[0] = IDS[30]
    original = pipeline.classify_new_link

    def risk(_client, dynamic_id, **kwargs):
        if dynamic_id == fail_at[0]:
            raise RuntimeError("API error -352: risk")
        return original(_client, dynamic_id, **kwargs)

    monkeypatch.setattr(pipeline, "classify_new_link", risk)
    with pytest.raises(RuntimeError, match="-352"):
        feed.scan_following_feed(client_factory=lambda: FeedClient(IDS))
    assert known_activity_ids() == set(IDS[:25])
    assert json.loads(state_path.read_text(encoding="utf-8")).get("last_seen_dynamic_id") == OLD_CHECKPOINT


def test_whole_pipeline_and_following_batches_keep_same_classification(monkeypatch, isolated_home):
    """Compare the old single call with V115 using identical V107 source evidence."""
    from src import data_paths
    from src.activity_store import load_activities
    from src.canonical_identity import CanonicalIdentity
    from src.db.engine import reset_engine_for_tests
    from src.db.schema import init_db
    from src.sources.common import opus_link

    source_ids = IDS[:55]
    origin_id = IDS[90]
    wrapper_ids = {IDS[24], IDS[25]}
    ordinary = lambda did: {
        "id_str": did, "type": "DYNAMIC_TYPE_DRAW",
        "modules": {"module_author": {"mid": 888}},
    }
    items = {did: ordinary(did) for did in source_ids}
    for did in wrapper_ids:
        items[did] = {
            "id_str": did, "type": "DYNAMIC_TYPE_FORWARD",
            "modules": {"module_author": {"mid": 888}},
            "orig": ordinary(origin_id),
        }
    monkeypatch.setattr(feed, "get_login_uid", lambda: 999)
    accepted, identities, provenance, preserve, _counters, blocking = feed._select_following_candidates(
        source_ids, item_by_id=items, client_factory=lambda: NoNetworkClient(), detail_cache={},
    )
    assert blocking == 0
    assert accepted[24:27] == [IDS[24], origin_id, IDS[25]]
    assert preserve == wrapper_ids | {origin_id}
    # A pre-resolved alias after the batch boundary exercises canonical dedupe.
    identities[IDS[40]] = CanonicalIdentity(
        raw_dynamic_id=IDS[40], canonical_dynamic_id=origin_id,
        wrapper_author_uid="888", canonical_author_uid="888", is_forward=False,
        canonical_item=ordinary(origin_id),
    )
    non_lottery = {IDS[5], IDS[35]}
    kind_by_id = {did: ("互动抽奖" if did in {IDS[12], IDS[44]} else "转发抽奖") for did in accepted}
    classified: list[str] = []

    def classify(_client, dynamic_id, **_kwargs):
        classified.append(dynamic_id)
        detail = items.get(dynamic_id, ordinary(dynamic_id))
        if dynamic_id in non_lottery:
            return ClassifyOutcome(dynamic_id, "非抽奖活动", True, "非抽奖活动", detail_item=detail)
        return ClassifyOutcome(dynamic_id, kind_by_id[dynamic_id], False, detail_item=detail)

    class Activity:
        skipped = False

        def __init__(self, dynamic_id, lottery_type):
            self.dynamic_id = dynamic_id
            self.lottery_type = lottery_type

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id, "lottery_type": self.lottery_type,
                    "draw_status": "active", "activity_status": "未参加"}

    monkeypatch.setattr(pipeline, "BilibiliClient", NoNetworkClient)
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda row: row)
    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    monkeypatch.setattr(pipeline, "enrich_activity", lambda _client, *, dynamic_id, lottery_type, **_kw:
                        Activity(dynamic_id, lottery_type))
    selected = ["whole"]
    monkeypatch.setattr(data_paths, "get_database_path", lambda profile_id=None:
                        isolated_home / selected[0] / "binggo.db")
    page_by_id = {did: index // 20 + 1 for index, did in enumerate(source_ids)}
    observations = [{"source_id": "following_feed", "dynamic_id": did,
                     "page_index": page_by_id.get(did), **provenance.get(did, {})} for did in accepted]

    reset_engine_for_tests()
    init_db()
    whole = pipeline.run_new_links_pipeline(
        [opus_link(did) for did in accepted], workers=1, scan_started_at=123,
        identity_by_raw=identities, preserve_forward_raw_ids=preserve,
        source_context_by_dynamic_id=provenance, source_observations=observations,
    )
    whole_rows = {row["dynamic_id"]: row["lottery_type"] for row in load_activities()}
    whole_classified = list(classified)

    selected[0] = "batched"
    reset_engine_for_tests()
    init_db()
    classified.clear()
    per_batch = []
    original = feed.run_new_links_pipeline

    def capture(urls, **kwargs):
        result = original(urls, **kwargs)
        per_batch.append(result)
        return result

    monkeypatch.setattr(feed, "run_new_links_pipeline", capture)
    batched = feed._run_pipeline_batches(
        accepted, identities=identities, provenance=provenance, preserve_ids=preserve,
        page_by_id=page_by_id, on_progress=None, scan_started_at=123,
    )
    batched_rows = {row["dynamic_id"]: row["lottery_type"] for row in load_activities()}
    assert len(per_batch) == 3
    assert batched_rows == whole_rows
    assert classified == whole_classified
    assert set(wrapper_ids | {origin_id}).issubset(batched_rows)
    assert IDS[40] not in batched_rows
    assert batched["persisted_count"] == whole.persisted_count == len(whole_rows)
    assert batched["new_link_count"] == whole.new_link_count
    assert sum(result.classified_count for result in per_batch) == whole.classified_count
    assert sum(result.non_lottery_count for result in per_batch) == whole.non_lottery_count == 2
    assert sum(result.duplicate_link_count for result in per_batch) == whole.duplicate_link_count
