"""V113 tray status is a bounded, local, read-only view (no account requests)."""

from __future__ import annotations

import json
import subprocess
import types
from datetime import datetime, timedelta

import pytest

import binggo_launcher as launcher
from src import windows_tray
from web import auto_scheduler as scheduler_mod
from web import tray_health
from web.auto_scheduler import AutoScheduler, CN_TZ
from web.job_runner import JobStatus


def _snapshot(**changes):
    value = {
        "state": "running", "thread_alive": True,
        "last_tick_at": datetime.now(CN_TZ).isoformat(),
        "last_successful_scan_at": None,
        "next_scan_unix": int((datetime.now(CN_TZ) + timedelta(minutes=20)).timestamp()),
        "scan_running": False,
    }
    value.update(changes)
    return value


@pytest.fixture
def local_health(monkeypatch):
    state = _snapshot()
    risk = {"state_status": "missing", "paused": False}
    job = JobStatus()
    monkeypatch.setattr(tray_health.auto_scheduler, "tray_snapshot", lambda: state)
    monkeypatch.setattr(tray_health.runner, "get_status", lambda: job)
    monkeypatch.setattr(tray_health, "risk_pause_state", lambda: risk)
    monkeypatch.setattr(tray_health, "get_night_continue_enabled", lambda: False)
    monkeypatch.setattr(tray_health, "is_night_quiet", lambda *_args, **_kw: False)
    return state, risk, job


def test_health_distinguishes_scheduler_scan_quiet_and_formal_risk(local_health, monkeypatch):
    state, risk, job = local_health
    assert tray_health.current_tray_health()["status"] == "normal"
    job.state, job.action = "running", "refresh_all"
    assert tray_health.current_tray_health()["status"] == "scanning"
    job.action = "participate"
    assert tray_health.current_tray_health()["status"] == "normal"
    state["scan_running"] = True
    assert tray_health.current_tray_health()["status"] == "scanning"
    state["scan_running"] = False
    monkeypatch.setattr(tray_health, "is_night_quiet", lambda *_args, **_kw: True)
    assert tray_health.current_tray_health()["status"] == "night_quiet"
    risk.update(state_status="ok", paused=True)
    view = tray_health.current_tray_health()
    assert view["status"] == "risk_paused" and view["risk"] == "paused"
    assert view["next_scan_unix"] is None
    risk.update(state_status="unreadable", paused=True)
    assert tray_health.current_tray_health()["status"] == "unavailable"
    state["state"] = "stopped"
    assert tray_health.current_tray_health()["status"] == "stopped"
    state["state"] = "running"
    state["thread_alive"] = False
    assert tray_health.current_tray_health()["status"] == "unavailable"


def test_last_scan_only_after_success_and_snapshot_is_read_only(monkeypatch):
    sched = AutoScheduler()
    monkeypatch.setattr(sched, "_schedule_auto_snapshot", lambda **_kw: None)
    monkeypatch.setattr(sched, "_is_stale", lambda: False)
    sched._set_task_status("refresh", "error")
    assert sched.tray_snapshot()["last_successful_scan_at"] is None
    sched._set_task_status("participate", "completed")
    assert sched.tray_snapshot()["last_successful_scan_at"] is None
    sched._set_task_status("winner", "completed")
    first = sched.tray_snapshot()["last_successful_scan_at"]
    assert first is not None
    sched._set_task_status("following", "skipped_busy")
    assert sched.tray_snapshot()["last_successful_scan_at"] == first


def test_next_scan_uses_existing_shifted_due_not_participation():
    now = datetime(2026, 10, 8, 12, 0, tzinfo=CN_TZ)
    candidates = [
        ("participate", "自动参与", now + timedelta(minutes=2)),
        ("refresh", "刷新批次", now + timedelta(minutes=15)),
        ("winner", "中奖深探", now + timedelta(minutes=30)),
    ]
    assert scheduler_mod._next_scannable_due(candidates, night_continue=False) == now + timedelta(minutes=15)
    assert scheduler_mod._next_scannable_due(candidates[:1], night_continue=False) is None
    quiet = [("refresh", "刷新批次", now.replace(hour=0) + timedelta(minutes=15))]
    assert scheduler_mod._next_scannable_due(quiet, night_continue=False) is None


def test_tooltip_profile_identity_states_and_utf16(monkeypatch):
    names = {"account-1": "摸摸Pp", "account-2": "H牵手的痛"}
    import src.profile_manager as profiles
    monkeypatch.setattr(profiles, "get_profile_metadata", lambda profile: {"nickname": names[profile]})
    view = {"status": "normal", "risk": "unpaused", "next_scan_unix": int(datetime(2026, 10, 8, 12, 15, tzinfo=CN_TZ).timestamp()), "last_scan_at": "2026-10-08T11:00:00+08:00"}
    a = windows_tray.health_tooltip("account-1", view)
    b = windows_tray.health_tooltip("account-2", view)
    assert "摸摸Pp[account-1]" in a and "H牵手的痛[account-2]" in b
    assert "12:15" in a and "11:00" in a and "风控:未暂停" in a
    assert "上次扫描" not in windows_tray.health_tooltip("account-1", {**view, "last_scan_at": None})
    names["account-1"] = "😀" * 60 + "\n特殊·字符"
    long = windows_tray.health_tooltip("account-1", view)
    assert len(long.encode("utf-16-le")) // 2 <= 127 and "\n" not in long
    assert "风控:" in long
    assert "状态暂不可用" in windows_tray.health_tooltip("account-2", None)


def test_local_endpoint_and_supervisor_reject_identity_mismatch(monkeypatch):
    from web import app as app_mod
    from src import dashboard_server
    from src.runtime_instances import RUNTIME_INSTANCE_ENV

    monkeypatch.setattr(app_mod, "_runtime_binding_or_none", lambda: "account-1")
    monkeypatch.setenv(RUNTIME_INSTANCE_ENV, "instance-1")
    monkeypatch.setattr(dashboard_server, "get_dashboard_port", lambda: 8788)
    monkeypatch.setattr(tray_health, "current_tray_health", lambda: {"status": "normal"})
    result = app_mod.api_runtime_tray_health()
    assert result == {"ready": True, "profile_id": "account-1", "instance_id": "instance-1", "port": 8788, "status": "normal"}

    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self, *_args):
            return json.dumps(result).encode()
    class Opener:
        def open(self, request, timeout):
            assert request.full_url == "http://127.0.0.1:8788/api/runtime/tray-health"
            assert timeout == 0.5
            return Response()
    monkeypatch.setattr(launcher.urllib.request, "build_opener", lambda *_args: Opener())
    monkeypatch.setattr(launcher, "DASHBOARD_PORT", 8788)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "account-1")
    monkeypatch.setattr(launcher, "INSTANCE_ID", "instance-1")
    assert launcher._read_tray_health()["status"] == "normal"
    result["instance_id"] = "different"
    with pytest.raises(ValueError, match="身份不匹配"):
        launcher._read_tray_health()


def test_tooltip_failure_keeps_tray_healthy_and_does_not_stop_service(monkeypatch):
    class Icon:
        visible = True
        @property
        def title(self):
            return "old"
        @title.setter
        def title(self, _value):
            raise OSError("backend failed")
    tray = windows_tray.WindowsTray("account-1", "i1", lambda: None, lambda: None)
    tray._icon = Icon()
    tray._thread = types.SimpleNamespace(is_alive=lambda: True)
    tray._ready.set()
    assert tray.update_title("new") is False
    assert tray.is_healthy()
    monkeypatch.setattr(launcher, "_read_tray_health", lambda: (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setattr(windows_tray, "health_tooltip", lambda *_args: "状态暂不可用")
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "account-1")
    launcher._refresh_tray_tooltip(tray)
    assert tray.is_healthy()


def test_supervisor_tooltip_refresh_is_bounded_to_sixty_seconds(monkeypatch, tmp_path):
    class Service:
        def __init__(self):
            self.calls = 0
        def wait(self, timeout=None):
            self.calls += 1
            if self.calls <= 4:
                raise subprocess.TimeoutExpired("fake", timeout)
            return 0
        def poll(self):
            return None
    class Tray:
        def __init__(self):
            self.stopped = False
        def is_healthy(self):
            return True
        def stop(self):
            self.stopped = True
            return True
    service, tray = Service(), Tray()
    updates = []
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: service)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "_start_supervised_tray", lambda *_args: tray)
    monkeypatch.setattr(launcher, "_refresh_tray_tooltip", lambda _tray: updates.append(1))
    monkeypatch.setattr(launcher, "_open_current_ui", lambda **_kw: None)
    monkeypatch.setattr(launcher, "OPEN_BROWSER", False)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    times = iter((1.0, 30.0, 59.0, 61.0))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(times))
    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0
    assert len(updates) == 2 and tray.stopped
