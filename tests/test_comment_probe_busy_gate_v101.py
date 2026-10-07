"""V101: a busy JobRunner must defer comment recovery before any probe I/O."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

from src import comment_breaker, comment_confirmation, data_paths
from web.auto_scheduler import AutoScheduler, CN_TZ
from web.job_runner import JobStatus


DYNAMIC_ID = "1251573729479098372"


def _scheduler(action: str = "refresh_all", *, hour: int = 12) -> tuple[AutoScheduler, Mock]:
    runner = Mock()
    runner.is_running.return_value = action != ""
    runner.get_status.return_value = JobStatus(
        id=42 if action else None,
        state="running" if action else "idle",
        action=action,
        message="扫描中" if action else "",
    )
    scheduler = AutoScheduler(
        job_runner=runner,
        clock=lambda: datetime(2026, 10, 7, hour, 0, tzinfo=CN_TZ),
    )
    scheduler._status.state = "running"
    return scheduler, runner


def test_idle_runner_keeps_existing_recovery_path(isolated_home, monkeypatch) -> None:
    scheduler, runner = _scheduler("")
    recovery = Mock(return_value={"claimed": True, "result": "success"})
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)
    scheduler._click_and_wait = Mock(side_effect=AssertionError("probe replaces triple Job"))

    scheduler._run_triple_slot("idle-probe")

    recovery.assert_called_once_with()
    scheduler._click_and_wait.assert_not_called()
    runner.try_start.assert_not_called()
    assert scheduler._status.tasks["participate"]["status"] == "completed"


@pytest.mark.parametrize("action", ["refresh_all", "refresh_source", "update_all_datasources", "refresh_status"])
@pytest.mark.parametrize("guard_status", ["pending", "unknown"])
def test_busy_runner_defers_without_probe_or_guard_mutation(
    isolated_home, monkeypatch, action: str, guard_status: str
) -> None:
    comment_breaker.trigger_comment_limit()
    with comment_breaker.comment_mutation_gate():
        comment_confirmation._set_comment_status_unlocked(
            DYNAMIC_ID, uid=42, status=guard_status
        )
    breaker_path = comment_breaker._state_path()
    guard_path = comment_confirmation._state_path()
    before_breaker = breaker_path.read_bytes()
    before_guard = guard_path.read_bytes()

    scheduler, runner = _scheduler(action)
    recovery = Mock(side_effect=AssertionError("busy runner must not probe Bilibili"))
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)
    scheduler._click_and_wait = Mock(side_effect=AssertionError("busy runner must not start Job"))

    scheduler._run_triple_slot("busy-1")
    scheduler._run_triple_slot("busy-2")

    recovery.assert_not_called()
    scheduler._click_and_wait.assert_not_called()
    runner.cancel.assert_not_called()
    assert breaker_path.read_bytes() == before_breaker
    assert guard_path.read_bytes() == before_guard
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=42) == guard_status
    assert scheduler._status.tasks["participate"]["status"] == "skipped_busy"
    assert scheduler._status.state == "running"
    assert scheduler.get_status()["job_probe"]["job_action"] == action


def test_recovery_can_run_after_busy_job_finishes(isolated_home, monkeypatch) -> None:
    scheduler, runner = _scheduler("refresh_all")
    recovery = Mock(return_value={"claimed": True, "result": "success"})
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)
    scheduler._click_and_wait = Mock(side_effect=AssertionError("probe replaces triple Job"))

    scheduler._run_triple_slot("busy-slot")
    recovery.assert_not_called()
    runner.is_running.return_value = False
    runner.get_status.return_value = JobStatus(state="success", action="refresh_all")
    scheduler._run_triple_slot("later-slot")

    recovery.assert_called_once_with()
    assert scheduler._status.tasks["participate"]["status"] == "completed"


def test_busy_due_tick_replans_then_recovers_on_later_tick(isolated_home, monkeypatch) -> None:
    clock = {"now": datetime(2026, 10, 7, 12, 0, tzinfo=CN_TZ)}
    runner = Mock()
    runner.is_running.return_value = True
    runner.get_status.return_value = JobStatus(state="running", action="refresh_all")
    scheduler = AutoScheduler(
        job_runner=runner,
        clock=lambda: clock["now"],
        delay_sampler=lambda _minimum, _maximum: 240,
    )
    scheduler._status.state = "running"
    recovery = Mock(return_value={"claimed": True, "result": "success"})
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)
    scheduler._click_and_wait = Mock(side_effect=AssertionError("probe replaces triple Job"))

    scheduler._plan_triple(clock["now"], None)
    clock["now"] = clock["now"].replace(minute=4)
    scheduler._tick_triple(clock["now"])
    recovery.assert_not_called()

    runner.is_running.return_value = False
    runner.get_status.return_value = JobStatus(state="success", action="refresh_all")
    clock["now"] = clock["now"].replace(second=1)
    scheduler._tick_triple(clock["now"])
    recovery.assert_not_called()  # consumed scheduler slot only; recovery claim untouched

    clock["now"] = clock["now"].replace(minute=8)
    scheduler._tick_triple(clock["now"])
    recovery.assert_called_once_with()


def test_night_quiet_still_blocks_recovery(isolated_home, monkeypatch) -> None:
    scheduler, runner = _scheduler("", hour=3)
    recovery = Mock(side_effect=AssertionError("quiet time must not probe"))
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)

    scheduler._run_triple_slot("quiet-slot")

    recovery.assert_not_called()
    runner.try_start.assert_not_called()


def test_busy_gate_uses_own_profile_runner(isolated_home, monkeypatch, tmp_path) -> None:
    active = {"path": tmp_path / "account-1"}
    active["path"].mkdir()
    (tmp_path / "account-2").mkdir()
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: active["path"])
    recovery = Mock(return_value={"claimed": True, "result": "success"})
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", recovery)
    busy_scheduler, _ = _scheduler("refresh_all")
    idle_scheduler, _ = _scheduler("")
    busy_scheduler._click_and_wait = Mock(side_effect=AssertionError("busy runner"))
    idle_scheduler._click_and_wait = Mock(side_effect=AssertionError("probe replaces triple Job"))

    busy_scheduler._run_triple_slot("same-logical-slot")
    active["path"] = tmp_path / "account-2"
    idle_scheduler._run_triple_slot("same-logical-slot")

    recovery.assert_called_once_with()
    assert (tmp_path / "account-1" / "scheduler_slots.json").is_file()
    assert (tmp_path / "account-2" / "scheduler_slots.json").is_file()
