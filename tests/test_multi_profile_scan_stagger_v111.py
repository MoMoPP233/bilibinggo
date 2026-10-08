"""V111 bounded local scheduling tests; no Bilibili or user Profile access."""

from __future__ import annotations

from datetime import datetime, timedelta
import threading

import pytest

from src import data_paths, runtime_instances
import web.auto_scheduler as scheduler_mod
from web.auto_scheduler import AutoScheduler, CN_TZ, _scan_slot_key, _shift_scan_due


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(scheduler_mod, "get_night_continue_enabled", lambda: False)
    monkeypatch.delenv(runtime_instances.RUNTIME_PROFILE_ENV, raising=False)
    monkeypatch.delenv(runtime_instances.RUNTIME_INSTANCE_ENV, raising=False)


def at(hour: int, minute: int, *, day: int = 17) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=CN_TZ)


def due(scheduler: AutoScheduler, kind: str, base: datetime, now: datetime) -> datetime:
    slots = scheduler._runtime_scan_slots(now)
    assert slots is not None
    return next(item_due for item_kind, item_base, item_due in slots
                if item_kind == kind and item_base == base)


def test_single_runtime_keeps_original_exact_minute(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 30))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 0)
    assert scheduler._runtime_scan_slots(at(12, 30)) is None
    assert scheduler.get_status()["tasks"]["refresh"]["next_run_at"] == int(at(12, 40).timestamp())


@pytest.mark.parametrize("rank,expected", [(0, 0), (1, 15), (2, 30)])
def test_ready_runtime_rank_has_fifteen_minute_steps(monkeypatch, rank, expected):
    monkeypatch.setenv(runtime_instances.RUNTIME_PROFILE_ENV, f"account-{rank+1}")
    monkeypatch.setenv(runtime_instances.RUNTIME_INSTANCE_ENV, f"instance-{rank+1}")
    roster = {
        f"account-{i+1}": {"profile_id": f"account-{i+1}", "instance_id": f"instance-{i+1}",
                          "started_at": float(i), "ready": True}
        for i in range(3)
    }
    monkeypatch.setattr(runtime_instances, "list_runtime_instances", lambda: roster)
    assert AutoScheduler()._scan_offset_minutes() == expected


def test_stale_registry_entry_does_not_affect_rank(monkeypatch):
    monkeypatch.setenv(runtime_instances.RUNTIME_PROFILE_ENV, "account-2")
    monkeypatch.setenv(runtime_instances.RUNTIME_INSTANCE_ENV, "b")
    monkeypatch.setattr(runtime_instances, "list_runtime_instances", lambda: {
        "stale": {"profile_id": "stale", "instance_id": "x", "started_at": 0, "ready": False},
        "account-1": {"profile_id": "account-1", "instance_id": "a", "started_at": 1, "ready": True},
        "account-2": {"profile_id": "account-2", "instance_id": "b", "started_at": 2, "ready": True},
    })
    assert AutoScheduler()._scan_offset_minutes() == 15


def test_equal_start_times_sort_by_profile_id(monkeypatch):
    monkeypatch.setenv(runtime_instances.RUNTIME_PROFILE_ENV, "account-2")
    monkeypatch.setenv(runtime_instances.RUNTIME_INSTANCE_ENV, "b")
    monkeypatch.setattr(runtime_instances, "list_runtime_instances", lambda: {
        "account-2": {"profile_id": "account-2", "instance_id": "b", "started_at": 1, "ready": True},
        "account-1": {"profile_id": "account-1", "instance_id": "a", "started_at": 1, "ready": True},
    })
    assert AutoScheduler()._scan_offset_minutes() == 15


@pytest.mark.parametrize("offset,expected", [(15, at(12, 55)), (30, at(13, 10))])
def test_refresh_due_is_base_plus_offset(monkeypatch, offset, expected):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset)
    assert due(scheduler, "refresh", at(12, 40), at(12, 30)) == expected


def test_all_three_heavy_scan_kinds_shift_and_keep_own_base(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    now = at(11, 50)
    assert due(scheduler, "winner", at(12, 10), now) == at(12, 25)
    assert due(scheduler, "refresh", at(12, 40), now) == at(12, 55)
    assert due(scheduler, "following", at(14, 0), now) == at(14, 15)


def test_join_then_leave_before_base_restores_original_plan(monkeypatch):
    offset = [15]
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset[0])
    assert due(scheduler, "refresh", at(12, 40), at(12, 20)) == at(12, 55)
    offset[0] = 0
    assert due(scheduler, "refresh", at(12, 40), at(12, 25)) == at(12, 40)


def test_leave_after_base_preserves_pending_later_due_no_immediate_burst(monkeypatch):
    offset = [15]
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset[0])
    assert due(scheduler, "refresh", at(12, 40), at(12, 30)) == at(12, 55)
    offset[0] = 0
    assert due(scheduler, "refresh", at(12, 40), at(12, 45)) == at(12, 55)
    assert due(scheduler, "refresh", at(18, 10), at(12, 45)) == at(18, 10)


def test_rank_drop_during_due_minute_does_not_erase_pending_scan(monkeypatch):
    offset = [15]
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset[0])
    scheduler._runtime_scan_slots(at(12, 50))
    offset[0] = 0
    assert due(scheduler, "refresh", at(12, 40), at(12, 55).replace(second=10)) == at(12, 55)


def test_scheduler_dispatches_shifted_logical_slot_not_original_minute(monkeypatch):
    for now, expected in ((at(12, 40), []), (at(12, 55), ["2026-07-17-12"])):
        scheduler = AutoScheduler(clock=lambda now=now: now)
        monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
        stop = threading.Event()
        calls = []
        monkeypatch.setattr(scheduler, "_run_refresh_batch", lambda key: calls.append(key) or stop.set())
        monkeypatch.setattr(scheduler, "_tick_triple", lambda *args, **kwargs: stop.set())
        scheduler._generation = 1
        scheduler._stop_event = stop
        scheduler._loop(1, stop)
        assert calls == expected


def test_completed_slot_is_not_advertised_again(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    scheduler._done_refresh.add(_scan_slot_key("refresh", at(12, 40)))
    candidates, _ = scheduler._runtime_candidates(at(12, 50))
    assert ("refresh", "刷新批次", at(12, 55)) not in candidates


def test_persisted_claim_prevents_duplicate_after_restart(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    monkeypatch.setattr(scheduler_mod, "_read_slot_state", lambda path: {"refresh": _scan_slot_key("refresh", at(12, 40))})
    candidates, _ = scheduler._runtime_candidates(at(12, 50))
    assert ("refresh", "刷新批次", at(12, 55)) not in candidates


def test_pending_slot_remains_scheduled_when_rank_changes(monkeypatch):
    rank = [30]
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: rank[0])
    assert due(scheduler, "refresh", at(12, 40), at(12, 45)) == at(13, 10)
    rank[0] = 0
    assert due(scheduler, "refresh", at(12, 40), at(12, 50)) == at(13, 10)


def test_inflight_job_is_not_cancelled_when_rank_changes(monkeypatch):
    class Runner:
        def cancel(self):
            pytest.fail("stagger must not cancel an in-flight job")
    scheduler = AutoScheduler(job_runner=Runner())
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    scheduler._status.tasks["refresh"]["status"] = "running"
    scheduler._runtime_scan_slots(at(12, 30))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 0)
    scheduler._runtime_scan_slots(at(12, 35))
    assert scheduler._status.tasks["refresh"]["status"] == "running"


def test_long_running_first_scan_can_overlap_second_without_extra_coordination(monkeypatch):
    first, second = AutoScheduler(), AutoScheduler()
    monkeypatch.setattr(first, "_scan_offset_minutes", lambda: 0)
    monkeypatch.setattr(second, "_scan_offset_minutes", lambda: 15)
    assert first._runtime_scan_slots(at(12, 30)) is None
    assert due(second, "refresh", at(12, 40), at(12, 30)) == at(12, 55)
    # The first scan may still be running at 12:55; no cross-process lock is added.


def test_midnight_shift_defers_past_quiet_and_preserves_spacing():
    base = at(23, 0)
    assert _shift_scan_due(base, 60, night_continue=False) == at(8, 0, day=18)
    assert _shift_scan_due(base, 75, night_continue=False) == at(8, 15, day=18)
    assert _shift_scan_due(base, 60, night_continue=True) == at(0, 0, day=18)


def test_large_roster_keeps_prior_day_logical_slot(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 25 * 60)
    assert due(scheduler, "refresh", at(12, 40, day=16), at(13, 30)) == at(13, 40)


def test_next_task_and_task_next_run_show_shifted_due(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 30))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    status = scheduler.get_status()
    assert status["tasks"]["refresh"]["next_run_at"] == int(at(12, 55).timestamp())
    assert status["next_task"]["at_unix"] == int(at(12, 55).timestamp())
    assert status["next_slot"]["at_unix"] == int(at(12, 55).timestamp())


def test_stagger_does_not_clear_existing_risk_pause(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 30))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    monkeypatch.setattr(scheduler_mod, "risk_pause_state", lambda: {
        "paused": True, "reason": "platform cooldown", "paused_until": 12345,
    })
    status = scheduler.get_status()
    assert status["tasks"]["refresh"]["status"] == "risk_paused"
    assert status["tasks"]["refresh"]["resume_at"] == 12345


def test_auto_participation_due_is_not_shifted(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 30)
    participate = at(12, 35)
    monkeypatch.setattr(scheduler_mod, "_next_occurrence_candidates", lambda now: [
        ("participate", "自动参与", participate), ("refresh", "刷新批次", at(12, 40))])
    candidates, _ = scheduler._runtime_candidates(at(12, 30))
    assert ("participate", "自动参与", participate) in candidates
    assert ("refresh", "刷新批次", at(12, 40)) not in candidates


def test_two_scheduler_instances_keep_separate_pending_plans(monkeypatch):
    a, b = AutoScheduler(), AutoScheduler()
    monkeypatch.setattr(a, "_scan_offset_minutes", lambda: 15)
    monkeypatch.setattr(b, "_scan_offset_minutes", lambda: 30)
    assert due(a, "refresh", at(12, 40), at(12, 30)) == at(12, 55)
    assert due(b, "refresh", at(12, 40), at(12, 30)) == at(13, 10)
    assert a._scan_plans != b._scan_plans


def test_stagger_does_not_modify_rate_limit_configuration():
    from web.auto_config import MIN_REMOTE_STAGE_GAP_SECONDS
    assert MIN_REMOTE_STAGE_GAP_SECONDS == 60
