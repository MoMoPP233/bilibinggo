from __future__ import annotations

from pathlib import Path

from src.ds9_rejection_audit import MAX_AUDIT_SAMPLES, clear_rejections, list_rejections, record_rejection


def _record(dynamic_id: str, *, topic_id: str = "47801", seen_at: int = 1) -> None:
    record_rejection(
        dynamic_id=dynamic_id,
        canonical_url=f"https://www.bilibili.com/opus/{dynamic_id}",
        source_topic_id=topic_id,
        source_topic_name={
            "1261901": "抽奖！！！",
            "1055656": "转发动态抽奖！",
            "47801": "抽奖号的日常",
        }[topic_id],
        classifier_text=f"最终分类文本 {dynamic_id}",
        seen_at=seen_at,
    )


def test_audit_upsert_capacity_and_clear_are_profile_local(isolated_home: Path) -> None:
    for index in range(MAX_AUDIT_SAMPLES + 1):
        _record(str(10**18 + index), seen_at=index + 1)

    rows = list_rejections()
    assert len(rows) == MAX_AUDIT_SAMPLES
    assert rows[0]["dynamic_id"] == str(10**18 + MAX_AUDIT_SAMPLES)
    assert str(10**18) not in {row["dynamic_id"] for row in rows}

    _record(str(10**18 + MAX_AUDIT_SAMPLES), seen_at=999)
    rows = list_rejections()
    assert len(rows) == MAX_AUDIT_SAMPLES
    assert rows[0]["last_seen_at"] == 999

    assert clear_rejections() == MAX_AUDIT_SAMPLES
    assert list_rejections() == []


def test_audit_topic_filter_and_three_builtin_names(isolated_home: Path) -> None:
    _record("100000000000000001", topic_id="1261901")
    _record("100000000000000002", topic_id="1055656")
    _record("100000000000000003", topic_id="47801")

    rows = list_rejections(source_topic_id="47801")
    assert len(rows) == 1
    assert rows[0]["source_topic_name"] == "抽奖号的日常"
    assert rows[0]["classification_result"] == "非抽奖活动"


def test_audit_record_keeps_classifier_text_and_deduplicates(isolated_home: Path) -> None:
    _record("100000000000000004", seen_at=10)
    record_rejection(
        dynamic_id="100000000000000004",
        canonical_url="https://www.bilibili.com/opus/100000000000000004",
        source_topic_id="47801",
        source_topic_name="抽奖号的日常",
        classifier_text="normalize 后送入 classifier 的最终文本",
        seen_at=20,
    )

    rows = list_rejections()
    assert len(rows) == 1
    assert rows[0]["classifier_text"] == "normalize 后送入 classifier 的最终文本"
    assert rows[0]["first_seen_at"] == 10
    assert rows[0]["last_seen_at"] == 20


def test_pipeline_audit_is_side_effect_only_and_keeps_non_lottery_result(
    isolated_home: Path,
    monkeypatch,
) -> None:
    from src.pipeline.classify_step import ClassifyOutcome
    from src.pipeline.refresh_all_pipeline import run_new_links_pipeline

    dynamic_id = "100000000000000005"

    class FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.known_activity_ids", lambda: set()
    )
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.load_participations", lambda: {})
    monkeypatch.setattr("src.pipeline.refresh_all_pipeline.BilibiliClient", FakeClient)
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.classify_new_link",
        lambda _client, did: ClassifyOutcome(
            str(did),
            "非抽奖活动",
            True,
            "非抽奖活动",
            classify_content="normalize 后送入 classifier 的最终文本",
        ),
    )

    result = run_new_links_pipeline(
        [f"https://www.bilibili.com/opus/{dynamic_id}"],
        workers=1,
        source_context_by_dynamic_id={
            dynamic_id: {"topic_id": "47801", "topic_name": "抽奖号的日常"}
        },
    )

    assert result.non_lottery_count == 1
    assert result.persisted_count == 0
    assert list_rejections()[0]["source_topic_id"] == "47801"
    assert list_rejections()[0]["classifier_text"] == "normalize 后送入 classifier 的最终文本"
