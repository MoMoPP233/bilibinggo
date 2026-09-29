from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import web.auto_scheduler as scheduler_mod
from web.auto_config import CLEANUP_HOURS, CLEANUP_MINUTE
from web.auto_scheduler import AutoScheduler


@pytest.fixture
def slot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: tmp_path / "scheduler_slots.json")
    monkeypatch.setattr("src.data_paths.get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr("src.data_paths.get_runtime_profile_id", lambda: "account-a")
    return tmp_path


def _one_tick(scheduler: AutoScheduler) -> None:
    stop = __import__("threading").Event()
    stop.wait = lambda _timeout: stop.set()  # type: ignore[method-assign]
    scheduler._loop(0, stop)


def test_only_0405_is_a_fixed_cleanup_slot() -> None:
    assert CLEANUP_HOURS == frozenset({4})
    assert CLEANUP_MINUTE == 5


def test_1605_does_not_start_fixed_cleanup(slot_root: Path) -> None:
    scheduler = AutoScheduler(job_runner=MagicMock())
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda key, **kwargs: calls.append(key)  # type: ignore[method-assign]
    scheduler._clock = lambda: datetime(2026, 9, 30, 16, 5, tzinfo=scheduler_mod.CN_TZ)

    _one_tick(scheduler)

    assert calls == []


def test_0405_starts_once_and_same_day_tick_does_not_repeat(slot_root: Path) -> None:
    scheduler = AutoScheduler(job_runner=MagicMock())
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda key, **kwargs: calls.append(key) or scheduler._done_cleanup.add(key)  # type: ignore[method-assign]
    scheduler._clock = lambda: datetime(2026, 9, 30, 4, 5, tzinfo=scheduler_mod.CN_TZ)

    _one_tick(scheduler)
    _one_tick(scheduler)

    assert calls == ["cleanup-2026-09-30-04"]


def test_next_day_0405_gets_a_new_fixed_slot(slot_root: Path) -> None:
    scheduler = AutoScheduler(job_runner=MagicMock())
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda key, **kwargs: calls.append(key) or scheduler._done_cleanup.add(key)  # type: ignore[method-assign]
    clocks = iter(
        [
            datetime(2026, 9, 30, 4, 5, tzinfo=scheduler_mod.CN_TZ),
            datetime(2026, 10, 1, 4, 5, tzinfo=scheduler_mod.CN_TZ),
        ]
    )
    scheduler._clock = lambda: next(clocks)

    _one_tick(scheduler)
    _one_tick(scheduler)

    assert calls == ["cleanup-2026-09-30-04", "cleanup-2026-10-01-04"]


def test_resume_due_at_1605_is_not_a_fixed_slot_but_can_run_resume(
    slot_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert scheduler_mod._save_cleanup_budget_resume(
        {
            "budget_exhausted": True,
            "window_started_at": 100,
            "window_expires_at": 200,
            "resume_after": 200,
        }
    )
    scheduler = AutoScheduler(job_runner=MagicMock())
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", lambda: (False, ""))
    monkeypatch.setattr("src.repost_cleanup.cleanup_has_pending_local_work", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_remote_gate",
        lambda: scheduler_mod.RemoteGateDecision(True, "ok", ""),
    )
    calls: list[str] = []
    scheduler._run_cleanup_cycle = lambda key, **kwargs: calls.append(key)  # type: ignore[method-assign]

    now = datetime.fromtimestamp(200, scheduler_mod.CN_TZ)
    assert scheduler._maybe_run_cleanup_budget_resume(now) is True
    assert calls == ["cleanup-resume-100:200"]
