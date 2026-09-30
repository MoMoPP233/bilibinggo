"""V72 调度时刻与夜间开关专项（fake clock / 本地文件，零真实网络）。"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import MagicMock

import pytest

CN_TZ = timezone(timedelta(hours=8))


@pytest.fixture(autouse=True)
def isolate_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from src import data_paths

    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


def test_night_quiet_boundaries_default_off():
    from web.auto_config import is_night_quiet

    assert is_night_quiet(datetime(2026, 9, 30, 23, 59, tzinfo=CN_TZ), config=False) is False
    assert is_night_quiet(datetime(2026, 10, 1, 0, 0, tzinfo=CN_TZ), config=False) is True
    assert is_night_quiet(datetime(2026, 10, 1, 6, 59, 59, tzinfo=CN_TZ), config=False) is True
    assert is_night_quiet(datetime(2026, 10, 1, 7, 0, tzinfo=CN_TZ), config=False) is False


def test_night_continue_persists_and_is_profile_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import web.auto_config as config
    from src import data_paths

    profile_a = tmp_path / "account-a"
    profile_b = tmp_path / "account-b"
    current = {"path": profile_a}
    monkeypatch.setattr(data_paths, "get_profile_dir", lambda profile_id=None: current["path"])

    assert config.get_night_continue_enabled() is False
    assert config.set_night_continue_enabled(True) is True
    assert config.get_night_continue_enabled() is True
    current["path"] = profile_b
    assert config.get_night_continue_enabled() is False


@pytest.mark.parametrize(
    ("hour", "minute", "allowed"),
    [(23, 59, True), (0, 0, False), (6, 59, False), (7, 0, True)],
)
def test_remote_gate_default_window_boundaries(hour: int, minute: int, allowed: bool):
    from web.auto_scheduler import AutoScheduler

    runner = MagicMock()
    scheduler = AutoScheduler(
        job_runner=runner,
        clock=lambda: datetime(2026, 10, 1, hour, minute, tzinfo=CN_TZ),
    )
    decision = scheduler._remote_gate()
    assert decision.allowed is allowed
    if not allowed:
        assert decision.kind == "night_quiet"
        assert "00:00～07:00" in decision.message
    runner.try_start.assert_not_called()


def test_remote_gate_night_continue_allows_remote_job(monkeypatch):
    from web.auto_config import set_night_continue_enabled
    from web.auto_scheduler import AutoScheduler

    set_night_continue_enabled(True)
    scheduler = AutoScheduler(
        job_runner=MagicMock(),
        clock=lambda: datetime(2026, 10, 1, 3, 0, tzinfo=CN_TZ),
    )
    assert scheduler._remote_gate().allowed is True


def test_participation_respects_night_gate_but_keeps_jitter(monkeypatch: pytest.MonkeyPatch):
    from web.auto_config import set_night_continue_enabled
    from web.auto_scheduler import AutoScheduler
    import src.comment_probe as comment_probe

    monkeypatch.setattr(comment_probe, "run_comment_recovery_probe", lambda: {"claimed": False})

    clock = {"now": datetime(2026, 10, 1, 6, 59, tzinfo=CN_TZ)}
    runner = MagicMock()
    runner.is_running.return_value = False
    scheduler = AutoScheduler(
        job_runner=runner,
        clock=lambda: clock["now"],
        delay_sampler=lambda low, high: 301,
    )
    scheduler._click_and_wait = MagicMock(return_value={"skipped": False})
    set_night_continue_enabled(False)
    scheduler._run_triple_slot("night-off")
    scheduler._click_and_wait.assert_not_called()

    clock["now"] = datetime(2026, 10, 1, 3, 0, tzinfo=CN_TZ)
    from web.auto_scheduler import _reset_slot_state_for_tests

    _reset_slot_state_for_tests()
    scheduler = AutoScheduler(
        job_runner=runner,
        clock=lambda: clock["now"],
        delay_sampler=lambda low, high: 301,
    )
    scheduler._click_and_wait = MagicMock(return_value={"skipped": False})
    set_night_continue_enabled(True)
    scheduler._run_triple_slot("night-on")
    scheduler._click_and_wait.assert_called_once_with("participate_triple")


def test_v72_fixed_slots_are_explicit_and_non_overlapping():
    from web.auto_scheduler import _dedicated_minute, _next_occurrence_candidates

    now = datetime(2026, 9, 30, 23, 59, tzinfo=CN_TZ)
    candidates = _next_occurrence_candidates(now)
    refresh = [due.strftime("%H:%M") for kind, _label, due in candidates if kind == "refresh"]
    following = [due.strftime("%H:%M") for kind, _label, due in candidates if kind == "following"]
    assert refresh == ["07:10", "12:40", "18:10", "22:40"]
    assert following == ["08:00", "11:00", "14:00", "17:00", "20:00", "23:00"]
    assert all(_dedicated_minute(due) for _kind, _label, due in candidates if _kind != "participate")
