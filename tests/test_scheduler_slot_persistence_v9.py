"""V9：AutoScheduler 自动槽位跨进程持久化防重复（全 mock / temp Profile state）。"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web.auto_scheduler as scheduler_mod
from web.auto_scheduler import AutoScheduler


@pytest.fixture
def slot_root(tmp_path: Path, monkeypatch) -> Path:
    from src import data_paths as _data_paths

    monkeypatch.setattr(_data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)
    return tmp_path


def _slot_file(root: Path) -> Path:
    return root / "scheduler_slots.json"


def _scheduler_with_count(count: list[int], slot_root: Path):
    scheduler = AutoScheduler(job_runner=None)
    scheduler._click_and_wait = lambda action, pipeline_index=None: count.append(action)
    return scheduler


def _count(holder: list[str]) -> int:
    return len(holder)


def test_first_run_missing_state_claims_and_dispatches_once(slot_root: Path) -> None:
    clicks: list[str] = []
    scheduler = _scheduler_with_count(clicks, slot_root)
    scheduler._run_refresh_batch("2026-09-06-03")  # 三步：refresh_all/watch/status
    assert _count(clicks) == 3
    scheduler._run_refresh_batch("2026-09-06-03")
    assert _count(clicks) == 3  # 同进程再次同 slot 不 dispatch
    assert _slot_file(slot_root).exists()


def test_restart_same_slot_does_not_dispatch(slot_root: Path) -> None:
    clicks_a: list[str] = []
    scheduler_a = _scheduler_with_count(clicks_a, slot_root)
    scheduler_a._run_refresh_batch("2026-09-06-03")
    assert _count(clicks_a) == 3

    # 模拟进程重启：新实例读取同一 temp Profile state。
    clicks_b: list[str] = []
    scheduler_b = _scheduler_with_count(clicks_b, slot_root)
    scheduler_b._run_refresh_batch("2026-09-06-03")
    assert clicks_b == []


def test_next_slot_after_restart_dispatches(slot_root: Path) -> None:
    clicks: list[str] = []
    scheduler_a = _scheduler_with_count(clicks, slot_root)
    scheduler_a._run_following_scan("following-2026-09-06-06")
    assert _count(clicks) == 1

    scheduler_b = _scheduler_with_count(clicks, slot_root)
    scheduler_b._run_following_scan("following-2026-09-06-12")
    assert _count(clicks) == 2


def test_profile_a_consumed_does_not_block_profile_b(tmp_path: Path, monkeypatch) -> None:
    from src import data_paths as _data_paths

    root_a = tmp_path / "pa"
    root_b = tmp_path / "pb"
    clicks: list[str] = []

    def make(root):
        monkeypatch.setattr(_data_paths, "get_profile_dir", lambda profile_id=None: root)
        scheduler = AutoScheduler(job_runner=None)
        scheduler._click_and_wait = lambda action, pipeline_index=None: clicks.append(action)
        return scheduler

    scheduler_a = make(root_a)
    scheduler_a._run_triple_slot("2026-09-06-03-05")
    assert _count(clicks) == 1
    scheduler_b = make(root_b)
    scheduler_b._run_triple_slot("2026-09-06-03-05")
    assert _count(clicks) == 2  # B 不受 A 影响


def test_persist_happens_before_any_remote_click(slot_root: Path) -> None:
    clicks: list[str] = []

    def fake_click(action, *, pipeline_index=None):
        # 第一次远程点击前，状态文件已包含本 slot（persist 严格先于 try_start）。
        data = json.loads(_slot_file(slot_root).read_text(encoding="utf-8"))
        assert data.get("refresh") == "2026-09-06-03"
        clicks.append(action)

    scheduler = AutoScheduler(job_runner=None)
    scheduler._click_and_wait = fake_click
    scheduler._run_refresh_batch("2026-09-06-03")
    assert _count(clicks) == 3


def test_crash_after_claim_before_try_start_no_rerun(slot_root: Path) -> None:
    # 直接模拟“persist 成功 → try_start 前进程崩溃”：claim 已落盘，从未 dispatch。
    assert scheduler_mod._persist_slot_claimed("refresh", "2026-09-06-09") is True
    clicks: list[str] = []
    scheduler_b = _scheduler_with_count(clicks, slot_root)
    scheduler_b._run_refresh_batch("2026-09-06-09")
    assert clicks == []  # 重启后同 slot 绝不补跑


def test_persist_failure_fails_closed_zero_remote(slot_root: Path, monkeypatch) -> None:
    clicks: list[str] = []
    scheduler = _scheduler_with_count(clicks, slot_root)

    def broken_write(path, state):
        raise OSError("disk full")

    monkeypatch.setattr(scheduler_mod, "_write_slot_state_atomic", broken_write)
    scheduler._run_refresh_batch("2026-09-06-03")
    assert clicks == []
    assert not _slot_file(slot_root).exists()


def test_corrupt_state_fails_closed(slot_root: Path) -> None:
    _slot_file(slot_root).write_text("{not-json", encoding="utf-8")
    clicks: list[str] = []
    scheduler = _scheduler_with_count(clicks, slot_root)
    scheduler._run_refresh_batch("2026-09-06-03")
    assert clicks == []


def test_missing_state_is_not_corrupt(slot_root: Path) -> None:
    clicks: list[str] = []
    scheduler = _scheduler_with_count(clicks, slot_root)
    scheduler._run_maintenance("maint-2026-09-06-04")
    assert _count(clicks) == 1


def test_concurrent_same_slot_single_winner(slot_root: Path) -> None:
    lock = threading.Lock()
    clicks: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            scheduler = _scheduler_with_count(clicks, slot_root)
            scheduler._run_refresh_batch("2026-09-06-06")
        except BaseException as exc:  # pragma: no cover
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not errors
    assert _count(clicks) == 3  # 只有一个线程拿到完整三步投递权


def test_different_tasks_same_time_do_not_clash(slot_root: Path) -> None:
    clicks: list[str] = []
    scheduler = _scheduler_with_count(clicks, slot_root)
    scheduler._run_refresh_batch("2026-09-06-03")
    scheduler._last_remote_stage_finished_mono = 0.0  # 取消 60s 错峰影响，仅验证 task 键互不覆盖
    scheduler._run_triple_slot("2026-09-06-03-05")
    assert clicks.count("refresh_all") == 1
    assert clicks.count("participate_triple") == 1


def test_collision_keeps_slot_consumed_across_restart(slot_root: Path) -> None:
    class RunnerBusy:
        def is_running(self):
            return True

        def get_status(self):
            return type("S", (), {"to_dict": lambda self: {"state": "running", "action": "x"}})()

    scheduler_a = AutoScheduler(job_runner=RunnerBusy())
    scheduler_a._run_refresh_batch("2026-09-06-12")

    clicks: list[str] = []
    scheduler_b = _scheduler_with_count(clicks, slot_root)
    scheduler_b._run_refresh_batch("2026-09-06-12")
    assert clicks == []


def test_stale_generation_cannot_claim(slot_root: Path) -> None:
    clicks: list[str] = []
    scheduler = AutoScheduler(job_runner=None)
    scheduler._generation = 2
    errors: list[BaseException] = []

    def stale_worker() -> None:
        try:
            scheduler._ctx.generation = 1
            scheduler._ctx.stop_event = threading.Event()
            scheduler._run_triple_slot("2026-09-06-03-05")
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    thread = threading.Thread(target=stale_worker)
    thread.start()
    thread.join(timeout=10)
    assert not errors
    assert clicks == []
    assert not _slot_file(slot_root).exists()  # stale generation 未写 slot


def test_state_write_is_atomic_no_partial_file(slot_root: Path) -> None:
    scheduler = _scheduler_with_count([], slot_root)
    scheduler._run_maintenance("maint-2026-09-06-08")
    assert _slot_file(slot_root).exists()
    leftovers = list(slot_root.glob(".scheduler_slots.json.*.tmp"))
    assert leftovers == []
    data = json.loads(_slot_file(slot_root).read_text(encoding="utf-8"))
    assert data["maintain"] == "maint-2026-09-06-08"
