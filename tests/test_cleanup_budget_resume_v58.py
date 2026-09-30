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
def slot_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "scheduler_slots.json"
    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: path)
    return path


def _one_tick(scheduler: AutoScheduler) -> None:
    stop = threading.Event()
    stop.wait = lambda _timeout: stop.set()  # type: ignore[method-assign]
    scheduler._loop(0, stop)


def test_legacy_budget_resume_state_is_retained_but_never_read_or_claimed(
    slot_file: Path,
) -> None:
    legacy = {
        "profile_id": "account-a",
        "flow": "cleanup_maintenance_cycle",
        "window_id": "100:200",
        "available_at": 100,
    }
    slot_file.write_text(
        json.dumps(
            {
                "cleanup_budget_resume": json.dumps(legacy),
                "cleanup_budget_resume_claim": "100:200",
            }
        ),
        encoding="utf-8",
    )
    runner = MagicMock()
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._clock = lambda: datetime(2026, 9, 30, 4, 5, tzinfo=scheduler_mod.CN_TZ)

    _one_tick(scheduler)

    runner.try_start.assert_not_called()
    stored = json.loads(slot_file.read_text(encoding="utf-8"))
    assert stored["cleanup_budget_resume_claim"] == "100:200"


def test_expired_legacy_resume_does_not_create_cleanup_dispatch(slot_file: Path) -> None:
    slot_file.write_text(
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


def test_next_task_has_no_cleanup_slot(slot_file: Path) -> None:
    now = datetime(2026, 9, 30, 3, 0, tzinfo=scheduler_mod.CN_TZ)
    candidates = scheduler_mod._next_occurrence_candidates(now)
    assert all(kind != "cleanup" for kind, _label, _due in candidates)
    assert scheduler_mod.next_auto_task(now)["action"] != "cleanup_maintenance_cycle"
