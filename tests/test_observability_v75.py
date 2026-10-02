from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from src.job_store import dumps_result_json, get_job
from src.observability import (
    JobMetrics,
    current_metrics,
    format_performance_summary,
    metrics_context,
    performance_metrics,
    request_kind,
)
from web.job_runner import JobRunner


def test_request_kind_uses_endpoint_path_without_sensitive_query() -> None:
    assert request_kind(
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail?id=secret"
    ) == "detail"
    assert request_kind(
        "https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice?business_id=secret"
    ) == "notice"
    assert request_kind("https://www.bilibili.com/opus/123456") == "body"
    assert request_kind("https://api.bilibili.com/x/web-interface/nav") == "other"


def test_metrics_snapshot_aggregates_request_phases_and_llm_cache_hits() -> None:
    metrics = JobMetrics()
    metrics.record_bilibili_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", 12, kind="detail")
    metrics.record_bilibili_request("https://api.vc.bilibili.com/lottery_svr/v1/lottery_svr/lottery_notice", 23, kind="notice")
    metrics.record_bilibili_request("https://www.bilibili.com/opus/123", 34, kind="body")
    metrics.record_llm_call(45)
    metrics.record_llm_cache_hit()
    metrics.record_stage_duration("分类阶段", 56)
    metrics.record_stage_duration("Pipeline总耗时", 78)

    snapshot = metrics.snapshot()

    assert snapshot["pipeline_ms"] == 78
    assert snapshot["classification_stage_ms"] == 56
    assert snapshot["requests"] == {
        "detail": {"count": 1, "ms": 12},
        "notice": {"count": 1, "ms": 23},
        "body": {"count": 1, "ms": 34},
    }
    assert snapshot["llm"] == {"calls": 1, "cache_hits": 1, "ms": 45}
    serialized = str(snapshot)
    assert "secret" not in serialized


def test_performance_metrics_exposes_stable_job_result_contract() -> None:
    snapshot = {
        "pipeline_ms": 1_300_000,
        "feed_fetch_ms": 40_000,
        "classification_stage_ms": 1_260_000,
        "requests": {
            "detail": {"count": 160, "ms": 300_000},
            "notice": {"count": 80, "ms": 120_000},
            "body": {"count": 12, "ms": 20_000},
        },
        "llm": {"calls": 60, "cache_hits": 7, "ms": 900_000},
        "database_save_ms": 20,
    }

    public = performance_metrics(snapshot)

    assert public == {
        "pipeline_duration_ms": 1_300_000,
        "feed_fetch_duration_ms": 40_000,
        "classify_duration_ms": 1_260_000,
        "detail_requests": 160,
        "detail_duration_ms": 300_000,
        "notice_requests": 80,
        "notice_duration_ms": 120_000,
        "body_requests": 12,
        "body_duration_ms": 20_000,
        "llm_calls": 60,
        "llm_cache_hits": 7,
        "llm_duration_ms": 900_000,
        "db_save_duration_ms": 20,
    }
    summary = format_performance_summary(public)
    assert "性能：Pipeline 21m40s" in summary
    assert "Detail 160次/5m00s" in summary
    assert "LLM 60次/15m00s（缓存命中 7）" in summary


def _wait_for_terminal(runner: JobRunner, timeout: float = 3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = runner.get_status()
        if status.state != "running":
            return status
        time.sleep(0.01)
    raise AssertionError("Job did not reach a terminal state")


def test_success_job_persists_performance_metrics_and_summary(
    isolated_home: Path,
) -> None:
    _ = isolated_home
    runner = JobRunner()

    def fake_action(action, params, *, on_progress, cancel_event):
        metrics = current_metrics()
        assert metrics is not None
        metrics.record_stage_duration("Pipeline总耗时", 65_000)
        metrics.record_stage_duration("分类阶段", 61_000)
        metrics.record_bilibili_request("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail", 500, kind="detail")
        metrics.record_llm_call(2_000)
        return {"ok": True, "message": "done", "result": {"business": "unchanged"}, "log": "done"}

    with patch("web.job_runner.run_action", side_effect=fake_action):
        job_id = runner.try_start("refresh_status")
        assert job_id is not None
        status = _wait_for_terminal(runner)

    assert status.state == "success"
    assert status.result["business"] == "unchanged"
    assert status.result["performance_metrics"]["pipeline_duration_ms"] == 65_000
    assert "性能：Pipeline 1m05s" in status.log
    persisted = get_job(job_id)
    assert persisted is not None
    assert persisted["result"]["performance_metrics"] == status.result["performance_metrics"]


def test_failed_job_does_not_publish_success_performance_metrics(
    isolated_home: Path,
) -> None:
    _ = isolated_home
    runner = JobRunner()

    def fake_action(action, params, *, on_progress, cancel_event):
        metrics = current_metrics()
        assert metrics is not None
        metrics.record_stage_duration("Pipeline总耗时", 10_000)
        return {"ok": False, "message": "failed", "result": {"reason": "business"}, "log": "failed"}

    with patch("web.job_runner.run_action", side_effect=fake_action):
        job_id = runner.try_start("refresh_status")
        assert job_id is not None
        status = _wait_for_terminal(runner)

    assert status.state == "error"
    assert "performance_metrics" not in (status.result or {})
    assert "性能：" not in status.log


def test_oversized_job_result_keeps_performance_metrics() -> None:
    metrics = {"pipeline_duration_ms": 1234, "llm_calls": 2}
    encoded = dumps_result_json({"payload": "x" * 70_000, "performance_metrics": metrics})
    assert encoded is not None
    assert '"_truncated":true' in encoded
    assert '"performance_metrics"' in encoded


def test_forward_parser_cache_hit_is_counted_without_provider_call(monkeypatch) -> None:
    import src.forward_parser as parser

    monkeypatch.setattr(
        parser,
        "get_cached_classify",
        lambda dynamic_id, content: {
            "classify_parser_version": parser.CLASSIFY_PARSER_VERSION,
            "is_lottery": False,
            "confidence": "high",
        },
    )
    monkeypatch.setattr(parser, "chat_json", lambda **kwargs: (_ for _ in ()).throw(AssertionError()))

    metrics = JobMetrics()
    with metrics_context(metrics):
        result = parser.classify_forward_lottery("123", "cached content")

    assert result["from_cache"] is True
    assert metrics.snapshot()["llm_cache_hits"] == 1
