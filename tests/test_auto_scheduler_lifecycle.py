"""AutoScheduler 生命周期 V2：每代独立 stop Event + generation。

全部使用 fake runner，零真实网络。
覆盖：快速 stop→start 不再产生双调度资格、旧代晚醒不启动 Job、
旧代退出不污染新代、stop 不 cancel/不 join、同分钟不重跑、隐藏竞态 A~F。
"""

from __future__ import annotations

import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import web.auto_scheduler as scheduler_mod
from web import auto_remote_state as state_mod
from web.auto_config import REFRESH_HOURS
from web.auto_scheduler import AutoScheduler
from web.job_runner import JobStatus

CN_TZ = timezone(timedelta(hours=8))
real_sleep = time.sleep
SCHEDULER_THREAD_NAME = "binggo-auto-scheduler"


class FrozenClock(datetime):
    """把调度器内所有 datetime.now(CN_TZ) 冻结到目标时刻，便于触发刻度。"""

    _fixed: datetime | None = None

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        if cls._fixed is not None:
            return cls._fixed
        return super().now(tz)


class StubRunner:
    """可暂停的 fake JobRunner：Job 一直 running，直到 finish_ev 被设置。"""

    def __init__(self, *, finish_ev: threading.Event | None = None) -> None:
        self.finish_ev = finish_ev or threading.Event()
        self.running = False
        self.started: list[str] = []
        self.cancel_calls = 0
        self._lock = threading.Lock()
        self._job_id = 100

    def is_running(self) -> bool:
        return self.running

    def try_start(self, action: str, params: dict, source: str = "auto") -> int | None:
        with self._lock:
            if self.running:
                return None
            self.running = True
            self.started.append(action)
            self._job_id += 1
            return self._job_id

    def cancel(self) -> None:
        with self._lock:
            self.cancel_calls += 1
            self.running = False

    def get_status(self) -> JobStatus:
        return self.resolve_job_status(self._job_id)

    def resolve_job_status(self, job_id: int) -> JobStatus:
        if self.running and self.finish_ev.is_set():
            # 模拟真实 JobRunner：终态后槽位空闲，后续 try_start 可再启动。
            self.running = False
            return JobStatus(
                id=job_id,
                state="success",
                action=self.started[-1] if self.started else "",
                message="done",
            )
        if not self.running:
            return JobStatus(
                id=job_id,
                state="idle",
                action="",
                message="",
            )
        return JobStatus(
            id=job_id,
            state="running",
            action=self.started[-1] if self.started else "",
            message="working",
            progress_message="working",
        )

    def release(self) -> None:
        self.finish_ev.set()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "profile-a" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)
    return target


@pytest.fixture
def fast_clock(monkeypatch):
    """把调度时钟冻结在整点刷新刻度（选非维护/非 following 的整点）。"""
    refresh_hour = next(h for h in sorted(REFRESH_HOURS) if h % 2 == 1 and h % 6 != 1)
    today = datetime.now(CN_TZ).replace(
        hour=refresh_hour, minute=0, second=0, microsecond=0
    )
    FrozenClock._fixed = today
    monkeypatch.setattr(scheduler_mod, "datetime", FrozenClock)
    monkeypatch.setattr(scheduler_mod, "JOB_POLL_INTERVAL_SEC", 0.05)
    monkeypatch.setattr(
        scheduler_mod.time,
        "sleep",
        lambda seconds: real_sleep(min(float(seconds), 0.03)),
    )
    return today


def scheduler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == SCHEDULER_THREAD_NAME]


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        real_sleep(0.02)
    return False


def stop_and_cleanup(scheduler: AutoScheduler, runner: StubRunner) -> None:
    runner.release()
    scheduler.stop()
    wait_until(lambda: not any(t.is_alive() for t in scheduler_threads()), timeout=3.0)


def test_second_start_while_running_raises_with_single_thread(
    fast_clock, state_path
) -> None:
    scheduler = AutoScheduler(job_runner=StubRunner())
    scheduler.start()
    assert scheduler._status.state == "running"
    assert scheduler._generation == 1
    with pytest.raises(RuntimeError, match="已在运行"):
        scheduler.start()
    assert scheduler._generation == 1
    assert len(scheduler_threads()) == 1
    stop_and_cleanup(scheduler, scheduler._runner)


def test_concurrent_start_apis_create_only_one_generation(fast_clock, state_path) -> None:
    scheduler = AutoScheduler(job_runner=StubRunner())
    barrier = threading.Barrier(3)
    outcomes: list[str] = []

    def caller() -> None:
        barrier.wait()
        try:
            scheduler.start()
            outcomes.append("ok")
        except RuntimeError as exc:
            outcomes.append(str(exc))

    workers = [threading.Thread(target=caller, daemon=True) for _ in range(2)]
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(timeout=5)

    assert outcomes.count("ok") == 1
    assert any("已在运行" in text for text in outcomes if text != "ok")
    assert scheduler._generation == 1
    assert len(scheduler_threads()) == 1
    assert scheduler._status.state == "running"
    stop_and_cleanup(scheduler, scheduler._runner)


def test_old_generation_never_clears_new_generation_state(
    fast_clock, state_path
) -> None:
    scheduler = AutoScheduler(job_runner=StubRunner())
    scheduler.start()
    first_thread = scheduler._thread
    first_event = scheduler._stop_event

    scheduler.stop()
    assert wait_until(lambda: not first_thread.is_alive())

    scheduler.start()
    second_thread = scheduler._thread
    assert scheduler._generation == 2
    assert scheduler._status.state == "running"
    # 旧代 Event 仍为 set（旧线程确实被 stop 过），新代 Event 未 set。
    assert first_event.is_set() is True
    assert scheduler._stop_event is not first_event
    assert scheduler._stop_event.is_set() is False
    assert scheduler._thread is second_thread
    stop_and_cleanup(scheduler, scheduler._runner)


def test_old_generation_wakes_late_but_cannot_start_any_job(
    fast_clock, state_path
) -> None:
    """旧代线程停在 Job 等待中，stop→start 后即使 Job 结束晚醒也不能再调度。"""
    runner = StubRunner()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler.start()
    assert wait_until(lambda: len(runner.started) == 1, timeout=3.0), "gen1 未点击"
    assert wait_until(lambda: "等待结束" in scheduler._status.current_phase, timeout=3.0)

    scheduler.stop()  # 不 join、不 cancel，Job 继续 running
    scheduler.start()  # 新一代立即接管
    second_thread = scheduler._thread

    # 新代同一刻度撞见旧 Job：collision skip，不 try_start、不 fatal。
    real_sleep(0.3)
    assert len(runner.started) == 1
    assert scheduler._status.state == "running"
    assert scheduler._status.fatal_error is None

    # 旧 Job 自然结束：旧代晚醒，只应退出，不得启动后续 stage。
    runner.release()
    assert wait_until(lambda: not any(
        t is not second_thread and t.name == SCHEDULER_THREAD_NAME and t.is_alive()
        for t in threading.enumerate()
    ), timeout=3.0)
    real_sleep(0.3)

    assert runner.started == ["refresh_all"]
    assert runner.cancel_calls == 0
    assert scheduler._thread is second_thread
    assert scheduler._status.state == "running"
    stop_and_cleanup(scheduler, runner)


def test_stop_returns_fast_and_never_kills_running_job(
    fast_clock, state_path
) -> None:
    runner = StubRunner()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler.start()
    assert wait_until(lambda: len(runner.started) == 1, timeout=3.0)
    assert wait_until(lambda: "等待结束" in scheduler._status.current_phase, timeout=3.0)

    started_mono = time.monotonic()
    scheduler.stop()
    elapsed = time.monotonic() - started_mono

    assert elapsed < 1.0, "stop 必须立即返回"
    assert runner.cancel_calls == 0
    assert runner.running is True  # Job 自然继续
    assert scheduler._status.state == "stopped"
    stop_and_cleanup(scheduler, runner)


def test_new_generation_collision_skips_without_fatal_when_old_job_running(
    fast_clock, state_path
) -> None:
    runner = StubRunner()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler.start()
    assert wait_until(lambda: len(runner.started) == 1, timeout=3.0)
    scheduler.stop()
    scheduler.start()

    real_sleep(0.3)
    assert runner.started == ["refresh_all"]  # 新代 collision skip
    assert runner.cancel_calls == 0
    assert scheduler._status.state == "running"
    assert scheduler._status.fatal_error is None
    stop_and_cleanup(scheduler, runner)


def test_stale_generation_cannot_fatal_or_pause_new_generation(
    fast_clock, state_path
) -> None:
    scheduler = AutoScheduler(job_runner=StubRunner())
    scheduler.start()
    scheduler.stop()
    scheduler.start()
    assert scheduler._generation == 2

    poll: list[str] = []

    def old_gen_caller() -> None:
        # 模拟旧代线程：线程级上下文仍绑定 generation=1，实例已换代到 2。
        scheduler._ctx.generation = 1
        scheduler._ctx.stop_event = threading.Event()
        scheduler._fatal("旧代撞车晚到")
        poll.append("fatal-called")
        scheduler._pause_all_auto_remote("participate_triple", "旧代风控晚到")
        poll.append("pause-called")

    thread = threading.Thread(target=old_gen_caller, daemon=True)
    thread.start()
    thread.join(timeout=5)

    assert poll == ["fatal-called", "pause-called"]
    assert scheduler._status.state == "running"
    assert scheduler._status.fatal_error is None
    assert state_mod.is_risk_paused() is False
    stop_and_cleanup(scheduler, scheduler._runner)


def test_same_minute_completed_slot_is_not_rerun_after_stop_start(
    fast_clock, state_path
) -> None:
    runner = StubRunner()
    runner.finish_ev.set()  # Job 立即成功，批次跑完整三个 stage
    scheduler = AutoScheduler(job_runner=runner)
    scheduler.start()

    assert wait_until(lambda: len(runner.started) >= 3, timeout=3.0), "gen1 未跑完整批"
    assert wait_until(lambda: bool(scheduler._done_refresh), timeout=3.0)

    scheduler.stop()
    scheduler.start()
    real_sleep(0.3)

    # 同一分钟 stop→start：已完成的 slot 不得因换代重跑。
    assert len(runner.started) == 3
    assert runner.cancel_calls == 0
    stop_and_cleanup(scheduler, runner)


def test_multiple_fast_stop_start_cycles_leave_only_latest_generation(
    fast_clock, state_path
) -> None:
    scheduler = AutoScheduler(job_runner=StubRunner())
    for cycle in range(1, 4):
        scheduler.start()
        assert scheduler._generation == cycle
        assert scheduler._status.state == "running"
        assert len(scheduler_threads()) == 1
        scheduler.stop()
        assert scheduler._status.state == "stopped"
        assert wait_until(lambda: not any(
            t.is_alive() for t in scheduler_threads()
        ), timeout=3.0)

    scheduler.start()
    assert scheduler._generation == 4
    assert scheduler._status.state == "running"
    assert len(scheduler_threads()) == 1
    stop_and_cleanup(scheduler, scheduler._runner)
