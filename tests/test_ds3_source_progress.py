from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline.refresh_all_pipeline import PipelineResult
from src.sources.common import CheckResult
from web import actions


SOURCE_MESSAGES = [
    "正在读取最新专栏…",
    "正在读取专栏正文…",
    "正在分析候选链接 (1/3)",
    "发现 2 个合集，正在展开 1/2",
    "部分合集读取失败：https://www.bilibili.com/opus/1220000000000000001 正文不可读",
    "正在整理活动链接…",
]


def _result(source_id: str = "DS-3", *, updated: bool = True) -> CheckResult:
    return CheckResult(
        source_id=source_id,
        updated=updated,
        container_url="https://www.bilibili.com/read/cv999001",
        container_id="999001",
        title="测试专栏",
        published_at=1,
        previous_container_url=None,
        activity_links=["https://www.bilibili.com/opus/1220000000000000002"],
        checked_at=2,
    )


def _pipeline_result() -> PipelineResult:
    return PipelineResult(
        ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
        classified_count=1, skipped_count=0, enriched_count=1, persisted_count=1,
        message="流水线完成",
    )


def _mock_writes(monkeypatch) -> None:
    monkeypatch.setattr(actions, "commit_source_checkpoint", lambda result: None)
    monkeypatch.setattr(actions, "invalidate_activity_cache", lambda: None)
    monkeypatch.setattr(actions, "set_last_pipeline_persisted", lambda **kwargs: None)


def test_ds3_check_progress_is_live_and_retained_in_source_payload() -> None:
    observed: list[tuple[int, int, str]] = []

    def check_update(*, force, on_progress):
        assert force is False
        for index, message in enumerate(SOURCE_MESSAGES):
            on_progress(index, len(SOURCE_MESSAGES), message)
            assert observed[-1] == (index, len(SOURCE_MESSAGES), message)
        return _result()

    _, payload, _, _ = actions._run_ds_check(
        1, "DS-3", check_update, lambda result: Path("ds3.json"),
        on_progress=lambda *event: observed.append(event),
    )

    assert payload["source_log_lines"] == [f"【DS-3】{message}" for message in SOURCE_MESSAGES]
    assert len(observed) == len(SOURCE_MESSAGES)


@pytest.mark.parametrize("source_id", ["DS-1", "DS-2", "DS-4", "DS-5", "DS-6", "DS-7"])
def test_other_sources_keep_existing_check_signature(source_id) -> None:
    def check_update(*, force):
        assert force is False
        return _result(source_id)

    observed = []
    _, payload, _, _ = actions._run_ds_check(
        1, source_id, check_update, lambda result: None,
        on_progress=lambda *args: observed.append(args),
    )

    assert observed == []
    assert payload["source_log_lines"] == []


@pytest.mark.parametrize("with_progress", [False, True])
def test_refresh_source_retains_partial_warning_after_success(monkeypatch, with_progress) -> None:
    events: list[dict] = []
    pipeline_calls: list[str] = []

    def check_update(*, force, on_progress):
        for index, message in enumerate(SOURCE_MESSAGES):
            on_progress(index, len(SOURCE_MESSAGES), message)
            if with_progress:
                assert events[-1]["log_append"] == f"【DS-3】{message}"
                assert events[-1]["step"] == 1
                assert events[-1]["total"] == actions.REFRESH_SOURCE_TOTAL
                assert pipeline_calls == []
        return _result()

    def run_pipeline(results, *, on_progress):
        pipeline_calls.append("called")
        assert len(results) == 1
        return _pipeline_result()

    monkeypatch.setattr(actions, "DS_HANDLER_BY_ID", {"DS-3": (check_update, lambda result: None)})
    monkeypatch.setattr(actions, "run_refresh_all_pipeline", run_pipeline)
    _mock_writes(monkeypatch)

    payload = actions.run_action(
        "refresh_source", {"source_id": "DS-3"},
        on_progress=(lambda **event: events.append(event)) if with_progress else None,
    )

    assert payload["ok"] is True
    assert pipeline_calls == ["called"]
    for message in SOURCE_MESSAGES:
        assert payload["log"].count(message) == 1


def test_parallel_refresh_all_forwards_ds3_progress_and_preserves_final_log(monkeypatch) -> None:
    events: list[dict] = []
    pipeline_calls: list[str] = []

    def ds3_check(*, force, on_progress):
        for index, message in enumerate(SOURCE_MESSAGES):
            on_progress(index, len(SOURCE_MESSAGES), message)
            assert events[-1]["log_append"] == f"【DS-3】{message}"
            assert events[-1]["step"] == 0
            assert events[-1]["total"] == actions.REFRESH_ALL_TOTAL
            assert pipeline_calls == []
        return _result()

    def ds2_check(*, force):
        return _result("DS-2", updated=False)

    def run_pipeline(results, *, on_progress):
        pipeline_calls.append("called")
        assert [result.source_id for result in results] == ["DS-2", "DS-3"]
        return _pipeline_result()

    monkeypatch.setattr(actions, "DS_HANDLERS", [
        ("DS-2", ds2_check, lambda result: None),
        ("DS-3", ds3_check, lambda result: None),
    ])
    monkeypatch.setattr(actions, "run_refresh_all_pipeline", run_pipeline)
    _mock_writes(monkeypatch)

    payload = actions.run_action("refresh_all", on_progress=lambda **event: events.append(event))

    assert payload["ok"] is True
    assert pipeline_calls == ["called"]
    for message in SOURCE_MESSAGES:
        assert payload["log"].count(message) == 1
    assert "【DS-2】同一专栏，已跳过" in payload["log"]


def test_unchanged_ds3_keeps_source_messages_when_pipeline_is_skipped(monkeypatch) -> None:
    def check_update(*, force, on_progress):
        on_progress(0, 0, "正在读取最新专栏…")
        return _result(updated=False)

    monkeypatch.setattr(actions, "DS_HANDLER_BY_ID", {"DS-3": (check_update, lambda result: None)})
    _mock_writes(monkeypatch)
    payload = actions.run_action("refresh_source", {"source_id": "DS-3"})

    assert payload["ok"] is True
    assert payload["result"]["pipeline_skipped"] is True
    assert "【DS-3】正在读取最新专栏…" in payload["log"]
