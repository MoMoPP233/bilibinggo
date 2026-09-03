"""本轮扫描统计口径测试：只复用已有数据流，不增加远程请求。"""

from __future__ import annotations

from src.lottery_enricher import EnrichedActivity, EnrichSkippedError
from src.pipeline.classify_step import ClassifyOutcome
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline

SCAN = 1_700_000_000
BASE = 1_000_000_000_000_000_000


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _url(dynamic_id: str, *, legacy: bool = False) -> str:
    if legacy:
        return f"https://t.bilibili.com/{dynamic_id}"
    return f"https://www.bilibili.com/opus/{dynamic_id}"


def _activity(dynamic_id: str, *, expired: bool = False) -> EnrichedActivity:
    return EnrichedActivity(
        dynamic_id=dynamic_id,
        source_url=_url(dynamic_id),
        lottery_type="互动抽奖",
        enriched_at=SCAN,
        business_id=dynamic_id,
        business_type=1,
        draw_status="active",
        lottery_time=SCAN - 1 if expired else SCAN + 100,
        prizes=[],
        participants=0,
        conditions={},
        winners=None,
        platform_participated=None,
    )


def test_scan_counts_are_exclusive_complete_and_use_no_extra_remote_calls(
    monkeypatch,
) -> None:
    existing = str(BASE + 1)
    non_lottery = str(BASE + 2)
    charging = str(BASE + 3)
    unreadable = str(BASE + 4)
    expired = str(BASE + 5)
    active = str(BASE + 6)
    enrich_failed = str(BASE + 7)
    raw_urls = [
        _url(existing),
        _url(existing, legacy=True),
        _url(non_lottery),
        _url(charging),
        _url(unreadable),
        _url(expired),
        _url(active),
        _url(enrich_failed),
        _url(active, legacy=True),
        "https://example.com/not-a-dynamic",
    ]
    classify_calls: list[str] = []
    enrich_calls: list[str] = []
    appended: list[dict] = []

    def classify(_client, dynamic_id):
        dynamic_id = str(dynamic_id)
        classify_calls.append(dynamic_id)
        if dynamic_id == non_lottery:
            return ClassifyOutcome(dynamic_id, "非抽奖活动", True, "非抽奖活动")
        if dynamic_id == charging:
            return ClassifyOutcome(dynamic_id, "充电抽奖", True, "充电抽奖")
        if dynamic_id == unreadable:
            raise RuntimeError("无法获取动态正文: 所有正文接口均失败")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False)

    def enrich(_client, *, dynamic_id, **_kwargs):
        dynamic_id = str(dynamic_id)
        enrich_calls.append(dynamic_id)
        if dynamic_id == enrich_failed:
            raise EnrichSkippedError(dynamic_id)
        return _activity(dynamic_id, expired=dynamic_id == expired)

    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: {existing}
    )
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", _FakeClient)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.classify_new_link", classify)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.enrich_activity", enrich)
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.append_activities",
        lambda rows: appended.extend(rows) or len(rows),
    )

    result = run_new_links_pipeline(
        raw_urls,
        workers=1,
        scan_started_at=SCAN,
    )

    assert result.discovered_count == 10
    assert result.invalid_link_count == 1
    assert result.duplicate_link_count == 2
    assert result.existing_count == 1
    assert result.candidate_count == 6
    assert result.non_lottery_count == 1
    assert result.other_skipped_count == 1
    assert result.failed_count == 2
    assert result.expired_skipped_count == 1
    assert result.persist_skipped_count == 0
    assert result.persisted_count == 1
    assert result.to_dict()["discovered_count"] == 10
    assert result.to_dict()["candidate_count"] == 6
    assert result.discovered_count == (
        result.invalid_link_count
        + result.duplicate_link_count
        + result.existing_count
        + result.candidate_count
    )
    assert result.candidate_count == (
        result.non_lottery_count
        + result.other_skipped_count
        + result.failed_count
        + result.expired_skipped_count
        + result.persist_skipped_count
        + result.persisted_count
    )
    assert set(classify_calls) == {
        non_lottery,
        charging,
        unreadable,
        expired,
        active,
        enrich_failed,
    }
    assert set(enrich_calls) == {expired, active, enrich_failed}
    assert [row["dynamic_id"] for row in appended] == [active]


def test_large_existing_database_only_processes_unique_new_candidates(monkeypatch) -> None:
    existing = [str(BASE + index) for index in range(1, 301)]
    candidates = [str(BASE + index) for index in range(301, 501)]
    classify_calls: list[str] = []

    def classify(_client, dynamic_id):
        classify_calls.append(str(dynamic_id))
        return ClassifyOutcome(str(dynamic_id), "非抽奖活动", True, "非抽奖活动")

    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set(existing)
    )
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", _FakeClient)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.classify_new_link", classify)

    result = run_new_links_pipeline([_url(item) for item in existing + candidates])

    assert result.discovered_count == 500
    assert result.existing_count == 300
    assert result.candidate_count == 200
    assert result.non_lottery_count == 200
    assert result.persisted_count == 0
    assert classify_calls == candidates


def test_write_time_race_is_not_misreported_as_initially_existing(monkeypatch) -> None:
    dynamic_id = str(BASE + 900)
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set())
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", _FakeClient)
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.classify_new_link",
        lambda _client, did: ClassifyOutcome(str(did), "互动抽奖", False),
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.enrich_activity",
        lambda _client, *, dynamic_id, **_kwargs: _activity(str(dynamic_id)),
    )
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.append_activities", lambda rows: 0)

    result = run_new_links_pipeline([_url(dynamic_id)], scan_started_at=SCAN)

    assert result.existing_count == 0
    assert result.candidate_count == 1
    assert result.persist_skipped_count == 1
    assert result.persisted_count == 0
