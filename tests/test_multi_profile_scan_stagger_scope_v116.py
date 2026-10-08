"""V116: only automatic refresh and Following scans inherit Profile stagger."""

from __future__ import annotations

from datetime import datetime
import threading

import pytest

from src import data_paths
import web.auto_scheduler as scheduler_mod
from web.auto_scheduler import AutoScheduler, CN_TZ, _scan_slot_key


@pytest.fixture(autouse=True)
def isolated_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(scheduler_mod, "get_night_continue_enabled", lambda: False)


def at(hour: int, minute: int, *, day: int = 17) -> datetime:
    return datetime(2026, 7, day, hour, minute, tzinfo=CN_TZ)


def slot_due(scheduler: AutoScheduler, kind: str, base: datetime, now: datetime) -> datetime:
    slots = scheduler._runtime_scan_slots(now)
    assert slots is not None
    return next(due for item_kind, item_base, due in slots if item_kind == kind and item_base == base)


def test_single_profile_keeps_every_original_due(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 0))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 0)
    assert scheduler._runtime_scan_slots(at(12, 0)) is None
    status = scheduler.get_status()
    assert status["tasks"]["winner"]["next_run_at"] == int(at(12, 10).timestamp())
    assert status["tasks"]["refresh"]["next_run_at"] == int(at(12, 40).timestamp())
    assert status["tasks"]["following"]["next_run_at"] == int(at(14, 0).timestamp())


@pytest.mark.parametrize("offset,refresh,following", [
    (15, at(12, 55), at(14, 15)),
    (30, at(13, 10), at(14, 30)),
])
def test_second_and_third_profiles_shift_only_selected_scans(monkeypatch, offset, refresh, following):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset)
    now = at(11, 50)
    assert slot_due(scheduler, "winner", at(12, 10), now) == at(12, 10)
    assert slot_due(scheduler, "refresh", at(12, 40), now) == refresh
    assert slot_due(scheduler, "following", at(14, 0), now) == following


def test_morning_refresh_and_following_are_fifteen_minutes_apart(monkeypatch):
    first = AutoScheduler()
    second = AutoScheduler()
    monkeypatch.setattr(first, "_scan_offset_minutes", lambda: 0)
    monkeypatch.setattr(second, "_scan_offset_minutes", lambda: 15)
    assert first._runtime_scan_slots(at(6, 50)) is None
    assert slot_due(second, "refresh", at(7, 10), at(6, 50)) == at(7, 25)
    assert slot_due(second, "following", at(8, 0), at(6, 50)) == at(8, 15)


def test_winner_dispatches_at_original_minute_not_shifted_minute(monkeypatch):
    for now, expected in ((at(12, 10), ["winner-2026-07-17-12-10"]), (at(12, 25), [])):
        scheduler = AutoScheduler(clock=lambda now=now: now)
        monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
        stop = threading.Event()
        called: list[str] = []
        monkeypatch.setattr(scheduler, "_run_winner_scan", lambda key: called.append(key) or stop.set())
        monkeypatch.setattr(scheduler, "_tick_triple", lambda *_args: stop.set())
        scheduler._generation = 1
        scheduler._stop_event = stop
        scheduler._loop(1, stop)
        assert called == expected


def test_rank_change_keeps_winner_base_and_pending_refresh_safe(monkeypatch):
    offset = [15]
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: offset[0])
    assert slot_due(scheduler, "winner", at(12, 10), at(11, 50)) == at(12, 10)
    assert slot_due(scheduler, "refresh", at(12, 40), at(12, 30)) == at(12, 55)
    offset[0] = 0
    assert slot_due(scheduler, "refresh", at(12, 40), at(12, 45)) == at(12, 55)
    scheduler._done_winner.add(_scan_slot_key("winner", at(12, 10)))
    candidates, _ = scheduler._runtime_candidates(at(12, 45))
    assert not any(kind == "winner" and due == at(12, 10) for kind, _label, due in candidates)
    assert ("refresh", "刷新批次", at(12, 55)) in candidates


def test_next_task_and_tray_scan_due_use_unshifted_winner(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 0))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    status = scheduler.get_status()
    assert status["next_task"]["action"] == "winner"
    assert status["next_task"]["at_unix"] == int(at(12, 10).timestamp())
    assert status["tasks"]["refresh"]["next_run_at"] == int(at(12, 55).timestamp())
    assert status["tasks"]["following"]["next_run_at"] == int(at(14, 15).timestamp())
    candidates, _ = scheduler._runtime_candidates(at(12, 0))
    assert scheduler_mod._next_scannable_due(candidates, night_continue=False) == at(12, 10)
    stop = threading.Event()
    monkeypatch.setattr(scheduler, "_tick_triple", lambda *_args: stop.set())
    scheduler._generation = 1
    scheduler._stop_event = stop
    scheduler._loop(1, stop)
    assert scheduler.tray_snapshot()["next_scan_unix"] == int(at(12, 10).timestamp())


def test_night_quiet_still_defers_shifted_following_but_not_winner(monkeypatch):
    scheduler = AutoScheduler()
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 75)
    now = at(21, 50)
    assert slot_due(scheduler, "winner", at(22, 10), now) == at(22, 10)
    assert slot_due(scheduler, "following", at(23, 0), now) == at(8, 15, day=18)


def test_participation_due_and_risk_pause_are_unchanged(monkeypatch):
    scheduler = AutoScheduler(clock=lambda: at(12, 0))
    monkeypatch.setattr(scheduler, "_scan_offset_minutes", lambda: 15)
    participate_due = at(12, 5)
    original = scheduler_mod._next_occurrence_candidates
    monkeypatch.setattr(scheduler_mod, "_next_occurrence_candidates", lambda now:
                        original(now) + [("participate", "自动参与", participate_due)])
    monkeypatch.setattr(scheduler_mod, "risk_pause_state", lambda: {
        "paused": True, "reason": "platform cooldown", "paused_until": 12345,
    })
    status = scheduler.get_status()
    assert status["next_task"]["action"] == "participate"
    assert status["next_task"]["at_unix"] == int(participate_due.timestamp())
    assert status["tasks"]["winner"]["status"] == "risk_paused"
    assert status["tasks"]["refresh"]["status"] == "risk_paused"
