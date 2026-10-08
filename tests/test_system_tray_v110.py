"""V110 tray lifecycle, identity and graceful service exit (offline)."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

import binggo_launcher as launcher
from src import windows_tray
from src.restart_control import RestartControl, RestartPendingError
from src import tray_shutdown


class FakeIcon:
    icons: list["FakeIcon"] = []

    def __init__(self, name, image, title, menu):
        self.name, self.image, self.title, self.menu = name, image, title, menu
        self.visible = False
        self.stopped = threading.Event()
        self.running = threading.Event()
        self.icons.append(self)

    def run(self, setup=None):
        self.running.set()
        if setup:
            setup(self)
        self.stopped.wait(2)

    def stop(self):
        self.stopped.set()


@pytest.fixture
def fake_tray(monkeypatch, tmp_path):
    from PIL import Image

    FakeIcon.icons.clear()
    path = tmp_path / "icon.png"
    Image.new("RGB", (16, 16), "blue").save(path)
    monkeypatch.setattr(windows_tray, "icon_path", lambda: path)
    monkeypatch.setattr(windows_tray, "profile_title", lambda profile: f"Binggo - {profile}")
    monkeypatch.setattr(windows_tray.sys, "platform", "win32")
    class Item:
        def __init__(self, title, callback, **kwargs):
            self.title, self.callback, self.kwargs = title, callback, kwargs
    monkeypatch.setitem(sys.modules, "pystray", types.SimpleNamespace(Icon=FakeIcon, Menu=lambda *items: items, MenuItem=Item))
    yield
    for icon in FakeIcon.icons:
        icon.stop()


def test_tray_starts_only_after_ready_and_stops_with_service(fake_tray, monkeypatch, tmp_path):
    events = []
    class Proc:
        stdin = io.BytesIO()
        def wait(self):
            events.append("wait")
            return 0
        def poll(self):
            return None
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: Proc())
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: events.append("ready") or True)
    monkeypatch.setattr(launcher, "_open_current_ui", lambda **kw: events.append("browser"))
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0
    assert events == ["ready", "browser", "wait"]
    assert len(FakeIcon.icons) == 1 and FakeIcon.icons[0].stopped.is_set()


def test_browser_failure_keeps_runtime_and_tray(fake_tray, monkeypatch, tmp_path):
    class Proc:
        stdin = io.BytesIO()
        def wait(self):
            assert FakeIcon.icons and not FakeIcon.icons[0].stopped.is_set()
            return 0
        def poll(self):
            return None
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: Proc())
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a, **k: (_ for _ in ()).throw(OSError("browser")))
    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0


def test_browser_client_closing_does_not_send_shutdown(fake_tray):
    requests = []
    tray = windows_tray.WindowsTray("account-1", "i1", lambda: None, lambda: requests.append("shutdown"))
    assert tray.start()
    assert FakeIcon.icons[-1].visible is True
    assert requests == []
    tray.stop()


def test_tray_start_requires_visible_icon(fake_tray, monkeypatch):
    class HiddenIcon(FakeIcon):
        @property
        def visible(self):
            return False

        @visible.setter
        def visible(self, value):
            if value:
                raise OSError("Shell_NotifyIcon failed")

    monkeypatch.setattr(sys.modules["pystray"], "Icon", HiddenIcon)
    tray = windows_tray.WindowsTray("account-1", "hidden", lambda: None, lambda: None)
    assert tray.start() is False
    assert HiddenIcon.icons[-1].stopped.is_set()


def test_tray_start_rejects_silent_visibility_failure(fake_tray, monkeypatch):
    class InvisibleIcon(FakeIcon):
        @property
        def visible(self):
            return False

        @visible.setter
        def visible(self, value):
            pass

    monkeypatch.setattr(sys.modules["pystray"], "Icon", InvisibleIcon)
    tray = windows_tray.WindowsTray("account-1", "invisible", lambda: None, lambda: None)
    assert tray.start() is False
    assert InvisibleIcon.icons[-1].stopped.is_set()


@pytest.mark.parametrize("profile,port", [("account-1", 8787), ("account-2", 8788)])
def test_tray_open_uses_own_ready_registry(profile, port, monkeypatch):
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", profile)
    monkeypatch.setattr(launcher, "INSTANCE_ID", f"instance-{profile}")
    monkeypatch.setattr(launcher, "DASHBOARD_PORT", port)
    monkeypatch.setattr(launcher, "DASHBOARD_URL", f"http://127.0.0.1:{port}")
    from src import runtime_instances
    monkeypatch.setattr(runtime_instances, "get_runtime_instance", lambda name: {
        "ready": True, "instance_id": f"instance-{name}", "port": port,
    })
    urls = []
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url, **kw: urls.append(url) or True)
    launcher._open_current_ui()
    assert urls == [f"http://127.0.0.1:{port}"]


@pytest.mark.parametrize("field,value", [("ready", False), ("instance_id", "other"), ("port", 9999)])
def test_tray_rejects_stale_registry(field, value, monkeypatch):
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "account-1")
    monkeypatch.setattr(launcher, "INSTANCE_ID", "own")
    monkeypatch.setattr(launcher, "DASHBOARD_PORT", 8787)
    from src import runtime_instances
    entry = {"ready": True, "instance_id": "own", "port": 8787}
    entry[field] = value
    monkeypatch.setattr(runtime_instances, "get_runtime_instance", lambda _: entry)
    monkeypatch.setattr(launcher, "_show_error", lambda _: None)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda *a, **k: pytest.fail("stale URL opened"))
    launcher._open_current_ui()


def test_two_trays_have_independent_actions(fake_tray):
    opened, exited = [], []
    a = windows_tray.WindowsTray("account-1", "a", lambda: opened.append("A"), lambda: exited.append("A"))
    b = windows_tray.WindowsTray("account-2", "b", lambda: opened.append("B"), lambda: exited.append("B"))
    assert a.start() and b.start()
    icons = FakeIcon.icons
    assert len(icons) == 2 and icons[0].name != icons[1].name
    icons[0].menu[0].callback(None, None)
    icons[1].menu[0].callback(None, None)
    icons[0].menu[1].callback(None, None)
    assert opened == ["A", "B"] and exited == ["A"]
    assert not icons[1].stopped.is_set()
    a.stop()
    icons[1].menu[1].callback(None, None)
    assert exited == ["A", "B"]
    b.stop()


def test_tray_exit_message_is_single_and_profile_local():
    class Proc:
        def __init__(self):
            self.stdin = io.BytesIO()
        def poll(self):
            return None
    a, b = Proc(), Proc()
    requested_a, requested_b = threading.Event(), threading.Event()
    launcher._request_tray_exit(a, requested_a)
    launcher._request_tray_exit(a, requested_a)
    assert a.stdin.getvalue() == b"shutdown\n" and b.stdin.getvalue() == b""
    launcher._request_tray_exit(b, requested_b)
    assert b.stdin.getvalue() == b"shutdown\n"


def test_tray_exit_channel_failure_does_not_force_kill(monkeypatch):
    class Proc:
        stdin = None
        def poll(self):
            return None
    messages = []
    monkeypatch.setattr(launcher, "_show_error", messages.append)
    requested = threading.Event()
    launcher._request_tray_exit(Proc(), requested)
    assert not requested.is_set() and messages


def test_tray_error_does_not_crash_runtime(fake_tray, monkeypatch):
    monkeypatch.setattr(windows_tray, "icon_path", lambda: Path("nonexistent"))
    tray = windows_tray.WindowsTray("account-1", "a", lambda: None, lambda: None)
    assert tray.start() is False


def test_dev_and_frozen_icon_paths(monkeypatch, tmp_path):
    from src import app_paths
    monkeypatch.delattr(sys, "frozen", raising=False)
    assert windows_tray.icon_path().is_file()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    assert windows_tray.icon_path() == tmp_path / "packaging/assets/app-icon.png"


def test_profile_title_uses_nickname_or_id(monkeypatch):
    from src import profile_manager
    monkeypatch.setattr(profile_manager, "get_profile_metadata", lambda _: {"nickname": "小号"})
    assert "小号 [account-1]" in windows_tray.profile_title("account-1")
    monkeypatch.setattr(profile_manager, "get_profile_metadata", lambda _: {"nickname": ""})
    assert windows_tray.profile_title("account-1") == "Binggo - account-1"


def test_shutdown_gate_blocks_new_work_and_exits_without_restart(monkeypatch):
    control = RestartControl()
    server = types.SimpleNamespace(should_exit=False)
    control.register_server(server)
    monkeypatch.setattr(tray_shutdown, "restart_control", control)
    import web.auto_scheduler as scheduler_module
    import web.job_runner as job_module
    events = []
    monkeypatch.setattr(scheduler_module.auto_scheduler, "stop", lambda **kw: events.append("scheduler"))
    monkeypatch.setattr(scheduler_module.auto_scheduler, "wait_until_stopped", lambda **kw: True)
    monkeypatch.setattr(job_module.runner, "cancel", lambda: events.append("cancel"))
    monkeypatch.setattr(job_module.runner, "is_running", lambda: False)
    assert tray_shutdown.shutdown_current_service()
    assert events == ["scheduler", "cancel"] and server.should_exit
    with pytest.raises(RestartPendingError):
        with control.new_work_guard():
            pass
    assert control.finish_server(server) is False


def test_shutdown_waits_for_job_terminal(monkeypatch):
    control = RestartControl()
    server = types.SimpleNamespace(should_exit=False)
    control.register_server(server)
    monkeypatch.setattr(tray_shutdown, "restart_control", control)
    import web.auto_scheduler as scheduler_module
    import web.job_runner as job_module
    monkeypatch.setattr(scheduler_module.auto_scheduler, "stop", lambda **kw: None)
    monkeypatch.setattr(scheduler_module.auto_scheduler, "wait_until_stopped", lambda **kw: True)
    monkeypatch.setattr(job_module.runner, "cancel", lambda: None)
    running = iter([True, False])
    monkeypatch.setattr(job_module.runner, "is_running", lambda: next(running))
    assert tray_shutdown.shutdown_current_service()
    assert server.should_exit


def test_pending_shutdown_is_not_double_accepted(monkeypatch):
    control = RestartControl()
    server = types.SimpleNamespace(should_exit=False)
    control.register_server(server)
    assert control.begin_shutdown()
    assert not control.begin_shutdown()


def test_service_control_listener_only_for_supervised_child(monkeypatch):
    monkeypatch.delenv(tray_shutdown.TRAY_CONTROL_ENV, raising=False)
    monkeypatch.setattr(tray_shutdown.threading, "Thread", lambda **kw: pytest.fail("unexpected listener"))
    tray_shutdown.start_shutdown_listener()


def test_service_control_listener_handles_only_shutdown_command(monkeypatch):
    monkeypatch.setenv(tray_shutdown.TRAY_CONTROL_ENV, "1")
    monkeypatch.setattr(tray_shutdown.sys, "stdin", io.StringIO("ignored\nshutdown\n"))
    called = threading.Event()
    monkeypatch.setattr(tray_shutdown, "shutdown_current_service", lambda: called.set())
    tray_shutdown.start_shutdown_listener()
    assert called.wait(2)


def test_packaging_includes_icon_and_tray_backend():
    root = Path(__file__).resolve().parents[1]
    spec = (root / "packaging/windows/binggo.spec").read_text(encoding="utf-8")
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    assert "app-icon.png" in spec and '"pystray._win32"' in spec
    assert "pystray==0.19.5" in requirements


def test_service_failure_removes_tray(fake_tray, monkeypatch, tmp_path):
    class Proc:
        stdin = io.BytesIO()
        def wait(self):
            return 9
        def poll(self):
            return None
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: Proc())
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    monkeypatch.setattr(launcher, "_open_current_ui", lambda **kw: None)
    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 9
    assert FakeIcon.icons[-1].stopped.is_set()


def test_tray_request_does_not_swallow_profile_restart(fake_tray, monkeypatch, tmp_path):
    from src.restart_control import RESTART_EXIT_CODE

    class Proc:
        stdin = io.BytesIO()
        def __init__(self, code):
            self.code = code
        def wait(self):
            if self.code == RESTART_EXIT_CODE:
                FakeIcon.icons[-1].menu[1].callback(None, None)
            return self.code
        def poll(self):
            return None
    processes = iter([Proc(RESTART_EXIT_CODE), Proc(0)])
    spawned = []
    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: spawned.append(next(processes)) or spawned[-1])
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "_wait_for_server_stop", lambda: True)
    monkeypatch.setattr(launcher, "_open_current_ui", lambda **kw: None)
    monkeypatch.setattr(launcher, "RUNTIME_PROFILE_ID", "")
    assert launcher._supervise_server(log_path=tmp_path / "log", data_root=tmp_path) == 0
    assert len(spawned) == 2


@pytest.mark.parametrize("profile", ["account-1", "account-2"])
def test_supervisor_exit_cleans_only_own_registry_and_lifecycle_lock(monkeypatch, tmp_path, profile):
    from src import app_logging, app_paths, data_paths, runtime_instances

    events = []
    # main() intentionally freezes process globals/environment for production.
    # Restore every one of those bindings after this in-process launcher test.
    for name in ("RUNTIME_PROFILE_ID", "INSTANCE_ID", "DASHBOARD_PORT", "DASHBOARD_URL", "OPEN_BROWSER"):
        monkeypatch.setattr(launcher, name, getattr(launcher, name))
    for name in (runtime_instances.RUNTIME_PROFILE_ENV, runtime_instances.RUNTIME_PRIMARY_ENV,
                 data_paths.LEGACY_DATA_ROOT_ENV):
        monkeypatch.setenv(name, os.environ.get(name, ""))
    monkeypatch.setattr(launcher.sys, "argv", ["binggo_launcher.py", "--profile", profile, "--no-browser"])
    monkeypatch.setattr(data_paths, "ensure_data_root_selected", lambda: tmp_path)
    monkeypatch.setattr(data_paths, "initialize_data_layout", lambda: None)
    monkeypatch.setattr(launcher, "_acquire_single_instance", lambda: True)
    monkeypatch.setattr(launcher, "_release_single_instance", lambda: events.append(("release", profile)))
    monkeypatch.setattr(launcher, "_port_available", lambda *args: True)
    monkeypatch.setattr(runtime_instances, "reserve_runtime", lambda *args, **kwargs: 8787)
    monkeypatch.setattr(runtime_instances, "claim_runtime", lambda name, **kw: events.append(("claim", name)))
    monkeypatch.setattr(runtime_instances, "clear_runtime", lambda name, **kw: events.append(("clear", name)))
    monkeypatch.setattr(app_paths, "ensure_user_dirs", lambda: None)
    monkeypatch.setattr(app_paths, "user_home", lambda: tmp_path)
    monkeypatch.setattr(app_paths, "runtime_label", lambda: "test")
    monkeypatch.setattr(app_logging, "log_file", lambda: tmp_path / "test.log")
    monkeypatch.setattr(launcher, "_supervise_server", lambda **kw: 0)
    assert launcher.main() == 0
    assert events == [("claim", profile), ("clear", profile), ("release", profile)]
