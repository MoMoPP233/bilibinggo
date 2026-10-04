"""V91: reuse only the pre-classification activity-ID snapshot (offline)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src import activity_store
from src.llm_client import LlmTemporaryError
from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.classify_step import ClassifyOutcome


BASE = 2_900_000_000_000_000_000


def _id(index: int) -> str:
    return str(BASE + index)


def _url(dynamic_id: str) -> str:
    return f"https://www.bilibili.com/opus/{dynamic_id}"


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class _Activity:
    skipped = False

    def __init__(self, dynamic_id: str) -> None:
        self.dynamic_id = dynamic_id

    def to_dict(self) -> dict:
        return {"dynamic_id": self.dynamic_id, "lottery_type": "互动抽奖"}


@pytest.fixture
def offline_pipeline(isolated_home: Path, monkeypatch: pytest.MonkeyPatch):
    real_known = activity_store.known_activity_ids
    real_append = activity_store.append_activities
    known_reads: list[set[str]] = []
    append_calls: list[list[str]] = []
    classified: list[str] = []
    enriched: list[str] = []
    behavior = {
        "classify": lambda dynamic_id: ClassifyOutcome(dynamic_id, "互动抽奖", False),
        "enrich": lambda dynamic_id: _Activity(dynamic_id),
    }

    def known() -> set[str]:
        ids = real_known()
        known_reads.append(set(ids))
        return ids

    def classify(_client, dynamic_id: str) -> ClassifyOutcome:
        classified.append(dynamic_id)
        return behavior["classify"](dynamic_id)

    def enrich(_client, *, dynamic_id: str, **_kwargs) -> _Activity:
        enriched.append(dynamic_id)
        return behavior["enrich"](dynamic_id)

    def append(rows: list[dict]) -> int:
        append_calls.append([str(row["dynamic_id"]) for row in rows])
        return real_append(rows)

    monkeypatch.setattr(pipeline, "known_activity_ids", known)
    monkeypatch.setattr(pipeline, "append_activities", append)
    monkeypatch.setattr(pipeline, "BilibiliClient", _Client)
    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    monkeypatch.setattr(pipeline, "enrich_activity", enrich)
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda row: row)
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr(pipeline, "invalid_activity_ids", lambda: set())
    monkeypatch.setattr(pipeline, "manual_ignore_ids", lambda: set())
    monkeypatch.setattr(activity_store, "known_activity_aliases", lambda: set())
    monkeypatch.setattr("src.db.uids.participation_uid", lambda: "test-uid")
    monkeypatch.setattr("src.participation_guard.load_blocked_guard_ids", lambda _uid: set())
    monkeypatch.setattr("src.participation_guard.list_confirmed_guards", lambda _uid: [])
    return SimpleNamespace(
        known_reads=known_reads, append_calls=append_calls,
        classified=classified, enriched=enriched, behavior=behavior,
    )


def test_new_candidates_use_two_known_reads_and_persist(offline_pipeline) -> None:
    ids = [_id(1), _id(2)]
    result = pipeline.run_new_links_pipeline([_url(item) for item in ids], workers=1)
    assert result.persisted_count == 2
    assert offline_pipeline.classified == ids
    assert offline_pipeline.append_calls == [ids]
    assert len(offline_pipeline.known_reads) == 2
    assert activity_store.known_activity_ids() == set(ids)


def test_existing_candidates_use_one_known_read(offline_pipeline) -> None:
    dynamic_id = _id(3)
    activity_store.append_activities([{"dynamic_id": dynamic_id}])
    result = pipeline.run_new_links_pipeline([_url(dynamic_id)], workers=1)
    assert result.existing_count == 1
    assert result.persisted_count == 0
    assert offline_pipeline.classified == []
    assert len(offline_pipeline.known_reads) == 1


def test_batch_duplicates_are_still_deduplicated(offline_pipeline) -> None:
    dynamic_id = _id(4)
    result = pipeline.run_new_links_pipeline(
        [_url(dynamic_id), f"https://t.bilibili.com/{dynamic_id}"], workers=1,
    )
    assert result.duplicate_link_count == 1
    assert result.persisted_count == 1
    assert offline_pipeline.classified == [dynamic_id]
    assert len(offline_pipeline.known_reads) == 2


def test_canonical_duplicates_still_collapse(offline_pipeline) -> None:
    original = _id(5)
    wrappers = [_id(6), _id(7)]

    def classify(dynamic_id: str) -> ClassifyOutcome:
        return ClassifyOutcome(
            dynamic_id, "互动抽奖", False,
            detail_item={
                "id_str": dynamic_id, "type": "DYNAMIC_TYPE_FORWARD",
                "modules": {"module_author": {"mid": 1001}},
                "orig": {
                    "id_str": original, "type": "DYNAMIC_TYPE_DRAW",
                    "modules": {"module_author": {"mid": 2002}},
                },
            },
        )

    offline_pipeline.behavior["classify"] = classify
    result = pipeline.run_new_links_pipeline([_url(item) for item in wrappers], workers=1)
    assert result.duplicate_link_count == 1
    assert result.persisted_count == 1
    assert offline_pipeline.append_calls == [[original]]
    assert len(offline_pipeline.known_reads) == 2


def test_classification_time_insert_is_caught_by_fresh_read(offline_pipeline) -> None:
    dynamic_id = _id(8)

    def classify(item: str) -> ClassifyOutcome:
        activity_store.append_activities([{"dynamic_id": item}])
        return ClassifyOutcome(item, "互动抽奖", False)

    offline_pipeline.behavior["classify"] = classify
    result = pipeline.run_new_links_pipeline([_url(dynamic_id)], workers=1)
    assert result.persisted_count == 0
    assert result.duplicate_link_count == 1
    assert offline_pipeline.append_calls == []
    assert offline_pipeline.known_reads[0] == set()
    assert offline_pipeline.known_reads[1] == {dynamic_id}


def test_enrichment_time_insert_is_caught_by_writer(offline_pipeline) -> None:
    dynamic_id = _id(9)

    def enrich(item: str) -> _Activity:
        activity_store.append_activities([{"dynamic_id": item}])
        return _Activity(item)

    offline_pipeline.behavior["enrich"] = enrich
    result = pipeline.run_new_links_pipeline([_url(dynamic_id)], workers=1)
    assert result.persisted_count == 0
    assert result.persist_skipped_count == 1
    assert offline_pipeline.append_calls == [[dynamic_id]]
    assert len(offline_pipeline.known_reads) == 2
    assert activity_store.known_activity_ids() == {dynamic_id}


def test_partial_pipeline_keeps_successful_candidate(offline_pipeline) -> None:
    failing, successful = _id(10), _id(11)

    def classify(item: str) -> ClassifyOutcome:
        if item == failing:
            raise LlmTemporaryError("offline temporary failure")
        return ClassifyOutcome(item, "互动抽奖", False)

    offline_pipeline.behavior["classify"] = classify
    result = pipeline.run_new_links_pipeline([_url(failing), _url(successful)], workers=1)
    assert result.partial is True
    assert result.persisted_count == 1
    assert offline_pipeline.append_calls == [[successful]]
    assert len(offline_pipeline.known_reads) == 2


def test_empty_batch_uses_one_known_read(offline_pipeline) -> None:
    result = pipeline.run_new_links_pipeline([])
    assert result.pipeline_skipped is True
    assert offline_pipeline.append_calls == []
    assert len(offline_pipeline.known_reads) == 1


def test_large_batch_query_count_is_constant(offline_pipeline) -> None:
    existing = [_id(index) for index in range(100, 300)]
    new = [_id(index) for index in range(300, 450)]
    activity_store.append_activities([{"dynamic_id": item} for item in existing])
    offline_pipeline.behavior["classify"] = lambda item: ClassifyOutcome(
        item, "非抽奖活动", True, "非抽奖活动",
    )
    result = pipeline.run_new_links_pipeline([_url(item) for item in existing + new])
    assert result.existing_count == len(existing)
    assert result.new_link_count == len(new)
    assert len(offline_pipeline.classified) == len(new)
    assert len(offline_pipeline.known_reads) == 2


def test_profile_db_reads_remain_isolated(offline_pipeline, isolated_home: Path, monkeypatch) -> None:
    from src import data_paths
    from src.db.engine import reset_engine_for_tests
    from src.db.schema import init_db

    current = {"path": isolated_home / "profile-a" / "binggo.db"}
    monkeypatch.setattr(data_paths, "get_database_path", lambda profile_id=None: current["path"])
    reset_engine_for_tests()
    init_db()
    dynamic_id = _id(500)
    activity_store.append_activities([{"dynamic_id": dynamic_id}])
    result_a = pipeline.run_new_links_pipeline([_url(dynamic_id)], workers=1)
    assert result_a.existing_count == 1

    current["path"] = isolated_home / "profile-b" / "binggo.db"
    reset_engine_for_tests()
    init_db()
    result_b = pipeline.run_new_links_pipeline([_url(dynamic_id)], workers=1)
    assert result_b.existing_count == 0
    assert result_b.persisted_count == 1
    assert activity_store.known_activity_ids() == {dynamic_id}
    assert len(offline_pipeline.known_reads) == 3
