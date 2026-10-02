from __future__ import annotations

import json
import time
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from src.bilibili_rate_limit import BilibiliRateLimitAcquisition
from src.job_store import dumps_result_json, get_job
from src.observability import (
    JobMetrics,
    current_metrics,
    format_performance_summary,
    metrics_context,
    performance_metrics,
    request_kind,
    sleep_with_bili_retry_metrics,
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
    metrics.record_bilibili_rate_limit_config(0.5, "env")
    metrics.record_bilibili_rate_limit_wait(1_500)
    metrics.record_bilibili_client_lock_wait(20)
    metrics.record_bilibili_retry_wait(1_200)
    metrics.record_llm_wait(30)
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
    assert snapshot["bili_rps_effective"] == 0.5
    assert snapshot["bili_rps_source"] == "env"
    assert snapshot["bili_rate_limit_wait_count"] == 1
    assert snapshot["bili_rate_limit_wait_ms"] == 1_500
    assert snapshot["bili_rate_limit_wait_max_ms"] == 1_500
    assert snapshot["bili_client_lock_wait_count"] == 1
    assert snapshot["bili_retry_wait_count"] == 1
    assert snapshot["llm_wait_count"] == 1
    serialized = str(snapshot)
    assert "secret" not in serialized


def test_http_duration_excludes_rate_limit_wait(monkeypatch) -> None:
    import src.bilibili_client as client_module

    clock = {"now": 0.0}

    def perf_counter() -> float:
        return clock["now"]

    def acquire_slot() -> BilibiliRateLimitAcquisition:
        clock["now"] += 1.5
        return BilibiliRateLimitAcquisition(0.5, "env", True, 1_500)

    class Response:
        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(
        client_module, "time", SimpleNamespace(perf_counter=perf_counter)
    )
    monkeypatch.setattr(
        client_module, "acquire_bilibili_request_slot", acquire_slot
    )
    client = client_module.BilibiliClient(warmup=False)

    def fake_get(*args, **kwargs):
        clock["now"] += 0.2
        return Response()

    monkeypatch.setattr(client._client, "get", fake_get)
    metrics = JobMetrics()
    with metrics_context(metrics):
        client._http_get("https://api.bilibili.com/x/polymer/web-dynamic/v1/detail")
    client.close()

    snapshot = metrics.snapshot()
    assert snapshot["bili_rate_limit_wait_ms"] == 1_500
    assert 190 <= snapshot["bilibili_request_ms"] <= 200
    assert snapshot["bili_rps_effective"] == 0.5
    assert snapshot["bili_rps_source"] == "env"


def test_client_lock_records_only_real_contention(monkeypatch) -> None:
    import src.bilibili_client as client_module

    clock = {"now": 0.0}

    class ContendedLock:
        def acquire(self, blocking=True):
            if not blocking:
                return False
            clock["now"] += 0.025
            return True

        def release(self) -> None:
            return None

    monkeypatch.setattr(
        client_module,
        "time",
        SimpleNamespace(perf_counter=lambda: clock["now"]),
    )
    client = client_module.BilibiliClient(warmup=False)
    client._http_lock = ContendedLock()
    metrics = JobMetrics()
    with metrics_context(metrics):
        client._acquire_http_lock()
    client.close()

    snapshot = metrics.snapshot()
    assert snapshot["bili_client_lock_wait_count"] == 1
    assert snapshot["bili_client_lock_wait_ms"] == 25
    assert snapshot["bili_client_lock_wait_max_ms"] == 25


def test_retry_wait_uses_real_elapsed_time_with_fake_clock(monkeypatch) -> None:
    import src.observability as observability

    clock = {"now": 0.0}
    monkeypatch.setattr(
        observability,
        "time",
        SimpleNamespace(perf_counter=lambda: clock["now"]),
    )

    def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds

    metrics = JobMetrics()
    with metrics_context(metrics):
        sleep_with_bili_retry_metrics(1.2, sleeper=fake_sleep)
        sleep_with_bili_retry_metrics(2.0, sleeper=fake_sleep)

    snapshot = metrics.snapshot()
    assert snapshot["bili_retry_wait_count"] == 2
    assert snapshot["bili_retry_wait_ms"] == 3_200


def test_llm_semaphore_wait_is_aggregated(monkeypatch) -> None:
    import src.llm_client as llm_client

    clock = {"now": 0.0}

    class ContendedSemaphore:
        def acquire(self, blocking=True):
            if not blocking:
                return False
            clock["now"] += 0.03
            return True

        def release(self) -> None:
            return None

    monkeypatch.setattr(llm_client, "_llm_lock", ContendedSemaphore())
    monkeypatch.setattr(
        llm_client,
        "time",
        SimpleNamespace(perf_counter=lambda: clock["now"]),
    )
    metrics = JobMetrics()
    with metrics_context(metrics):
        llm_client._acquire_llm_slot()
        llm_client._llm_lock.release()

    snapshot = metrics.snapshot()
    assert snapshot["llm_wait_count"] == 1
    assert snapshot["llm_wait_ms"] == 30


def test_performance_metrics_exposes_stable_job_result_contract() -> None:
    snapshot = {
        "pipeline_ms": 1_300_000,
        "feed_fetch_ms": 40_000,
        "classification_stage_ms": 1_260_000,
        "bilibili_request_count": 252,
        "bilibili_request_ms": 440_000,
        "bili_rps_effective": 0.5,
        "bili_rps_source": "env",
        "bili_rate_limit_wait_count": 250,
        "bili_rate_limit_wait_ms": 860_000,
        "bili_rate_limit_wait_max_ms": 2_000,
        "bili_client_lock_wait_count": 2,
        "bili_client_lock_wait_ms": 10,
        "bili_client_lock_wait_max_ms": 8,
        "bili_retry_wait_count": 3,
        "bili_retry_wait_ms": 6_000,
        "requests": {
            "detail": {"count": 160, "ms": 300_000},
            "notice": {"count": 80, "ms": 120_000},
            "body": {"count": 12, "ms": 20_000},
        },
        "llm": {"calls": 60, "cache_hits": 7, "ms": 900_000},
        "llm_wait_count": 4,
        "llm_wait_ms": 500,
        "database_save_ms": 20,
    }

    public = performance_metrics(snapshot)

    assert public == {
        "pipeline_duration_ms": 1_300_000,
        "feed_fetch_duration_ms": 40_000,
        "classify_duration_ms": 1_260_000,
        "bili_request_count": 252,
        "bili_request_duration_ms": 440_000,
        "bili_rps_effective": 0.5,
        "bili_rps_source": "env",
        "bili_rate_limit_wait_count": 250,
        "bili_rate_limit_wait_ms": 860_000,
        "bili_rate_limit_wait_max_ms": 2_000,
        "bili_client_lock_wait_count": 2,
        "bili_client_lock_wait_ms": 10,
        "bili_client_lock_wait_max_ms": 8,
        "bili_retry_wait_count": 3,
        "bili_retry_wait_ms": 6_000,
        "detail_requests": 160,
        "detail_duration_ms": 300_000,
        "notice_requests": 80,
        "notice_duration_ms": 120_000,
        "body_requests": 12,
        "body_duration_ms": 20_000,
        "llm_calls": 60,
        "llm_cache_hits": 7,
        "llm_duration_ms": 900_000,
        "llm_wait_count": 4,
        "llm_wait_ms": 500,
        "db_save_duration_ms": 20,
    }
    summary = format_performance_summary(public)
    assert "性能：Pipeline 21m40s" in summary
    assert "Detail 160次/5m00s" in summary
    assert "B站请求 252次/7m20s" in summary
    assert "限流 0.5 RPS（env），令牌等待 250次/14m20s" in summary
    assert "Retry等待 3次/6s" in summary
    assert "LLM 60次/15m00s（缓存命中 7，等待 4次/500ms）" in summary


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


def test_success_performance_summary_is_written_once_to_daily_log(
    isolated_home: Path, monkeypatch
) -> None:
    from src import app_logging

    app_logging.reset_logging_for_tests()
    monkeypatch.setattr(app_logging, "DATA_DIR", isolated_home)
    path = app_logging.setup_logging(console=False)
    runner = JobRunner()

    def fake_action(action, params, *, on_progress, cancel_event):
        metrics = current_metrics()
        assert metrics is not None
        metrics.record_stage_duration("Pipeline总耗时", 1_000)
        return {"ok": True, "message": "done", "result": {}, "log": "done"}

    try:
        with patch("web.job_runner.run_action", side_effect=fake_action):
            assert runner.try_start("refresh_status") is not None
            status = _wait_for_terminal(runner)
        assert status.state == "success"
    finally:
        app_logging.reset_logging_for_tests()

    records = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    summaries = [item for item in records if item.get("event") == "job.performance"]
    assert len(summaries) == 1
    assert summaries[0]["msg"].startswith("性能：Pipeline")


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
