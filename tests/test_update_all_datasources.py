"""V7 一键更新全部数据源：串行编排层测试（复用现有单源逻辑，无真实网络）。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import web.actions as actions
from src.pipeline.refresh_all_pipeline import PipelineResult
from src.sources.common import CheckResult

DS_ALL = [f"DS-{n}" for n in range(1, 8)]


def _noop_progress(**kwargs) -> None:
    return None


def _check_result(source_id: str, *, updated: bool = True) -> CheckResult:
    return CheckResult(
        source_id=source_id,
        updated=updated,
        container_url=f"https://example.com/{source_id}",
        container_id=f"{source_id}-id",
        title="标题",
        published_at=1,
        previous_container_url=None,
        activity_links=[],
        checked_at=1,
    )


def _save_result(_result: CheckResult) -> Path:
    return Path("/tmp/out")


def _make_record_check(sequence: list[str], source_id: str, *, updated: bool = False):
    def check_update(*, force=False) -> CheckResult:
        sequence.append(f"check:{source_id}")
        return _check_result(source_id, updated=updated)

    return check_update


def _fake_handlers(sequence: list[str], *, updated=True, fail_on=None, error=None):
    handlers = []
    for source_id in DS_ALL:
        def make(source_id, *, fail_on=fail_on, error=error):
            def check_update(*, force=False, **kwargs) -> CheckResult:
                sequence.append(f"check:{source_id}")
                if fail_on == source_id:
                    raise RuntimeError(error or f"{source_id} 内容解析异常")
                return _check_result(source_id, updated=updated)

            return check_update, _save_result

        handlers.append((source_id, *make(source_id)))
    return handlers


def _run_all(handlers, *, cancel_event=None):
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = handlers
    try:
        return actions.run_action(
            "update_all_datasources",
            {},
            on_progress=_noop_progress,
            cancel_event=cancel_event,
        )
    finally:
        actions.DS_HANDLERS = original


def test_update_all_runs_ds1_to_ds7_serially(isolated_home: Path) -> None:
    sequence: list[str] = []
    payload = _run_all(_fake_handlers(sequence, updated=False))

    assert payload["ok"] is True
    assert sequence == [f"check:{sid}" for sid in DS_ALL]
    result = payload["result"]["update_all"]
    assert result["success_count"] == 7
    assert result["failed_count"] == 0
    assert [entry["source_id"] for entry in result["sources"]] == DS_ALL
    assert all(entry["status"] == "success" for entry in result["sources"])


def test_import_of_previous_source_finishes_before_next_starts(
    isolated_home: Path, monkeypatch
) -> None:
    """DS-1 必须完成「导入」后，DS-2 的 check 才开始；不并发。"""
    sequence: list[str] = []
    events: list[str] = []
    import_done = {"value": False}

    def check_a(*, force=False) -> CheckResult:
        events.append("check:DS-1")
        return _check_result("DS-1", updated=True)

    def check_b(*, force=False) -> CheckResult:
        assert import_done["value"] is True, "DS-2 不得在 DS-1 导入完成前开始"
        events.append("check:DS-2")
        return _check_result("DS-2", updated=True)

    def fake_pipeline(ds_results, *, workers=4, on_progress=None) -> PipelineResult:
        source_id = ds_results[0].source_id
        events.append(f"pipeline:{source_id}")
        if source_id == "DS-1":
            import_done["value"] = True
            return PipelineResult(
                ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
                classified_count=1, skipped_count=0, enriched_count=1,
                persisted_count=12, message="新入库 12 条",
            )
        return PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
            classified_count=1, skipped_count=0, enriched_count=1,
            persisted_count=3, message="新入库 3 条",
        )

    monkeypatch.setattr(actions, "run_refresh_all_pipeline", fake_pipeline)
    handlers = [("DS-1", check_a, _save_result), ("DS-2", check_b, _save_result)]
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = handlers
    try:
        payload = actions.run_action(
            "update_all_datasources",
            {},
            on_progress=_noop_progress,
            cancel_event=None,
        )
    finally:
        actions.DS_HANDLERS = original

    assert payload["ok"] is True
    assert events == ["check:DS-1", "pipeline:DS-1", "check:DS-2", "pipeline:DS-2"]
    sources = payload["result"]["update_all"]["sources"]
    assert [entry["persisted_count"] for entry in sources] == [12, 3]


def test_ordinary_failure_is_recorded_and_next_source_continues(
    isolated_home: Path,
) -> None:
    sequence: list[str] = []
    handlers = _fake_handlers(sequence, updated=False, fail_on="DS-2", error="正文格式变化")
    payload = _run_all(handlers)

    assert payload["ok"] is True
    sources = payload["result"]["update_all"]["sources"]
    by_id = {entry["source_id"]: entry for entry in sources}
    assert by_id["DS-1"]["status"] == "success"
    assert by_id["DS-2"]["status"] == "failed"
    assert "正文格式变化" in (by_id["DS-2"]["error"] or "")
    assert by_id["DS-3"]["status"] == "success"
    assert payload["result"]["update_all"]["success_count"] == 6
    assert payload["result"]["update_all"]["failed_count"] == 1
    # DS-3 之后仍继续执行到 DS-7
    assert sequence == [f"check:{sid}" for sid in DS_ALL]


@pytest.mark.parametrize("message", ["API error -352: 风控", "-509 访问过于频繁", "HTTP 429 Too Many Requests"])
def test_platform_risk_stops_remaining_sources(
    isolated_home: Path, message: str,
) -> None:
    sequence: list[str] = []
    handlers = _fake_handlers(sequence, updated=False, fail_on="DS-3", error=message)
    payload = _run_all(handlers)

    assert payload["ok"] is False
    result = payload["result"]["update_all"]
    assert result["risk_stopped"] is True
    by_id = {entry["source_id"]: entry for entry in result["sources"]}
    assert by_id["DS-1"]["status"] == "success"
    assert by_id["DS-2"]["status"] == "success"
    assert by_id["DS-3"]["status"] == "risk"
    assert by_id["DS-4"]["status"] == "not_run"
    assert by_id["DS-7"]["status"] == "not_run"
    # DS-4 及以后不得开始执行
    assert sequence == ["check:DS-1", "check:DS-2", "check:DS-3"]
    # 已完成源的数据保留（作为成功记录返回，未回滚）
    assert result["success_count"] == 2


def test_cancel_keeps_finished_sources_and_marks_rest_not_run(
    isolated_home: Path,
) -> None:
    cancel_event = threading.Event()
    sequence: list[str] = []

    def check_second(*, force=False) -> CheckResult:
        sequence.append("check:DS-2")
        cancel_event.set()
        return _check_result("DS-2", updated=False)

    handlers = [
        ("DS-1", _make_record_check(sequence, "DS-1"), _save_result),
        ("DS-2", check_second, _save_result),
        ("DS-3", lambda *, force=False: (_ for _ in ()).throw(AssertionError("不应执行 DS-3")), _save_result),
    ]
    original = actions.DS_HANDLERS
    actions.DS_HANDLERS = handlers
    try:
        payload = actions.run_action(
            "update_all_datasources",
            {},
            on_progress=_noop_progress,
            cancel_event=cancel_event,
        )
    finally:
        actions.DS_HANDLERS = original

    assert payload["ok"] is False
    assert payload["cancelled"] is True
    result = payload["result"]["update_all"]
    by_id = {entry["source_id"]: entry for entry in result["sources"]}
    assert by_id["DS-1"]["status"] == "success"
    assert by_id["DS-2"]["status"] == "stopped"
    assert by_id["DS-3"]["status"] == "not_run"
    assert sequence == ["check:DS-1", "check:DS-2"]


def test_update_all_rejects_extra_params(isolated_home: Path) -> None:
    with pytest.raises(ValueError, match="不接受额外参数"):
        actions.run_action(
            "update_all_datasources",
            {"source_id": "DS-1"},
            on_progress=_noop_progress,
            cancel_event=None,
        )


def test_update_all_result_reports_aggregate_persisted(isolated_home: Path, monkeypatch) -> None:
    sequence: list[str] = []
    monkeypatch.setattr(
        actions,
        "run_refresh_all_pipeline",
        lambda ds_results, *, workers=4, on_progress=None: PipelineResult(
            ok=True, pipeline_skipped=False, raw_link_count=1, new_link_count=1,
            classified_count=1, skipped_count=0, enriched_count=1,
            persisted_count=5, message="新入库 5 条",
        ),
    )
    payload = _run_all(_fake_handlers(sequence, updated=True))

    assert payload["ok"] is True
    result = payload["result"]["update_all"]
    assert result["persisted_count"] == 35  # 7 * 5
    assert result["success_count"] == 7
