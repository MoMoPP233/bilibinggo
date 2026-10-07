"""V99: fake-clock, Profile-local night quiet planning; no remote requests."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import data_paths
from web import auto_scheduler as scheduler_mod

CN_TZ = scheduler_mod.CN_TZ


class Clock:
    def __init__(self, hour: int, minute: int, *, day: int = 1):
        self.now = datetime(2026, 10, day, hour, minute, tzinfo=CN_TZ)

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(scheduler_mod.AutoScheduler, "_schedule_auto_snapshot", lambda *a, **kw: None)


def _make(clock: Clock, *, delay: int = 240):
    runner = MagicMock()
    runner.is_running.return_value = False
    scheduler = scheduler_mod.AutoScheduler(
        runner, clock=clock, delay_sampler=lambda lo, hi: delay,
    )
    scheduler._click_and_wait = MagicMock(return_value={"skipped": False})
    scheduler._mark_remote_stage_finished = lambda: None
    return scheduler


def _due():
    return scheduler_mod._triple_due(scheduler_mod._read_slot_state(scheduler_mod._slot_state_path()))


def _old_plan(at: datetime):
    path = scheduler_mod._slot_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({scheduler_mod._TRIPLE_NEXT_RUN: at.isoformat()}), encoding="utf-8")


def test_2359_still_allowed_but_cross_midnight_plan_waits_until_morning():
    clock = Clock(23, 59)
    scheduler = _make(clock, delay=420)
    assert scheduler._remote_gate().allowed is True
    scheduler._tick_triple(clock.now)
    assert _due() == datetime(2026, 10, 2, 7, 7, tzinfo=CN_TZ)
    scheduler._click_and_wait.assert_not_called()


@pytest.mark.parametrize(
    ("hour", "minute", "quiet"),
    [(23, 59, False), (0, 0, True), (3, 0, True), (6, 59, True), (7, 0, False), (7, 1, False)],
)
def test_exact_cn_timezone_quiet_boundaries(hour, minute, quiet):
    clock = Clock(hour, minute)
    scheduler = _make(clock)
    assert scheduler._remote_gate().kind == ("night_quiet" if quiet else "ok")
    assert scheduler.get_status()["night_quiet"] is quiet


def test_0300_old_due_replans_once_without_job_or_repeated_logs():
    clock = Clock(3, 0)
    scheduler = _make(clock, delay=301)
    old = clock.now
    _old_plan(old)
    messages = []
    scheduler._log = lambda level, message: messages.append(message)
    for minute in (0, 5, 12, 19, 26, 33):
        clock.now = old + timedelta(minutes=minute)
        scheduler._tick_triple(clock.now)
    assert _due() == datetime(2026, 10, 1, 7, 5, 1, tzinfo=CN_TZ)
    scheduler._click_and_wait.assert_not_called()
    assert sum("夜间静默中" in message for message in messages) == 1
    next_task = scheduler.get_status()["next_task"]
    assert next_task["action"] == "participate"
    assert next_task["at_unix"] == int(_due().timestamp())
    assert next_task["minutes"] > 0


def test_stale_quiet_due_is_not_shown_as_fake_next_task_before_replan():
    clock = Clock(3, 0)
    _old_plan(clock.now + timedelta(minutes=5))
    scheduler = _make(clock)
    saved = scheduler_mod._slot_state_path().read_bytes()
    task = scheduler.get_status()["next_task"]
    assert task["action"] == "refresh"
    assert task["at_unix"] == int(datetime(2026, 10, 1, 7, 10, tzinfo=CN_TZ).timestamp())
    assert scheduler.get_status()["tasks"]["participate"]["next_run_at"] is None
    assert scheduler_mod._slot_state_path().read_bytes() == saved
    scheduler._tick_triple(clock.now)
    assert scheduler.get_status()["next_task"]["action"] == "participate"


def test_0659_no_job_0700_exit_and_jittered_0704_dispatch():
    clock = Clock(6, 59)
    scheduler = _make(clock)
    _old_plan(clock.now)
    scheduler._tick_triple(clock.now)
    assert _due() == datetime(2026, 10, 1, 7, 3, tzinfo=CN_TZ)
    scheduler._click_and_wait.assert_not_called()
    clock.now = datetime(2026, 10, 1, 7, 0, tzinfo=CN_TZ)
    scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_not_called()
    assert scheduler.get_status()["night_quiet"] is False
    clock.now = _due()
    scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_called_once_with("participate_triple")


def test_night_restart_preserves_plan_and_never_starts_job():
    clock = Clock(3, 0)
    _old_plan(clock.now)
    first = _make(clock, delay=420)
    first._tick_triple(clock.now)
    saved = _due()
    assert saved == datetime(2026, 10, 1, 7, 7, tzinfo=CN_TZ)
    second = _make(clock, delay=240)
    second._tick_triple(clock.now)
    assert _due() == saved
    second._click_and_wait.assert_not_called()


def test_manual_path_is_not_behind_auto_remote_gate():
    clock = Clock(3, 0)
    scheduler = _make(clock)
    assert scheduler._remote_gate().kind == "night_quiet"
    # Manual callers submit to JobRunner directly; this scheduler does not
    # intercept or change the runner's source=manual contract.
    scheduler._runner.try_start("refresh_status", {}, source="manual")
    scheduler._runner.try_start.assert_called_once_with("refresh_status", {}, source="manual")


def test_quiet_refresh_still_runs_its_local_status_step():
    clock = Clock(3, 0)
    scheduler = _make(clock)
    scheduler._run_refresh_batch("2026-10-01-03")
    scheduler._click_and_wait.assert_called_once_with("refresh_status", pipeline_index=2)
    assert scheduler._status.state != "fatal"


def test_multiple_remote_quiet_slots_emit_one_notice():
    clock = Clock(3, 0)
    scheduler = _make(clock)
    messages = []
    scheduler._log = lambda level, message: messages.append(message)
    for key in ("first", "second", "third"):
        scheduler._run_triple_slot(key)
    scheduler._click_and_wait.assert_not_called()
    assert sum("夜间静默中" in message for message in messages) == 1


def test_next_task_keeps_earlier_night_local_slot(monkeypatch):
    clock = Clock(3, 0)
    monkeypatch.setattr(scheduler_mod, "REFRESH_SLOTS", ((3, 2), *scheduler_mod.REFRESH_SLOTS))
    scheduler = _make(clock)
    scheduler._tick_triple(clock.now)
    next_task = scheduler.get_status()["next_task"]
    assert next_task["action"] == "refresh"
    assert next_task["at_unix"] == int(datetime(2026, 10, 1, 3, 2, tzinfo=CN_TZ).timestamp())


def test_two_profiles_keep_separate_plans(tmp_path, monkeypatch):
    profile = {"path": tmp_path / "account-1"}
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: profile["path"])
    clock = Clock(3, 0)
    _make(clock, delay=240)._tick_triple(clock.now)
    first = _due()
    profile["path"] = tmp_path / "account-2"
    _make(clock, delay=420)._tick_triple(clock.now)
    second = _due()
    assert first == datetime(2026, 10, 1, 7, 4, tzinfo=CN_TZ)
    assert second == datetime(2026, 10, 1, 7, 7, tzinfo=CN_TZ)
    profile["path"] = tmp_path / "account-1"
    assert _due() == first


def test_night_continue_keeps_original_short_jitter(monkeypatch):
    from web.auto_config import set_night_continue_enabled

    clock = Clock(3, 0)
    set_night_continue_enabled(True)
    scheduler = _make(clock)
    scheduler._tick_triple(clock.now)
    assert _due() == clock.now + timedelta(seconds=240)


def test_enabling_night_continue_reopens_deferred_plan_once():
    from web.auto_config import set_night_continue_enabled

    clock = Clock(3, 0)
    scheduler = _make(clock)
    scheduler._tick_triple(clock.now)
    assert _due() == datetime(2026, 10, 1, 7, 4, tzinfo=CN_TZ)
    set_night_continue_enabled(True)
    scheduler._tick_triple(clock.now)
    assert _due() == clock.now + timedelta(seconds=240)
    scheduler._tick_triple(clock.now)
    assert _due() == clock.now + timedelta(seconds=240)
    scheduler._click_and_wait.assert_not_called()
