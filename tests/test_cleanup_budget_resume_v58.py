from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import web.auto_scheduler as scheduler_mod
from web.auto_scheduler import AutoScheduler


@pytest.fixture
def slot_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "scheduler_slots.json"
    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: path)
    monkeypatch.setattr("src.data_paths.get_runtime_profile_id", lambda: "account-a")
    return path


def _budget_result(start: int = 100, expires: int = 200) -> dict[str, int | bool]:
    return {
        "budget_exhausted": True,
        "window_started_at": start,
        "window_expires_at": expires,
        "resume_after": expires,
    }


def test_budget_resume_state_uses_real_window_and_claims_once(slot_file: Path) -> None:
    assert scheduler_mod._save_cleanup_budget_resume(_budget_result()) is True
    payload = scheduler_mod._read_cleanup_budget_resume()
    assert payload is not None
    assert payload["available_at"] == 200
    assert payload["window_id"] == "100:200"
    assert scheduler_mod._claim_cleanup_budget_resume("100:200") is True
    assert scheduler_mod._claim_cleanup_budget_resume("100:200") is False
    stored = json.loads(slot_file.read_text(encoding="utf-8"))
    assert stored["cleanup_budget_resume_claim"] == "100:200"


def test_budget_resume_does_not_start_before_window(slot_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scheduler_mod._save_cleanup_budget_resume(_budget_result(start=100, expires=200))
    scheduler = AutoScheduler(job_runner=None)
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", lambda: (False, ""))
    pending_calls: list[str] = []
    monkeypatch.setattr("src.repost_cleanup.cleanup_has_pending_local_work", lambda: pending_calls.append("check") or True)
    scheduler._run_cleanup_cycle = lambda *args, **kwargs: pending_calls.append("run")  # type: ignore[method-assign]

    now = datetime.fromtimestamp(199, timezone.utc).astimezone(scheduler_mod.CN_TZ)
    assert scheduler._maybe_run_cleanup_budget_resume(now) is False
    assert pending_calls == []


def test_budget_resume_runs_once_after_window_and_does_not_remote_check_when_empty(
    slot_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler_mod._save_cleanup_budget_resume(_budget_result(start=100, expires=200))
    scheduler = AutoScheduler(job_runner=None)
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", lambda: (False, ""))
    monkeypatch.setattr("src.repost_cleanup.cleanup_has_pending_local_work", lambda: False)
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda *args, **kwargs: calls.append("run")  # type: ignore[method-assign]

    now = datetime.fromtimestamp(200, timezone.utc).astimezone(scheduler_mod.CN_TZ)
    assert scheduler._maybe_run_cleanup_budget_resume(now) is True
    assert calls == []
    assert scheduler_mod._read_cleanup_budget_resume() is None


def test_budget_resume_due_claims_and_starts_only_once(
    slot_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scheduler_mod._save_cleanup_budget_resume(_budget_result(start=100, expires=200))
    scheduler = AutoScheduler(job_runner=None)
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", lambda: (False, ""))
    monkeypatch.setattr("src.repost_cleanup.cleanup_has_pending_local_work", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_remote_gate",
        lambda: scheduler_mod.RemoteGateDecision(True, "ok", ""),
    )
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda *args, **kwargs: calls.append("run")  # type: ignore[method-assign]

    now = datetime.fromtimestamp(200, timezone.utc).astimezone(scheduler_mod.CN_TZ)
    assert scheduler._maybe_run_cleanup_budget_resume(now) is True
    assert scheduler._maybe_run_cleanup_budget_resume(now) is False
    assert calls == ["run"]


def test_new_budget_window_replaces_old_resume_and_can_claim_again(slot_file: Path) -> None:
    scheduler_mod._save_cleanup_budget_resume(_budget_result(start=100, expires=200))
    assert scheduler_mod._claim_cleanup_budget_resume("100:200") is True
    scheduler_mod._save_cleanup_budget_resume(_budget_result(start=300, expires=400))
    assert scheduler_mod._claim_cleanup_budget_resume("300:400") is True
    assert scheduler_mod._claim_cleanup_budget_resume("300:400") is False


def test_only_explicit_budget_result_is_extractable() -> None:
    normal = {"job": {"result": {"phases": [{"result": {"rate_limited": True}}]}}}
    assert scheduler_mod._cleanup_budget_result_from_outcome(normal) is None
    budget = {"job": {"result": {"phases": [{"result": dict(_budget_result())}]}}}
    assert scheduler_mod._cleanup_budget_result_from_outcome(budget) is not None
