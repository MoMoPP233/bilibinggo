"""auto_remote_state 损坏时 AutoScheduler 必须 Fail-Closed（V17）。

全 mock / tmp_path / fake，禁止真实 Bilibili：
A  missing：从未产生过状态 → unpaused，且不会创建文件
B  valid active cooldown → paused
C  valid expired cooldown → 恢复 unpaused
D  非法 JSON（截断）→ Fail-Closed
E  空文件 → Fail-Closed
F  根类型不是 object（[]/"abc"/123）→ Fail-Closed
G  关键 cooldown 字段格式非法 → Fail-Closed（旧合法文件仍兼容）
H  PermissionError/OSError 读取 → Fail-Closed
I  真实 AutoScheduler 自动远程入口被阻断（不启动 Job）
J  本地读取/status 不崩溃（不 500）
K  损坏文件读取后不被自动删除/清空/rewrite
L  Profile 隔离：A 损坏只挡 A
M  record 仍能写合法记录；replace 失败时旧文件保持
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from web import auto_remote_state as state_mod
from web.auto_config import AUTO_REMOTE_RISK_COOLDOWN_SECONDS
from web.auto_scheduler import AutoScheduler


@pytest.fixture(autouse=True)
def _isolate_profile_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把 runtime Profile 目录隔离到临时目录（scheduler slot / risk 文件共用）。"""
    from src import data_paths as _data_paths

    monkeypatch.setattr(_data_paths, "get_profile_dir", lambda profile_id=None: tmp_path)


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a" / "auto_remote_state.json"
    monkeypatch.setattr(state_mod, "_state_path", lambda: target)
    return target


def _scheduler_with_fake_click(fake):
    scheduler = AutoScheduler(job_runner=MagicMock())
    scheduler._click_and_wait = fake
    return scheduler


def _write_raw(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _valid_risk_state(now_ts: int = 1_000_000) -> dict:
    return {
        "paused": True,
        "paused_at": now_ts,
        "paused_until": now_ts + AUTO_REMOTE_RISK_COOLDOWN_SECONDS,
        "trigger_stage": "job:participate_triple",
        "code": "-352",
        "reason": "风控",
    }


# ---------- A：missing ----------


def test_missing_file_is_unpaused_and_creates_nothing(state_path: Path, monkeypatch) -> None:
    assert state_path.exists() is False
    assert state_mod.is_risk_paused() is False
    view = state_mod.risk_pause_state()
    assert view["paused"] is False
    assert view["state_status"] == "missing"
    scheduler = _scheduler_with_fake_click(lambda *a, **k: None)
    assert scheduler._remote_stage_allowed() is True
    assert state_path.exists() is False  # 读取/门禁绝不创建损坏占位


# ---------- B：valid active ----------


def test_valid_active_cooldown_is_paused(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-352")
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert view["expired"] is False
    assert view["state_status"] == "ok"
    assert state_mod.is_risk_paused() is True
    assert _scheduler_with_fake_click(lambda *a, **k: None)._remote_stage_allowed() is False


# ---------- C：valid expired ----------


def test_valid_expired_recovers_eligibility(state_path: Path) -> None:
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-352", now_ts=1_000_000)
    raw = _valid_risk_state(now_ts=1_000_000)
    raw["paused_until"] = int(time.time()) - 1
    state_mod._write_state(raw)
    view = state_mod.risk_pause_state()
    assert view["paused"] is False
    assert view["expired"] is True
    assert view["state_status"] == "ok"
    scheduler = _scheduler_with_fake_click(lambda *a, **k: None)
    assert scheduler._remote_stage_allowed() is True
    assert state_mod.is_risk_paused() is False  # 过期已本地清除


# ---------- D/E/F/G：存在但损坏 → Fail-Closed ----------


@pytest.mark.parametrize("content", [
    "{",
    '{"paused": true, "paused_until": 1',  # 截断
    "",
    "   ",
    "[]",
    '"abc"',
    "123",
    "null",
])
def test_corrupt_content_is_fail_closed(state_path: Path, content: str) -> None:
    _write_raw(state_path, content)
    view = state_mod.risk_pause_state()
    assert view["paused"] is True  # Fail-Closed
    assert view["expired"] is False
    assert view["state_status"] == "invalid"
    assert state_mod.is_risk_paused() is True


@pytest.mark.parametrize("payload", [
    {"paused": True, "paused_until": "not-a-number"},
    {"paused": True, "paused_until": True},
    {"paused": True, "paused_at": "x"},
    {"paused": "yes"},
    {"paused": False, "paused_until": 123, "paused_at": []},
])
def test_invalid_critical_field_is_fail_closed(state_path: Path, payload: dict) -> None:
    _write_raw(state_path, json.dumps(payload))
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert view["state_status"] == "invalid"
    assert state_mod.is_risk_paused() is True


def test_legacy_and_extra_fields_remain_readable(state_path: Path) -> None:
    """未知额外字段、可选字段缺失、0/1 形态 paused：旧语义保持，不因过严 Schema 误伤。"""
    payload = {
        "paused": 1,
        "paused_until": int(time.time()) + 3600,
        "extra_unknown_field": {"anything": True},
    }
    _write_raw(state_path, json.dumps(payload))
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert view["state_status"] == "ok"

    _write_raw(state_path, json.dumps({"paused": False}))
    assert state_mod.risk_pause_state()["paused"] is False
    assert state_mod.risk_pause_state()["state_status"] == "ok"


# ---------- H：unreadable ----------


class _UnreadablePath:
    def __init__(self, exc: type[OSError]) -> None:
        self._exc = exc

    def read_text(self, *args, **kwargs):
        raise self._exc("read denied")

    def __str__(self) -> str:
        return "auto_remote_state.json"


@pytest.mark.parametrize("error_type", [PermissionError, OSError])
def test_unreadable_file_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch, error_type: type[OSError],
) -> None:
    monkeypatch.setattr(state_mod, "_state_path", lambda: _UnreadablePath(error_type))
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert view["state_status"] == "unreadable"
    assert state_mod.is_risk_paused() is True
    assert _scheduler_with_fake_click(lambda *a, **k: None)._remote_stage_allowed() is False


# ---------- I：真实 Scheduler 自动远程入口 ----------


def test_corrupt_state_blocks_all_auto_remote_stages(state_path: Path) -> None:
    _write_raw(state_path, "{")  # 损坏
    calls: list[str] = []

    def fake(action, *, pipeline_index=None):
        calls.append(action)
        return {"skipped": False}

    scheduler = _scheduler_with_fake_click(fake)
    # 自动 refresh / participate / following 入口共用远程门禁。
    scheduler._run_refresh_batch("2026-07-17-6")
    scheduler._run_triple_slot("2026-07-17-06-05")
    scheduler._run_following_scan("following-2026-07-17-06")
    # V24：fail-closed 只拦远程动作；本地 refresh_status 仍可运行。
    assert calls == ["refresh_status"]
    assert scheduler._status.state != "fatal"


# ---------- J：本地读取/status 不崩溃 ----------


def test_local_reads_do_not_crash_when_state_corrupt(state_path: Path) -> None:
    _write_raw(state_path, "not-json{")
    view = state_mod.risk_pause_state()
    assert view["paused"] is True
    assert state_mod.is_risk_paused() is True
    assert state_mod.clear_expired_risk_pause() is False  # 不抛、不误清
    scheduler = _scheduler_with_fake_click(lambda *a, **k: None)
    status = scheduler.get_status()
    assert status["state"] in {"idle", "stopped", "running"}
    assert state_path.exists() is True


# ---------- K：不自动 heal ----------


def test_corrupt_state_is_never_auto_healed(state_path: Path) -> None:
    corrupt = '{"paused": true, "paused_until": "oops'  # 损坏原文
    _write_raw(state_path, corrupt)
    _ = state_mod.is_risk_paused()
    _ = state_mod.risk_pause_state()
    _ = state_mod.clear_expired_risk_pause()
    _ = _scheduler_with_fake_click(lambda *a, **k: None)._remote_stage_allowed()
    assert state_path.read_text(encoding="utf-8") == corrupt  # 未删除/清空/重写


# ---------- L：Profile 隔离 ----------


def test_corrupt_profile_a_does_not_block_profile_b(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "account-a" / "auto_remote_state.json"
    b = tmp_path / "account-b" / "auto_remote_state.json"
    _write_raw(a, "broken{")

    monkeypatch.setattr(state_mod, "_state_path", lambda: a)
    assert state_mod.risk_pause_state()["paused"] is True
    assert state_mod.risk_pause_state()["state_status"] == "invalid"

    # B：从未产生过状态 → 正常 unpaused
    monkeypatch.setattr(state_mod, "_state_path", lambda: b)
    assert state_mod.is_risk_paused() is False
    assert state_mod.risk_pause_state()["state_status"] == "missing"

    # B 自己写入合法 cooldown 后仍只影响 B
    state_mod.record_auto_remote_risk(trigger_stage="participate_triple", reason="-352")
    assert state_mod.is_risk_paused() is True

    monkeypatch.setattr(state_mod, "_state_path", lambda: a)
    assert state_mod.risk_pause_state()["state_status"] == "invalid"  # A 仍损坏并被挡
    assert state_mod.is_risk_paused() is True


# ---------- M：合法写入保持 + replace 失败旧文件不丢 ----------


def test_record_still_writes_valid_state_after_corruption(state_path: Path) -> None:
    _write_raw(state_path, "{corrupt")
    state = state_mod.record_auto_remote_risk(
        trigger_stage="participate_triple", reason="-352"
    )
    assert state["paused"] is True
    assert state["paused_until"] == int(time.time()) + AUTO_REMOTE_RISK_COOLDOWN_SECONDS
    assert state["state_status"] == "ok"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    assert raw["paused"] is True and raw["paused_until"] == state["paused_until"]


def test_write_replace_failure_keeps_old_file(state_path: Path, monkeypatch) -> None:
    _write_raw(state_path, json.dumps(_valid_risk_state(now_ts=1_000_000)))
    original = state_path.read_text(encoding="utf-8")

    def broken_replace(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", broken_replace)
    with pytest.raises(OSError, match="disk full"):
        state_mod.record_auto_remote_risk(
            trigger_stage="participate_triple", reason="-509", now_ts=1_000_200
        )
    # 旧目标文件保持原内容；无残留临时文件
    assert state_path.read_text(encoding="utf-8") == original
    leftovers = [
        name for name in os.listdir(state_path.parent)
        if name.startswith(".auto_remote_state.json.")
    ]
    assert leftovers == []
