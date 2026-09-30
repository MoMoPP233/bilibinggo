from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import web.auto_scheduler as scheduler_mod
from web.auto_scheduler import AutoScheduler


@pytest.fixture
def slot_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: tmp_path / "scheduler_slots.json")
    monkeypatch.setattr("src.data_paths.get_profile_dir", lambda profile_id=None: tmp_path)
    return tmp_path


def _one_tick(scheduler: AutoScheduler) -> None:
    stop = threading.Event()
    stop.wait = lambda _timeout: stop.set()  # type: ignore[method-assign]
    scheduler._loop(0, stop)


def test_0405_does_not_start_cleanup(slot_root: Path) -> None:
    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._clock = lambda: datetime(2026, 9, 30, 4, 5, tzinfo=scheduler_mod.CN_TZ)

    _one_tick(scheduler)

    runner.try_start.assert_not_called()


def test_fixed_scheduler_tasks_never_include_cleanup(slot_root: Path) -> None:
    now = datetime(2026, 9, 30, 4, 5, tzinfo=scheduler_mod.CN_TZ)
    assert all(kind != "cleanup" for kind, _label, _due in scheduler_mod._next_occurrence_candidates(now))
    assert scheduler_mod.next_auto_task(now)["action"] != "cleanup_maintenance_cycle"


def test_due_legacy_resume_is_ignored_by_scheduler(slot_root: Path) -> None:
    path = slot_root / "scheduler_slots.json"
    path.write_text(
        json.dumps(
            {
                "cleanup_budget_resume": json.dumps(
                    {"profile_id": "account-a", "window_id": "100:200", "available_at": 100}
                )
            }
        ),
        encoding="utf-8",
    )
    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._clock = lambda: datetime(2026, 9, 30, 16, 5, tzinfo=scheduler_mod.CN_TZ)

    _one_tick(scheduler)

    runner.try_start.assert_not_called()
