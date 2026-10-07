"""V106: DS9 unique-topic display baselines are local and commit-bound."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src import ds9_contribution_audit
from src.sources import ds9_topics as ds9
from src.sources.common import commit_source_checkpoint
from web.actions import _commit_ds9_unchanged_stats
from tests.test_ds9_topics_m1 import (
    DID1,
    DID2,
    TOPIC_A,
    TOPIC_B,
    _FakeTopicClient,
    _install_client,
    _original_payload,
)


@pytest.fixture(autouse=True)
def _isolated_runtime(isolated_home: Path):
    return isolated_home


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a" / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: target)
    return target


def _record(topic_id: str, dynamic_id: str) -> dict[str, str]:
    return {"topic_id": topic_id, "dynamic_id": dynamic_id}


def _pending(topic_id: str, *, incomplete: list[str] | None = None) -> dict[str, object]:
    return {
        "topic_id": topic_id,
        "fp": "fingerprint",
        "incomplete": incomplete or [],
        "last_success_at": 100,
        "last_found": 1,
        "last_scan_at": 100,
    }


def _topic(topic_id: str) -> dict:
    return next(item for item in ds9.list_topics() if item["topic_id"] == topic_id)


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (["A", "B", "C"], ["B", "C", "D", "E"], 2),
        (["A", "B"], ["B", "C"], 1),
        (["A", "B", "C"], ["C", "D"], 1),
        (["A", "B"], ["B", "A"], 0),
    ],
)
def test_unique_delta_is_set_difference_not_count_subtraction(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
    previous: list[str], current: list[str], expected: int,
) -> None:
    records = ds9.list_topics()
    records[0]["last_success_unique_ids"] = previous
    ds9._save_topic_records(records)
    monkeypatch.setattr(
        ds9_contribution_audit, "list_unique_valid",
        lambda: [_record(TOPIC_A, dynamic_id) for dynamic_id in current],
    )
    ds9._apply_topic_commits([_pending(TOPIC_A)])
    assert _topic(TOPIC_A)["last_unique_added"] == expected
    assert _topic(TOPIC_A)["last_success_unique_ids"] == sorted(current)


def test_first_scan_and_old_state_do_not_count_history_as_new(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"version": 1, "topics": [{"topic_id": TOPIC_A, "fp": "old"}]}), encoding="utf-8")
    assert _topic(TOPIC_A)["last_success_unique_ids"] is None
    monkeypatch.setattr(
        ds9_contribution_audit, "list_unique_valid",
        lambda: [_record(TOPIC_A, DID1), _record(TOPIC_A, DID2)],
    )
    ds9._apply_topic_commits([_pending(TOPIC_A)])
    assert _topic(TOPIC_A)["last_unique_added"] is None
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1, DID2]


def test_topics_and_profiles_have_independent_baselines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_a = tmp_path / "account-a" / "ds9_topics.json"
    profile_b = tmp_path / "account-b" / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: profile_a)
    records = ds9.list_topics()
    records[0]["last_success_unique_ids"] = [DID1]
    records[1]["last_success_unique_ids"] = [DID2]
    ds9._save_topic_records(records)
    monkeypatch.setattr(
        ds9_contribution_audit, "list_unique_valid",
        lambda: [_record(TOPIC_A, DID1), _record(TOPIC_A, DID2), _record(TOPIC_B, DID2)],
    )
    ds9._apply_topic_commits([_pending(TOPIC_A), _pending(TOPIC_B)])
    assert _topic(TOPIC_A)["last_unique_added"] == 1
    assert _topic(TOPIC_B)["last_unique_added"] == 0
    monkeypatch.setattr(ds9, "_state_path", lambda: profile_b)
    assert _topic(TOPIC_A)["last_success_unique_ids"] is None
    ds9._apply_topic_commits([_pending(TOPIC_A)])
    assert _topic(TOPIC_A)["last_unique_added"] is None
    monkeypatch.setattr(ds9, "_state_path", lambda: profile_a)
    assert _topic(TOPIC_A)["last_unique_added"] == 1


def test_incomplete_topic_and_audit_failure_keep_previous_baseline(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = ds9.list_topics()
    records[0]["last_success_unique_ids"] = [DID1]
    records[0]["last_unique_added"] = 3
    ds9._save_topic_records(records)
    monkeypatch.setattr(ds9_contribution_audit, "list_unique_valid", lambda: [_record(TOPIC_A, DID2)])
    ds9._apply_topic_commits([_pending(TOPIC_A, incomplete=["unresolved-outer"])])
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1]
    assert _topic(TOPIC_A)["last_unique_added"] == 3

    def unavailable():
        raise OSError("audit unavailable")

    monkeypatch.setattr(ds9_contribution_audit, "list_unique_valid", unavailable)
    ds9._apply_topic_commits([_pending(TOPIC_A)])
    assert _topic(TOPIC_A)["fp"] == "fingerprint"  # checkpoint must not depend on audit
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1]


def test_partial_and_network_failure_do_not_advance_baseline_then_recovery_does(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = ds9.list_topics()
    records[0]["last_success_unique_ids"] = [DID1]
    ds9._save_topic_records(records)
    monkeypatch.setattr(
        ds9_contribution_audit, "list_unique_valid",
        lambda: [_record(TOPIC_A, DID1), _record(TOPIC_A, DID2)],
    )
    _install_client(monkeypatch, _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID2)}))
    partial_result = ds9.check_update()
    assert partial_result.updated
    # The public action intentionally does not call the checkpoint hook on pipeline partial.
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1]

    _install_client(monkeypatch, _FakeTopicClient(errors_by_topic={TOPIC_A: OSError("network") }))
    failed_result = ds9.check_update()
    assert failed_result.ds9_summary["partial"] is True
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1]

    _install_client(monkeypatch, _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID2)}))
    recovered = ds9.check_update()
    commit_source_checkpoint(recovered)
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1, DID2]
    assert _topic(TOPIC_A)["last_unique_added"] == 1


def test_formal_risk_never_advances_baseline(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = ds9.list_topics()
    records[0]["last_success_unique_ids"] = [DID1]
    ds9._save_topic_records(records)
    request = httpx.Request("GET", "https://api.bilibili.com")
    risk = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    _install_client(monkeypatch, _FakeTopicClient(errors_by_topic={TOPIC_A: risk}))
    with pytest.raises(httpx.HTTPStatusError):
        ds9.check_update()
    assert _topic(TOPIC_A)["last_success_unique_ids"] == [DID1]


def test_unchanged_success_reports_zero_only_after_action_completion(
    state_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = [_record(TOPIC_A, DID1)]
    monkeypatch.setattr(ds9_contribution_audit, "list_unique_valid", lambda: current)
    _install_client(monkeypatch, _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)}))
    first = ds9.check_update()
    commit_source_checkpoint(first)
    assert _topic(TOPIC_A)["last_unique_added"] is None

    records = ds9.list_topics()
    records[0]["last_unique_added"] = 5
    ds9._save_topic_records(records)
    _install_client(monkeypatch, _FakeTopicClient(data_by_topic={TOPIC_A: _original_payload(DID1)}))
    unchanged = ds9.check_update()
    assert unchanged.updated is False
    assert _topic(TOPIC_A)["last_unique_added"] == 5  # source check alone cannot commit
    _commit_ds9_unchanged_stats(unchanged)  # the action's successful return boundary
    assert _topic(TOPIC_A)["last_unique_added"] == 0
