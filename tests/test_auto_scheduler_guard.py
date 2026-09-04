from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web import auto_remote_state as state_mod
from web.auto_config import AUTO_REMOTE_RISK_COOLDOWN_SECONDS
from web.auto_scheduler import AutoScheduler


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch) -> Path:
    target = tmp_path / "profile-a" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)
    return target


def _scheduler_with_fake_click(fake):
    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    return scheduler


def _cleanup_paused_factory():
    holder = {"paused": False, "reason": ""}

    def read():
        return holder["paused"], holder["reason"]

    def set(paused: bool, reason: str = ""):
        holder["paused"] = paused
        holder["reason"] = reason

    return read, set


def test_risk_markers() -> None:
    for text in ("API error -352: risk", "-509 访问频繁", "HTTP 429 Too Many Requests", "rate-limit", "风控", "限流"):
        assert state_mod.matches_platform_risk(text) is True
    assert state_mod.matches_platform_risk("普通网络错误 ConnectError") is False


def test_record_risk_sets_cooldown_until(state_path: Path) -> None:
    state = state_mod.record_auto_remote_risk(
        trigger_stage="refresh:refresh_all", reason="-352", now_ts=1_000_000
    )
    assert state["paused"] is True
    assert state["expired"] is False
    assert state["paused_at"] == 1_000_000
    assert state["paused_until"] == 1_000_000 + AUTO_REMOTE_RISK_COOLDOWN_SECONDS
    # 无任何 manual 残留。
    raw = state_mod._read_state()
    assert "manual_paused" not in raw
    assert "manual_reason" not in raw


def test_refresh_risk_stops_batch_immediately_and_records_cooldown(state_path: Path) -> None:
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        if action == "refresh_all":
            raise RuntimeError("API error -352: 风控校验失败")
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_refresh_batch("2026-07-17-6")

    assert calls == ["refresh_all"]
    assert scheduler._status.state != "fatal"
    assert "2026-07-17-6" in scheduler._done_refresh
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert "refresh" in view["trigger_stage"]


def test_cooldown_blocks_consecutive_ticks_with_zero_remote(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-509")
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {}

    scheduler = _scheduler_with_fake_click(fake)
    for i in range(4):
        scheduler._run_triple_slot(f"triple-{i}")
        scheduler._run_maintenance(f"maint-{i}")
        scheduler._run_refresh_batch(f"refresh-{i}")
    assert calls == []
    assert scheduler._status.state != "fatal"


def test_cooldown_survives_new_instance(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="429")
    assert state_mod.is_risk_paused() is True
    # “重启”：新 scheduler 读同一 per-Profile 文件，冷却仍生效。
    assert AutoScheduler(job_runner=MagicMock())._remote_stage_allowed() is False


def test_profile_a_risk_does_not_affect_profile_b(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "account-a" / "auto_remote_state.json"
    b = tmp_path / "account-b" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: a)
    state_mod.record_auto_remote_risk(trigger_stage="refresh:refresh_all", reason="429")

    monkeypatch.setattr(state_mod, "_state_path", lambda: b)
    assert state_mod.is_risk_paused() is False

    monkeypatch.setattr(state_mod, "_state_path", lambda: a)
    assert state_mod.is_risk_paused() is True


def test_expired_cooldown_only_regrants_eligibility_without_probing(state_path: Path) -> None:
    # 把 paused_until 写到过去，模拟 6 小时已过。
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-352", now_ts=1000)
    raw = state_mod._read_state()
    raw["paused_until"] = int(time.time()) - 1
    state_mod._write_state(raw)

    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {}

    scheduler = _scheduler_with_fake_click(fake)
    # 冷却结束、非正常调度时间（直接构造的 wrapper 校验不会擅自联网：我们把
    # 上一大任务刚结束的错峰设满，防止 gap 影响语义，仅证明“不会主动探测”）。
    assert scheduler._remote_stage_allowed() is True  # 过期即清除、恢复资格
    assert state_mod.is_risk_paused() is False
    assert calls == []


def test_expired_then_next_normal_tick_runs_once(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="refresh:refresh_all", reason="429", now_ts=1000)
    raw = state_mod._read_state()
    raw["paused_until"] = int(time.time()) - 1
    state_mod._write_state(raw)

    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_triple_slot("2026-07-17-06-05")
    assert calls == ["participate_triple"]
    # 成功后仍未再次暂停，文件已清除。
    assert state_mod.is_risk_paused() is False


def test_rerisk_extends_cooldown(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="refresh:refresh_all", reason="429", now_ts=1000)
    first_until = state_mod._read_state()["paused_until"]
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-509", now_ts=5000)
    second_until = state_mod._read_state()["paused_until"]
    assert second_until > first_until
    assert second_until == 5000 + AUTO_REMOTE_RISK_COOLDOWN_SECONDS


def test_cleanup_risk_sets_global_cooldown(state_path: Path, monkeypatch) -> None:
    cleanup_read, cleanup_set = _cleanup_paused_factory()
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", cleanup_read)
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        cleanup_set(True, "自动维护命中平台风控（-352）")
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_maintenance("2026-07-17-06-maintain")

    assert calls == ["cleanup_auto_maintain"]
    assert cleanup_read()[0] is True
    assert state_mod.is_risk_paused() is True
    assert "cleanup" in state_mod.risk_pause_state()["trigger_stage"]


def test_cleanup_local_pause_auto_clears_after_cooldown(
    state_path: Path, monkeypatch,
) -> None:
    cleanup_read, cleanup_set = _cleanup_paused_factory()
    cleanup_set(True, "风控冷却已到期")
    monkeypatch.setattr("src.repost_cleanup.auto_maintenance_paused_state", cleanup_read)
    monkeypatch.setattr(
        "src.repost_cleanup.auto_clear_expired_maintenance_risk",
        lambda **kwargs: cleanup_set(False) or True,
    )
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_maintenance("2026-07-17-08-maintain")
    # 冷却到期 → 本地恢复资格 → 本轮（正常维护刻度）执行 cleanup。
    assert calls == ["cleanup_auto_maintain"]
    assert state_mod.is_risk_paused() is False


def test_cleanup_local_pause_within_cooldown_skips_zero_remote(
    state_path: Path, monkeypatch,
) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.auto_maintenance_paused_state",
        lambda: (True, "风控冷却中"),
    )
    monkeypatch.setattr(
        "src.repost_cleanup.auto_clear_expired_maintenance_risk",
        lambda **kwargs: False,
    )
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_maintenance("2026-07-17-08-maintain")
    assert calls == []
    assert "2026-07-17-08-maintain" in scheduler._done_maintain


def test_min_remote_stage_gap_still_blocks(state_path: Path) -> None:
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._last_remote_stage_finished_mono = time.monotonic()
    scheduler._run_triple_slot("2026-07-17-06-05")
    assert calls == []
    assert "2026-07-17-06-05" in scheduler._done_triple

    scheduler._last_remote_stage_finished_mono = time.monotonic() - 61
    scheduler._run_triple_slot("2026-07-17-06-10")
    assert calls == ["participate_triple"]


def test_refresh_batch_internal_steps_have_no_gap(state_path: Path) -> None:
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    scheduler._run_refresh_batch("2026-07-17-6")
    assert calls == ["refresh_all", "refresh_watch", "refresh_status"]


def test_collision_skips_batch_without_fatal(state_path: Path) -> None:
    runner = MagicMock()
    runner.is_running.return_value = True
    scheduler = AutoScheduler(job_runner=runner)
    scheduler._run_refresh_batch("2026-07-17-9")
    assert "2026-07-17-9" in scheduler._done_refresh
    runner.try_start.assert_not_called()
    runner.cancel.assert_not_called()
    assert scheduler._status.state != "fatal"


def test_no_manual_or_manual_ui_remnants() -> None:
    root = Path(__file__).resolve().parents[1]
    for rel in (
        "web/auto_remote_state.py",
        "web/app.py",
        "web/frontend/src/auto/index.ts",
        "web/frontend/index.html",
    ):
        text = (root / rel).read_text(encoding="utf-8")
        assert "manual_paused" not in text
        assert "manual-recover" not in text
        assert "manual-pause" not in text
        assert "auto-remote-pause-btn" not in text
        assert "auto-remote-resume" not in text


def test_cleanup_helper_clears_only_after_cooldown(isolated_home, monkeypatch) -> None:
    from src.repost_cleanup import auto_clear_expired_maintenance_risk
    from src.repost_history import get_checkpoint, pause_maintenance_risk

    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf", 12345),
    )
    now = int(time.time())
    pause_maintenance_risk("12345", "风控", at=now - 100)

    # 冷却内：不清除。
    assert auto_clear_expired_maintenance_risk(cooldown_seconds=1000, now_ts=now - 50) is False
    assert get_checkpoint("12345").maintenance_risk_paused is True

    # 冷却到期：本地清除，0 远程。
    assert auto_clear_expired_maintenance_risk(cooldown_seconds=50, now_ts=now) is True
    assert get_checkpoint("12345").maintenance_risk_paused is False
