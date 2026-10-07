"""V102: participation is running only after JobRunner accepts its Job."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import Mock

import pytest

from src import data_paths
from web import auto_scheduler as scheduler_mod
from web.job_runner import JobStatus


@pytest.fixture(autouse=True)
def _profile(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(scheduler_mod.AutoScheduler, "_schedule_auto_snapshot", lambda *a, **kw: None)


def _scheduler(action: str = ""):
    runner = Mock()
    runner.is_running.return_value = bool(action)
    runner.get_status.return_value = JobStatus(
        id=41 if action else None,
        state="running" if action else "idle",
        action=action,
        message="扫描中" if action else "",
    )
    clock = {"now": datetime(2026, 10, 7, 12, 0, tzinfo=scheduler_mod.CN_TZ)}
    scheduler = scheduler_mod.AutoScheduler(
        runner, clock=lambda: clock["now"], delay_sampler=lambda _lo, _hi: 240,
    )
    scheduler._status.state = "running"
    return scheduler, runner, clock


def test_scan_busy_never_marks_participation_running_or_stops_scan(monkeypatch):
    scheduler, runner, clock = _scheduler("refresh_all")
    probe = Mock(side_effect=AssertionError("busy scan must prevent recovery probe"))
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", probe)
    seen = []
    original = scheduler._set_task_status

    def record(task, status, result=""):
        seen.append((task, status))
        original(task, status, result)

    scheduler._set_task_status = record
    scheduler._plan_triple(clock["now"], None)
    clock["now"] = clock["now"].replace(minute=4)
    scheduler._tick_triple(clock["now"])

    assert ("participate", "running") not in seen
    assert ("participate", "dispatch_check") in seen
    assert scheduler._status.tasks["participate"]["status"] == "skipped_busy"
    assert scheduler.get_status()["job_probe"]["job_action"] == "refresh_all"
    assert scheduler._status.state == "running"
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()
    probe.assert_not_called()

    # 现有契约：占用结束后重新规划未来刻度，不补跑已跳过的这一轮。
    runner.is_running.return_value = False
    runner.get_status.return_value = JobStatus(state="success", action="refresh_all")
    clock["now"] = clock["now"].replace(second=1)
    scheduler._tick_triple(clock["now"])
    due = scheduler_mod._triple_due(scheduler_mod._read_slot_state(scheduler_mod._slot_state_path()))
    assert due is not None and due > clock["now"]
    assert scheduler.get_status()["next_task"]["at_unix"] > int(clock["now"].timestamp())
    runner.try_start.assert_not_called()


@pytest.mark.parametrize("terminal", ["success", "error"])
def test_participation_running_only_after_runner_accepts(monkeypatch, terminal):
    scheduler, runner, _clock = _scheduler()
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", lambda: {"claimed": False})
    seen_at_start = []

    def accept(action, params, *, source):
        seen_at_start.append(scheduler._status.tasks["participate"]["status"])
        assert action == "participate_triple"
        assert source == "auto"
        return 42

    runner.try_start.side_effect = accept

    def terminal_status(job_id, _label):
        assert job_id == 42
        assert scheduler._status.tasks["participate"]["status"] == "running"
        assert scheduler._status.last_click["job_id"] == 42
        return {"state": terminal, "message": "done" if terminal == "success" else "failed"}

    scheduler._wait_until_terminal = terminal_status
    scheduler._run_triple_slot(f"v102-{terminal}")

    assert seen_at_start == ["dispatch_check"]
    assert scheduler._status.tasks["participate"]["status"] == (
        "completed" if terminal == "success" else "error"
    )
    runner.cancel.assert_not_called()


def test_runner_rejects_job_without_false_running(monkeypatch):
    scheduler, runner, _clock = _scheduler()
    monkeypatch.setattr("src.comment_probe.run_comment_recovery_probe", lambda: {"claimed": False})
    runner.try_start.return_value = None
    seen = []
    original = scheduler._set_task_status

    def record(task, status, result=""):
        seen.append(status)
        original(task, status, result)

    scheduler._set_task_status = record
    scheduler._run_triple_slot("v102-reject")
    assert "running" not in seen
    assert scheduler._status.tasks["participate"]["status"] == "skipped_busy"
