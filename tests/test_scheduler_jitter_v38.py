"""V38：fake clock/random/Job，无真实网络、用户数据或 mutation。"""
from __future__ import annotations

import json
import random
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

import web.auto_scheduler as sch


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 20, 10, 0, tzinfo=sch.CN_TZ)

    def __call__(self):
        return self.now


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("src.data_paths.get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(sch.AutoScheduler, "_schedule_auto_snapshot", lambda *a, **kw: None)
    # Windows 上临时文件刚关闭时，杀毒/索引器可能短暂持有目标文件句柄。
    # 只在本测试适配这一过渡窗口；生产写入逻辑和失败语义保持不变。
    original_write = sch._write_slot_state_atomic

    def write_with_windows_retry(path, state):
        for attempt in range(4):
            try:
                return original_write(path, state)
            except PermissionError:
                if attempt == 3:
                    raise
                time.sleep(0.02)

    monkeypatch.setattr(sch, "_write_slot_state_atomic", write_with_windows_retry)


def make(clock, delay=240):
    runner = MagicMock()
    runner.is_running.return_value = False
    scheduler = sch.AutoScheduler(runner, clock=clock, delay_sampler=lambda lo, hi: delay)
    scheduler._click_and_wait = MagicMock(return_value={"skipped": False})
    # fake time does not advance the real monotonic clock; isolate the tested cadence.
    scheduler._mark_remote_stage_finished = lambda: None
    return scheduler


def state():
    return sch._read_slot_state(sch._slot_state_path())


def due():
    return sch._triple_due(state())


def arm(scheduler, clock):
    scheduler._plan_triple(clock.now - timedelta(seconds=240), None)


@pytest.mark.parametrize("delay", [240, 420])
def test_delay_inclusive_boundaries(delay):
    clock = Clock()
    scheduler = make(clock, delay)
    scheduler._tick_triple(clock.now)
    assert due() == clock.now + timedelta(seconds=delay)
    scheduler._click_and_wait.assert_not_called()


def test_independent_delays_are_not_fixed_cron():
    clock = Clock()
    scheduler = make(clock)
    scheduler._delay_sampler = MagicMock(side_effect=[240, 301, 359, 420])
    for delay in [240, 301, 359, 420]:
        old = state().get(sch._TRIPLE_NEXT_RUN)
        scheduler._plan_triple(clock.now, old)
        assert due() == clock.now + timedelta(seconds=delay)
        clock.now = due()
    assert scheduler._delay_sampler.call_count == 4
    scheduler._delay_sampler.assert_called_with(240, 420)


def test_seeded_samples_all_in_range():
    clock = Clock()
    scheduler = make(clock)
    scheduler._delay_sampler = random.Random(38).randint
    delays = set()
    for _ in range(300):
        scheduler._plan_triple(clock.now, state().get(sch._TRIPLE_NEXT_RUN))
        delay = (due() - clock.now).total_seconds()
        assert 240 <= delay <= 420
        delays.add(delay)
    assert len(delays) > 100


@pytest.mark.parametrize("delay", [239, 421, 240.5, True])
def test_invalid_sampler_fails_closed(delay):
    clock = Clock()
    scheduler = make(clock, delay)
    with pytest.raises(sch.SlotStateError):
        scheduler._tick_triple(clock.now)
    assert not sch._slot_state_path().exists()
    scheduler._click_and_wait.assert_not_called()


def test_interval_starts_at_completion_not_start():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    def complete(action):
        assert state()["triple"] == f"jitter-{state()[sch._TRIPLE_NEXT_RUN]}"
        clock.now += timedelta(minutes=1)
        return {"skipped": False}
    scheduler._click_and_wait.side_effect = complete
    scheduler._tick_triple(clock.now)
    assert due() == datetime(2026, 9, 20, 10, 5, tzinfo=sch.CN_TZ)


def test_inflight_tick_cannot_overlap():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    entered, release = threading.Event(), threading.Event()
    def work(action):
        entered.set()
        assert release.wait(5)
        return {"skipped": False}
    scheduler._click_and_wait.side_effect = work
    worker = threading.Thread(target=scheduler._tick_triple, args=(clock.now,))
    worker.start()
    try:
        assert entered.wait(5)
        clock.now += timedelta(minutes=20)
        scheduler._tick_triple(clock.now)
        scheduler._click_and_wait.assert_called_once()
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert due() == clock.now + timedelta(seconds=240)


def test_restart_preserves_due_and_same_slot_runs_once():
    clock = Clock()
    scheduler = make(clock, 420)
    scheduler._tick_triple(clock.now)
    original = sch._slot_state_path().read_bytes()
    for _ in range(4):
        restarted = make(clock)
        restarted._delay_sampler = MagicMock(side_effect=AssertionError("must not resample"))
        restarted._tick_triple(clock.now)
        assert sch._slot_state_path().read_bytes() == original
    clock.now = due()
    restarted._delay_sampler = lambda lo, hi: 240
    restarted._tick_triple(clock.now)
    restarted._click_and_wait.assert_called_once_with("participate_triple")
    for _ in range(4):
        later = make(clock)
        later._tick_triple(clock.now)
        later._click_and_wait.assert_not_called()


def test_actual_process_restart_keeps_plan_and_does_not_replay(tmp_path):
    clock = Clock()
    make(clock, 420)._tick_triple(clock.now)
    saved = due()
    script = """
import json, sys
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock
from tests.offline_guard import install_offline_guard
install_offline_guard()
from src import data_paths
data_paths.get_profile_dir = lambda profile_id=None: Path(sys.argv[1])
import web.auto_scheduler as sch
now = datetime.fromisoformat(sys.argv[2])
runner = MagicMock()
runner.is_running.return_value = False
sampler = MagicMock(return_value=240)
scheduler = sch.AutoScheduler(runner, clock=lambda: now, delay_sampler=sampler)
scheduler._schedule_auto_snapshot = lambda **kw: None
scheduler._click_and_wait = MagicMock(return_value={'skipped': False})
scheduler._tick_triple(now)
print(json.dumps({'calls': scheduler._click_and_wait.call_count, 'samples': sampler.call_count}))
"""
    def child(at):
        result = subprocess.run([sys.executable, "-c", script, str(tmp_path), at.isoformat()],
                                capture_output=True, text=True, timeout=30, check=True)
        return json.loads(result.stdout.splitlines()[-1])
    assert child(clock.now) == {"calls": 0, "samples": 0}
    assert due() == saved
    assert child(saved) == {"calls": 1, "samples": 1}
    assert child(saved) == {"calls": 0, "samples": 0}


def test_claim_crash_never_replays_consumed_slot():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    sch._persist_slot_claimed("triple", f"jitter-{state()[sch._TRIPLE_NEXT_RUN]}")
    restarted = make(clock)
    restarted._tick_triple(clock.now)
    restarted._click_and_wait.assert_not_called()
    assert due() == clock.now + timedelta(seconds=240)


def test_missed_minute_does_not_catch_up():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    clock.now += timedelta(days=4)
    for _ in range(10):
        scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_not_called()
    assert due() == clock.now + timedelta(seconds=240)


def test_due_second_and_existing_minute_window():
    clock = Clock()
    clock.now += timedelta(seconds=20)
    scheduler = make(clock)
    arm(scheduler, clock)
    scheduler._tick_triple(clock.now - timedelta(seconds=1))
    scheduler._click_and_wait.assert_not_called()
    clock.now += timedelta(seconds=30)
    scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_called_once()


def test_profiles_have_independent_plans(tmp_path, monkeypatch):
    clock = Clock()
    for name, delay in [("a", 240), ("b", 420)]:
        monkeypatch.setattr(sch, "_slot_state_path", lambda name=name: tmp_path / name / "scheduler_slots.json")
        make(clock, delay)._tick_triple(clock.now)
    for name, delay in [("a", 240), ("b", 420)]:
        monkeypatch.setattr(sch, "_slot_state_path", lambda name=name: tmp_path / name / "scheduler_slots.json")
        make(clock)._tick_triple(clock.now)
        assert due() == clock.now + timedelta(seconds=delay)


@pytest.mark.parametrize("mode", ["stop", "generation"])
def test_stale_cannot_plan_or_dispatch(mode):
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    original = sch._slot_state_path().read_bytes()
    if mode == "stop":
        scheduler._stop_event.set()
    else:
        scheduler._ctx.generation = scheduler._generation - 1
    scheduler._tick_triple(clock.now)
    scheduler._plan_triple(clock.now, state()[sch._TRIPLE_NEXT_RUN])
    assert sch._slot_state_path().read_bytes() == original
    scheduler._click_and_wait.assert_not_called()


def test_stopped_or_timed_out_job_must_finish_before_new_delay():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    raw = state()[sch._TRIPLE_NEXT_RUN]
    sch._persist_slot_claimed("triple", f"jitter-{raw}")
    scheduler._runner.is_running.return_value = True
    clock.now += timedelta(minutes=10)
    scheduler._tick_triple(clock.now)
    assert state()[sch._TRIPLE_NEXT_RUN] == raw
    scheduler._runner.is_running.return_value = False
    scheduler._tick_triple(clock.now)
    assert due() == clock.now + timedelta(seconds=240)
    scheduler._click_and_wait.assert_not_called()


@pytest.mark.parametrize("bad", ["bad", "2026-09-20T10:00:00", 123, None])
def test_invalid_saved_due_fails_closed(bad):
    clock = Clock()
    scheduler = make(clock)
    sch._slot_state_path().write_text(json.dumps({sch._TRIPLE_NEXT_RUN: bad}), encoding="utf-8")
    with pytest.raises(sch.SlotStateError):
        scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_not_called()


def test_write_failure_before_dispatch_zero_jobs(monkeypatch):
    clock = Clock()
    scheduler = make(clock)
    monkeypatch.setattr(sch, "_write_slot_state_atomic", MagicMock(side_effect=OSError("disk")))
    with pytest.raises(sch.SlotStateError):
        scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_not_called()


def test_completion_write_failure_keeps_consumed_barrier(monkeypatch):
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    raw = state()[sch._TRIPLE_NEXT_RUN]
    write = sch._write_slot_state_atomic
    def fail_next_plan(path, data):
        if data.get(sch._TRIPLE_NEXT_RUN) != raw:
            raise OSError("disk full after job")
        write(path, data)
    monkeypatch.setattr(sch, "_write_slot_state_atomic", fail_next_plan)
    with pytest.raises(sch.SlotStateError):
        scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_called_once()
    assert state()["triple"] == f"jitter-{raw}"
    monkeypatch.setattr(sch, "_write_slot_state_atomic", write)
    restarted = make(clock)
    restarted._tick_triple(clock.now)
    restarted._click_and_wait.assert_not_called()
    assert due() == clock.now + timedelta(seconds=240)


def test_stop_during_round_leaves_claim_for_new_generation():
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    raw = state()[sch._TRIPLE_NEXT_RUN]
    def stop_inflight(action):
        scheduler._stop_event.set()
        scheduler._runner.is_running.return_value = True
        return {"stopped": True}
    scheduler._click_and_wait.side_effect = stop_inflight
    scheduler._tick_triple(clock.now)
    assert state()[sch._TRIPLE_NEXT_RUN] == raw
    restarted = make(clock)
    restarted._runner = scheduler._runner
    clock.now += timedelta(minutes=20)
    restarted._tick_triple(clock.now)
    assert state()[sch._TRIPLE_NEXT_RUN] == raw
    restarted._runner.is_running.return_value = False
    restarted._tick_triple(clock.now)
    assert due() == clock.now + timedelta(seconds=240)
    restarted._click_and_wait.assert_not_called()


def test_stale_compare_and_swap_cannot_replace_new_plan():
    clock = Clock()
    scheduler = make(clock)
    scheduler._tick_triple(clock.now)
    original = sch._slot_state_path().read_bytes()
    scheduler._delay_sampler = MagicMock(side_effect=AssertionError("lost CAS cannot sample"))
    scheduler._plan_triple(clock.now, None)
    assert sch._slot_state_path().read_bytes() == original


def test_real_dispatch_keeps_from_auto_and_jobrunner_collision(monkeypatch):
    from web.job_runner import JobStatus
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    scheduler._click_and_wait = sch.AutoScheduler._click_and_wait.__get__(scheduler)
    scheduler._runner.try_start.return_value = 38
    scheduler._wait_until_terminal = MagicMock(return_value={"state": "success", "message": "done"})
    scheduler._tick_triple(clock.now)
    scheduler._runner.try_start.assert_called_once_with("participate_triple", {"from_auto": True}, source="auto")
    clock.now = due()
    scheduler._runner.is_running.return_value = True
    scheduler._runner.get_status.return_value = JobStatus(state="running", action="participate_triple")
    scheduler._tick_triple(clock.now)
    assert scheduler._runner.try_start.call_count == 1
    scheduler._runner.cancel.assert_not_called()


def test_state_reads_do_not_write_or_sample():
    clock = Clock()
    scheduler = make(clock)
    scheduler._tick_triple(clock.now)
    original = sch._slot_state_path().read_bytes()
    scheduler._delay_sampler = MagicMock(side_effect=AssertionError("read sampled"))
    for _ in range(3):
        assert sch.next_auto_task(clock.now)["at_unix"] == int(due().timestamp())
        assert sch._next_runs_by_task(clock.now)["participate"] == int(due().timestamp())
        scheduler.get_status()
    assert sch._slot_state_path().read_bytes() == original


@pytest.mark.parametrize("code", [-352, -509, 429])
def test_risk_cooldown_blocks_due_plan(code):
    from web.auto_remote_state import record_auto_remote_risk
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    record_auto_remote_risk(trigger_stage="test", reason=f"API error {code}", code=str(code))
    for _ in range(4):
        scheduler._tick_triple(clock.now)
        clock.now = due()
    scheduler._click_and_wait.assert_not_called()


@pytest.mark.parametrize("hour, minute", [(7, 10), (12, 40), (18, 10), (22, 40), (8, 0), (11, 0), (12, 10), (22, 10)])
def test_random_plan_yields_to_all_fixed_task_minutes(hour, minute):
    clock = Clock()
    clock.now = clock.now.replace(hour=hour, minute=minute)
    scheduler = make(clock)
    arm(scheduler, clock)
    scheduler._tick_triple(clock.now)
    scheduler._click_and_wait.assert_not_called()
    assert due() == clock.now + timedelta(seconds=240)


def test_other_schedules_snapshot_unchanged():
    snapshot = {}
    now = datetime(2026, 9, 19, 23, 59, tzinfo=sch.CN_TZ)
    for kind, _, at in sch._next_occurrence_candidates(now):
        snapshot.setdefault(kind, []).append(at.strftime("%H:%M"))
    assert snapshot == {
        "refresh": ["07:10", "12:40", "18:10", "22:40"],
        "following": ["08:00", "11:00", "14:00", "17:00", "20:00", "23:00"],
        "winner": ["12:10", "22:10"],
    }
    assert [s["action"] for s in sch.REFRESH_STEPS] == ["refresh_all", "refresh_watch", "refresh_status"]
    from web.auto_config import CLEANUP_MAINTAIN_INTERVAL_HOURS

    assert CLEANUP_MAINTAIN_INTERVAL_HOURS == 24


def run_one_loop_tick(scheduler, clock):
    stop = threading.Event()
    stop.wait = lambda timeout: stop.set()
    scheduler._loop(0, stop)


def test_winner_actual_loop_twice_daily_and_restart():
    clock = Clock()
    clock.now = clock.now.replace(hour=12, minute=9)
    scheduler = make(clock)
    run_one_loop_tick(scheduler, clock)
    scheduler._click_and_wait.assert_not_called()
    clock.now += timedelta(minutes=1)
    scheduler = make(clock)
    run_one_loop_tick(scheduler, clock)
    scheduler._click_and_wait.assert_called_once_with("winner_deep_scan")
    for offset in [0, 1]:
        clock.now = clock.now.replace(minute=10 + offset)
        restarted = make(clock)
        run_one_loop_tick(restarted, clock)
        restarted._click_and_wait.assert_not_called()
    clock.now = clock.now.replace(hour=22, minute=10)
    next_day = make(clock)
    run_one_loop_tick(next_day, clock)
    next_day._click_and_wait.assert_called_once_with("winner_deep_scan")


def test_winner_missed_minute_is_not_replayed():
    clock = Clock()
    clock.now = clock.now.replace(hour=12, minute=11)
    scheduler = make(clock)
    run_one_loop_tick(scheduler, clock)
    scheduler._click_and_wait.assert_not_called()
    assert "winner_scan" not in state()


def test_due_plan_preserves_budget_failure_and_no_catchup():
    from src.bilibili_request_budget import (
        bilibili_request_budget_scope, reserve_bilibili_http_attempt,
    )
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    mutation = MagicMock()
    endpoint = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail"
    def protected_job(action):
        with bilibili_request_budget_scope(limit=1, window_seconds=21600):
            reserve_bilibili_http_attempt(endpoint)
            mutation()
        return {"skipped": False}
    with bilibili_request_budget_scope(limit=1, window_seconds=21600):
        reserve_bilibili_http_attempt(endpoint)
    scheduler._click_and_wait.side_effect = protected_job
    for _ in range(3):
        scheduler._tick_triple(clock.now)
        clock.now = due()
    mutation.assert_not_called()
    assert scheduler._status.tasks["participate"]["status"] == "error"
    assert scheduler._status.state != "fatal"
    assert scheduler._click_and_wait.call_count == 3


@pytest.mark.parametrize("status", ["pending", "unknown", "suspected"])
def test_guard_zero_resend_across_fast_rounds(isolated_home, status):
    from src import lottery_actions as actions
    from src.participation_guard import record_pending, mark_repost_unknown, mark_repost_suspected, get_guard
    uid, dynamic_id = "12345", "1000000000000000001"
    if status == "suspected":
        mark_repost_suspected(uid, dynamic_id)
    else:
        record_pending(uid, dynamic_id)
        if status == "unknown":
            mark_repost_unknown(uid, dynamic_id)
    clock = Clock()
    scheduler = make(clock)
    arm(scheduler, clock)
    client = MagicMock()
    def guarded_job(action):
        result = actions.repost_dynamic(client, dynamic_id=dynamic_id, my_uid=uid,
                                        csrf="fake", referer="https://t.bilibili.com/" + dynamic_id,
                                        content="fake")
        assert result.ok is False
        return {"skipped": True}
    scheduler._click_and_wait.side_effect = guarded_job
    for _ in range(5):
        scheduler._tick_triple(clock.now)
        clock.now = due()
    client.post_form.assert_not_called()
    assert get_guard(uid, dynamic_id).repost_status == status


def test_poison_candidate_backoff_survives_many_fast_rounds(monkeypatch):
    from web import activity_service as activity
    from tests.test_participate_triple import _row, _id
    start = Clock().now.timestamp()
    rows = [{**_row(_id(701), can_participate=True), "last_participation": {
        "status": "failed", "recorded_at": start,
        "context_snapshot": {"result_class": "failed_local", "backoff_until": start + 3600},
    }}, _row(_id(702), can_participate=True)]
    clock = Clock()
    clock.now = clock.now.replace(minute=1)
    scheduler = make(clock)
    arm(scheduler, clock)
    monkeypatch.setattr(activity.time, "time", lambda: clock.now.timestamp())
    selected = []
    def pick(action):
        selected.extend(row["dynamic_id"] for row in activity._pick_triple_from_rows(rows))
        return {"skipped": False}
    scheduler._click_and_wait.side_effect = pick
    for _ in range(15):
        scheduler._tick_triple(clock.now)
        clock.now = due()
    assert selected == [_id(702)] * 15
    scheduler._tick_triple(clock.now)
    assert selected[-2:] == [_id(701), _id(702)]
