"""Offline DELETE result contract, including Job/REST/SSE serialization."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from src import repost_cleanup as cleanup
from src.job_store import get_job
from web import actions
from web.app import app
from web.event_hub import event_hub
from web.job_runner import JobRunner
from web.sse import format_sse

IDS = [f"50000000000000000{n:02d}" for n in range(1, 21)]
ORIGINALS = {rid: f"60000000000000000{n:02d}" for n, rid in enumerate(IDS, 1)}
CONTRACT = json.loads((Path(__file__).parent / "fixtures" / "v87_delete_risk_20.json").read_text(encoding="utf-8"))


class FakeClient:
    def __init__(self):
        self.calls = []
        self.outcomes = {}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def post_json(self, url, payload, *, params, referer, retries, raise_on_code):
        rid = payload["dyn_id_str"]
        self.calls.append((rid, retries))
        assert rid in IDS and rid not in ORIGINALS.values()
        assert retries == 0
        outcome = self.outcomes.get(rid, {"code": 0})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def delete_env(isolated_home, monkeypatch):
    client = FakeClient()
    marks = []
    claims = []
    verification_risk = {"index": None}
    monkeypatch.setattr(cleanup, "get_runtime_profile_id", lambda: "account-1")
    monkeypatch.setattr(cleanup, "_require_verified_login", lambda _client: ("csrf", 123))
    monkeypatch.setattr(cleanup, "_assert_frozen_runtime", lambda **_: "csrf")
    monkeypatch.setattr(cleanup, "load_activities", lambda: [])
    monkeypatch.setattr(cleanup, "get_repost", lambda uid, rid: {
        "original_dynamic_id": ORIGINALS[rid], "delete_status": "active",
    } if rid in ORIGINALS else None)
    monkeypatch.setattr(cleanup, "list_repost_assessments", lambda uid: {
        original: object() for original in ORIGINALS.values()
    })
    monkeypatch.setattr(cleanup, "_defer_state", lambda record, stored: None)
    monkeypatch.setattr(cleanup, "_effective_candidate_state", lambda record, stored: ("safe", "", None))
    monkeypatch.setattr(cleanup, "_assess_original", lambda *_, **__: SimpleNamespace(level="safe"))

    def verify(_client, *, repost_dynamic_id, **_):
        if verification_risk["index"] == IDS.index(repost_dynamic_id) + 1:
            raise cleanup.AssessmentRateLimited("API error -352: 验证遇到平台风险")

    monkeypatch.setattr(cleanup, "_verify_owned_repost_detail", verify)
    monkeypatch.setattr(cleanup, "claim_delete_pending", lambda uid, rid, requested_at: claims.append(rid) or True)
    monkeypatch.setattr(cleanup, "mark_delete_result", lambda uid, rid, status, **kwargs: marks.append((rid, status)) or True)
    monkeypatch.setattr("web.auto_remote_state.record_auto_remote_risk", lambda **_: None)
    original = cleanup.delete_reposts
    monkeypatch.setattr(cleanup, "delete_reposts", lambda ids, **kwargs: original(ids, client_factory=lambda: client, **kwargs))
    return client, marks, claims, verification_risk


def assert_accounted(result):
    assert result["processed_count"] == len(result["items"])
    assert result["requested_count"] == result["processed_count"] + result["unprocessed_count"]
    assert result["processed_count"] == sum(result[key] for key in (
        "deleted_count", "failed_count", "unknown_count", "skipped_count",
    ))


def test_all_20_success(delete_env):
    client, marks, claims, _ = delete_env
    result = cleanup.delete_reposts(IDS)
    assert_accounted(result)
    assert (result["requested_count"], result["processed_count"], result["unprocessed_count"]) == (20, 20, 0)
    assert result["deleted_count"] == 20 and not result["stopped_early"]
    assert len(client.calls) == len(claims) == len(marks) == 20


@pytest.mark.parametrize("risk_at", [1, 5])
def test_formal_risk_preserves_prefix_and_leaves_tail_untouched(delete_env, risk_at):
    client, marks, claims, _ = delete_env
    client.outcomes[IDS[risk_at - 1]] = {"code": -352, "message": "平台风险"}
    result = cleanup.delete_reposts(IDS)
    assert_accounted(result)
    assert (result["requested_count"], result["processed_count"], result["unprocessed_count"]) == (20, risk_at, 20 - risk_at)
    assert (result["deleted_count"], result["failed_count"], result["unknown_count"], result["skipped_count"]) == (risk_at - 1, 1, 0, 0)
    assert result["stopped_early"] and result["stop_reason"] == "formal_risk"
    assert [rid for rid, _ in client.calls] == IDS[:risk_at]
    assert claims == IDS[:risk_at]
    assert [rid for rid, _ in marks] == IDS[:risk_at]


def test_http_429_is_unknown_and_rest_is_unprocessed(delete_env):
    client, marks, claims, _ = delete_env
    request = httpx.Request("POST", cleanup.DELETE_REPOST_URL)
    response = httpx.Response(429, request=request)
    client.outcomes[IDS[0]] = httpx.HTTPStatusError("HTTP 429", request=request, response=response)
    result = cleanup.delete_reposts(IDS)
    assert_accounted(result)
    assert (result["processed_count"], result["unknown_count"], result["unprocessed_count"]) == (1, 1, 19)
    assert result["stop_reason"] == "formal_risk"
    assert client.calls == [(IDS[0], 0)]
    assert claims == [IDS[0]] and marks == [(IDS[0], "unknown")]


def test_pre_delete_verification_risk_does_not_send_delete(delete_env):
    client, marks, claims, verification_risk = delete_env
    verification_risk["index"] = 1
    result = cleanup.delete_reposts(IDS)
    assert_accounted(result)
    assert (result["processed_count"], result["skipped_count"], result["unprocessed_count"]) == (1, 1, 19)
    assert result["stop_reason"] == "formal_risk"
    assert not client.calls and not claims and not marks


def test_ordinary_failure_and_real_skip_continue(delete_env):
    client, marks, claims, _ = delete_env
    client.outcomes[IDS[2]] = {"code": -400, "message": "明确失败"}
    original_get = cleanup.get_repost
    from pytest import MonkeyPatch
    with MonkeyPatch.context() as patch:
        patch.setattr(cleanup, "get_repost", lambda uid, rid: None if rid == IDS[3] else original_get(uid, rid))
        result = cleanup.delete_reposts(IDS)
    assert_accounted(result)
    assert (result["processed_count"], result["unprocessed_count"]) == (20, 0)
    assert (result["deleted_count"], result["failed_count"], result["skipped_count"]) == (18, 1, 1)
    assert not result["stopped_early"]
    assert IDS[3] not in claims and IDS[3] not in dict(marks)
    assert IDS[4] in dict(marks)


def test_cancel_after_prefix_retains_results_without_mutating_tail(delete_env):
    client, marks, claims, _ = delete_env
    cancelled = threading.Event()

    def progress(done, total, message, log_line=None):
        if done == 5:
            cancelled.set()

    result = cleanup.delete_reposts(IDS, on_progress=progress, cancel_check=cancelled.is_set)
    assert_accounted(result)
    assert (result["processed_count"], result["deleted_count"], result["unprocessed_count"]) == (5, 5, 15)
    assert result["stop_reason"] == "cancelled" and result["stopped_early"]
    assert [rid for rid, _ in client.calls] == claims == [rid for rid, _ in marks] == IDS[:5]


def test_cancel_before_auth_is_zero_mutation(delete_env):
    client, marks, claims, _ = delete_env
    result = cleanup.delete_reposts(IDS, cancel_check=lambda: True)
    assert_accounted(result)
    assert (result["processed_count"], result["unprocessed_count"]) == (0, 20)
    assert result["stop_reason"] == "cancelled" and not client.calls and not claims and not marks


def test_web_action_stops_without_filling_progress(delete_env):
    client, _, _, _ = delete_env
    client.outcomes[IDS[0]] = {"code": -352, "message": "平台风险"}
    steps = []
    payload = actions.run_action("delete_expired_reposts", {"repost_dynamic_ids": IDS}, on_progress=lambda **kw: steps.append(kw))
    assert payload["ok"] is False and payload["cancelled"] is False
    assert payload["result"]["unprocessed_count"] == 19
    assert steps[-1]["step"] == 1 and steps[-1]["total"] == 20
    assert "已处理 1/20" in payload["message"] and "剩余未处理 19" in payload["message"]


def test_full_core_action_job_rest_and_sse_contract(delete_env, monkeypatch):
    client, marks, claims, _ = delete_env
    client.outcomes[IDS[0]] = {"code": -352, "message": "平台风险"}
    event_hub.reset_for_tests()
    subscriber = event_hub.subscribe()
    runner = JobRunner()
    job_id = runner.try_start("delete_expired_reposts", {"repost_dynamic_ids": IDS})
    assert job_id is not None
    status = runner.wait_until_terminal(job_id, timeout_sec=8, initial_delay_sec=0, poll_interval_sec=0.02)
    rest = TestClient(app).get(f"/api/jobs/{job_id}")
    assert rest.status_code == 200
    persisted = get_job(job_id)
    events = []
    while not subscriber.queue.empty():
        events.append(subscriber.queue.get_nowait())
    event_hub.unsubscribe(subscriber)
    terminal = next(event for event in events if event.event == "job.terminal")
    sse_text = format_sse(terminal.event, terminal.data, event_id=terminal.seq).decode("utf-8")
    sse_data = json.loads(next(line[6:] for line in sse_text.splitlines() if line.startswith("data: ")))
    assert status.state == rest.json()["state"] == persisted["state"] == sse_data["state"] == "error"
    for key, expected in CONTRACT.items():
        assert status.result[key] == rest.json()["result"][key] == persisted["result"][key] == sse_data["result"][key] == expected
    assert status.progress_step == rest.json()["progress_step"] == persisted["progress_step"] == 1
    assert client.calls == [(IDS[0], 0)] and claims == [IDS[0]] and marks == [(IDS[0], "delete_failed")]


def test_cancelled_job_retains_processed_prefix_in_rest_and_sse(delete_env, monkeypatch):
    client, marks, claims, _ = delete_env
    runner = JobRunner()
    service = cleanup.delete_reposts

    def cancel_after_five(ids, **kwargs):
        callback = kwargs.pop("on_progress")

        def report(done, total, message, log_line=None):
            callback(done, total, message, log_line)
            if done == 5:
                runner.cancel()

        return service(ids, on_progress=report, **kwargs)

    monkeypatch.setattr(cleanup, "delete_reposts", cancel_after_five)
    event_hub.reset_for_tests()
    subscriber = event_hub.subscribe()
    job_id = runner.try_start("delete_expired_reposts", {"repost_dynamic_ids": IDS})
    assert job_id is not None
    status = runner.wait_until_terminal(job_id, timeout_sec=8, initial_delay_sec=0, poll_interval_sec=0.02)
    rest = TestClient(app).get(f"/api/jobs/{job_id}").json()
    events = []
    while not subscriber.queue.empty():
        events.append(subscriber.queue.get_nowait())
    event_hub.unsubscribe(subscriber)
    terminal = next(event for event in events if event.event == "job.terminal")
    assert status.state == rest["state"] == terminal.data["state"] == "cancelled"
    for key, expected in {"requested_count": 20, "processed_count": 5, "unprocessed_count": 15, "deleted_count": 5}.items():
        assert status.result[key] == rest["result"][key] == terminal.data["result"][key] == expected
    assert status.progress_step == rest["progress_step"] == 5
    assert [rid for rid, _ in client.calls] == claims == [rid for rid, _ in marks] == IDS[:5]


def test_full_batch_job_is_success_with_no_unprocessed(delete_env):
    client, _, _, _ = delete_env
    runner = JobRunner()
    job_id = runner.try_start("delete_expired_reposts", {"repost_dynamic_ids": IDS})
    assert job_id is not None
    status = runner.wait_until_terminal(job_id, timeout_sec=8, initial_delay_sec=0, poll_interval_sec=0.02)
    assert status.state == "success"
    assert status.result["requested_count"] == status.result["processed_count"] == 20
    assert status.result["unprocessed_count"] == 0
    assert status.progress_step == 20 and len(client.calls) == 20
