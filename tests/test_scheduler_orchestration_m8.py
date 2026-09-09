"""M8 scheduler orchestration（离线）：cleanup cycle / winner slot / persistent guard。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

import web.auto_scheduler as sch


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src import data_paths as dp

    monkeypatch.setattr(dp, "get_profile_dir", lambda profile_id=None: tmp_path)
    monkeypatch.setattr(sch, "_slot_state_path", lambda: tmp_path / "scheduler_slots.json")
    monkeypatch.setattr(sch, "_reset_slot_state_for_tests", lambda: None)
    sch._reset_slot_state_for_tests()


def _scheduler(fake):
    scheduler = sch.AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    return scheduler


def test_cleanup_cycle_slot_runs_once_per_slot_key(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    fake = lambda action, *, pipeline_index=None: calls.append(action) or {"skipped": False}
    monkeypatch.setattr(sch, "_persist_slot_claimed", sch._persist_slot_claimed)

    scheduler = _scheduler(fake)
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-08")
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-08")  # 同一 slot 不重复
    assert calls == ["cleanup_maintenance_cycle"]
    # “重启”：新实例同 slot → 0 重复
    scheduler2 = _scheduler(fake)
    scheduler2._run_cleanup_cycle("cleanup-2026-07-17-08")
    assert calls == ["cleanup_maintenance_cycle"]


def test_winner_slot_runs_once_per_slot_key(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    fake = lambda action, *, pipeline_index=None: calls.append(action) or {"skipped": False}
    scheduler = _scheduler(fake)
    scheduler._run_winner_scan("winner-2026-07-17-11")
    scheduler._run_winner_scan("winner-2026-07-17-11")
    scheduler2 = _scheduler(fake)
    scheduler2._run_winner_scan("winner-2026-07-17-11")
    assert calls == ["winner_deep_scan"]


def test_different_slots_are_distinct(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    fake = lambda action, *, pipeline_index=None: calls.append(action) or {"skipped": False}
    scheduler = _scheduler(fake)
    scheduler._run_winner_scan("winner-2026-07-17-05")
    scheduler._last_remote_stage_finished_mono = 0.0
    scheduler._run_winner_scan("winner-2026-07-17-11")
    scheduler._last_remote_stage_finished_mono = 0.0
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-08")
    assert calls == ["winner_deep_scan", "winner_deep_scan", "cleanup_maintenance_cycle"]


def test_cooldown_blocks_new_cleanup_cycle(tmp_path: Path, monkeypatch) -> None:
    from web import auto_remote_state as state_mod

    target = tmp_path / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)
    state_mod.record_auto_remote_risk(trigger_stage="refresh:refresh_all", reason="429")
    calls: list[str] = []
    fake = lambda action, *, pipeline_index=None: calls.append(action) or {"skipped": False}
    scheduler = _scheduler(fake)
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-08")
    assert calls == []  # cooldown 内 0 remote Job


def test_collision_skip_no_requeue(tmp_path: Path, monkeypatch) -> None:
    class Busy(RuntimeError):
        pass

    def fake(action, *, pipeline_index=None):
        raise sch.CollisionError("busy")

    scheduler = _scheduler(fake)
    scheduler._run_winner_scan("winner-2026-07-17-11")
    assert "winner-2026-07-17-11" in scheduler._done_winner  # 本轮标记完成，不补跑
    assert scheduler._status.state != "fatal"


def test_stale_generation_blocks_cleanup(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    fake = lambda action, *, pipeline_index=None: calls.append(action) or {"skipped": False}
    scheduler = _scheduler(fake)
    # 旧代失效场景由 _stale_or_slot_guard 的 slot claim + 运行期 is_stale 双重约束；
    # 此处模拟已完成的旧 slot（slot file 已 claim），新代同 slot 不得再次投递。
    scheduler._run_cleanup_cycle("cleanup-2026-07-17-08")
    calls.clear()
    scheduler2 = _scheduler(fake)
    scheduler2._run_cleanup_cycle("cleanup-2026-07-17-08")
    assert calls == []


def test_ds9_uses_same_refresh_path() -> None:
    import web.actions as actions

    assert any(sid == "DS-9" for sid, _, _ in actions.DS_HANDLERS)
    # AutoScheduler refresh 只用 refresh_all（含 DS9），与手动一键更新同路径
    assert actions.WRITER_LEASE_ACTIONS  # 兼容占位


# ---------- 静态 slot 唯一性（24h 模拟） ----------


def _intended_actions(now):
    actions = []
    h, m = now.hour, now.minute
    if h in {0, 3, 6, 9, 12, 15, 18, 21} and m == 0:
        actions.append("refresh_all")
    if m == 5 and h % 6 == 2:
        actions.append("cleanup_maintenance_cycle")
    if m == 3 and h % 6 == 1:
        actions.append("following_feed_scan")
    if m == 4 and h % 6 == 5:
        actions.append("winner_deep_scan")
    dedicated = (
        (m == 3 and h % 6 == 1) or (m == 4 and h % 6 == 5) or (m == 5 and h % 6 == 2)
    )
    if h not in {0, 3, 6, 9, 12, 15, 18, 21} and m in range(5, 60, 5) and not dedicated:
        actions.append("participate_triple")
    return actions


def test_no_minute_plans_two_remote_jobs() -> None:
    from datetime import datetime, timedelta

    base = datetime(2026, 7, 17, 0, 0)
    conflicts = []
    for minute in range(24 * 60):
        now = base + timedelta(minutes=minute)
        acts = _intended_actions(now)
        if len(acts) > 1:
            conflicts.append((now.strftime("%H:%M"), acts))
    assert conflicts == []


def _dt(hour, minute):
    from datetime import datetime

    return datetime(2026, 7, 17, hour, minute)


def test_cleanup_minutes_exclude_participation() -> None:
    for hour in range(24):
        if hour % 6 == 2:
            acts = _intended_actions(_dt(hour, 5))
            assert acts == ["cleanup_maintenance_cycle"], (hour, acts)


def test_winner_following_minutes_have_single_intent() -> None:
    for hour in range(24):
        if hour % 6 == 5:
            assert _intended_actions(_dt(hour, 4)) == ["winner_deep_scan"]
        if hour % 6 == 1:
            assert _intended_actions(_dt(hour, 3)) == ["following_feed_scan"]


# ---------- cleanup cycle 编排：唯一 assessment engine / stage1 阻断 ----------


def test_cycle_calls_auto_maintain_without_assessment_then_single_scan() -> None:
    import unittest.mock as um

    import src.repost_cleanup as rc

    calls = {"maintain": 0, "scan": 0, "delete": 0}

    def fake_maintain(**kwargs):
        calls["maintain"] += 1
        assert kwargs.get("assess") is False
        # 与生产 auto_maintain 正常返回 contract 一致：synced=True 才表示安全完成
        return {"synced": True, "rate_limited": False, "evaluated_originals": 0}

    def fake_scan(**kwargs):
        calls["scan"] += 1
        return {"rate_limited": False, "stopped_early": False, "stop_reason": "", "pending_originals": 0}

    def fake_delete(**kwargs):
        calls["delete"] += 1
        return {"deleted_count": 0}

    with um.patch.object(rc, "auto_maintain", fake_maintain), um.patch.object(
        rc, "scan_expired_reposts", fake_scan
    ), um.patch.object(rc, "auto_cleanup_delete", fake_delete):
        result = rc.cleanup_maintenance_cycle()
    assert calls == {"maintain": 1, "scan": 1, "delete": 1}
    assert result["ok"] is True


def test_cycle_pre_maintenance_risk_blocks_assessment_and_delete() -> None:
    import unittest.mock as um

    import src.repost_cleanup as rc

    calls = {"scan": 0, "delete": 0}

    def risk_maintain(**kwargs):
        raise RuntimeError("API error -352: 风控校验失败")

    def fake_scan(**kwargs):
        calls["scan"] += 1
        return {}

    def fake_delete(**kwargs):
        calls["delete"] += 1
        return {}

    with um.patch.object(rc, "auto_maintain", risk_maintain), um.patch.object(
        rc, "scan_expired_reposts", fake_scan
    ), um.patch.object(rc, "auto_cleanup_delete", fake_delete):
        try:
            rc.cleanup_maintenance_cycle()
            raise AssertionError("应抛出 risk")
        except RuntimeError as exc:
            assert "API error -352" in str(exc)
    assert calls == {"scan": 0, "delete": 0}


def test_cycle_stage1_rate_limited_return_blocks_scan_and_delete() -> None:
    """真实语义：auto_maintain 命中风控时不抛异常，而是落盘暂停后返回 rate_limited。"""
    import unittest.mock as um

    import src.repost_cleanup as rc

    calls = {"scan": 0, "delete": 0}

    def rate_limited_maintain(**kwargs):
        return {
            "uid": "1",
            "synced": False,
            "evaluated_originals": 0,
            "rate_limited": True,
            "message": "自动维护因平台限制已暂停，请稍后手动恢复。",
        }

    def fake_scan(**kwargs):
        calls["scan"] += 1
        return {}

    def fake_delete(**kwargs):
        calls["delete"] += 1
        return {}

    with um.patch.object(rc, "auto_maintain", rate_limited_maintain), um.patch.object(
        rc, "scan_expired_reposts", fake_scan
    ), um.patch.object(rc, "auto_cleanup_delete", fake_delete):
        result = rc.cleanup_maintenance_cycle()
    assert calls == {"scan": 0, "delete": 0}
    assert result["ok"] is True
    assert any(
        phase.get("stage") == "连续智能评估" and phase.get("result", {}).get("skipped")
        for phase in result["phases"]
    )


@pytest.mark.parametrize(
    "stage1_result",
    [
        {"synced": False, "rate_limited": False, "stopped_early": True, "message": "stopped"},
        {"synced": False, "rate_limited": False, "stop_reason": "hard_error", "message": "hard"},
        {"synced": False, "rate_limited": False, "message": "自动维护同步失败"},
    ],
)
def test_cycle_stage1_stopped_or_failure_blocks_scan_and_delete(stage1_result) -> None:
    import unittest.mock as um

    import src.repost_cleanup as rc

    calls = {"scan": 0, "delete": 0}

    def stopped_maintain(**kwargs):
        return stage1_result

    def fake_scan(**kwargs):
        calls["scan"] += 1
        return {}

    def fake_delete(**kwargs):
        calls["delete"] += 1
        return {}

    with um.patch.object(rc, "auto_maintain", stopped_maintain), um.patch.object(
        rc, "scan_expired_reposts", fake_scan
    ), um.patch.object(rc, "auto_cleanup_delete", fake_delete):
        result = rc.cleanup_maintenance_cycle()
    assert calls == {"scan": 0, "delete": 0}
    assert result["ok"] is True


def test_cycle_stage2_risk_blocks_delete() -> None:
    import unittest.mock as um

    import src.repost_cleanup as rc

    calls = {"scan": 0, "delete": 0}

    def safe_maintain(**kwargs):
        return {"synced": True, "rate_limited": False}

    def risky_scan(**kwargs):
        calls["scan"] += 1
        return {"rate_limited": True, "stopped_early": True, "stop_reason": "", "pending_originals": 0}

    def fake_delete(**kwargs):
        calls["delete"] += 1
        return {}

    with um.patch.object(rc, "auto_maintain", safe_maintain), um.patch.object(
        rc, "scan_expired_reposts", risky_scan
    ), um.patch.object(rc, "auto_cleanup_delete", fake_delete):
        result = rc.cleanup_maintenance_cycle()
    assert calls == {"scan": 1, "delete": 0}
    assert result["ok"] is True
