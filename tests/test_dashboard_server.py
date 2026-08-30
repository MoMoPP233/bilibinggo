from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_dev_port_is_8787(monkeypatch):
    monkeypatch.setattr("src.app_paths.is_frozen", lambda: False)
    import importlib

    import src.dashboard_server as dashboard_server

    importlib.reload(dashboard_server)
    assert dashboard_server.get_dashboard_port() == 8787
    assert dashboard_server.DASHBOARD_URL == "http://127.0.0.1:8787"


def test_packaged_port_is_8181(monkeypatch):
    monkeypatch.setattr("src.app_paths.is_frozen", lambda: True)
    import importlib

    import src.dashboard_server as dashboard_server

    importlib.reload(dashboard_server)
    assert dashboard_server.get_dashboard_port() == 8181
    assert dashboard_server.DASHBOARD_URL == "http://127.0.0.1:8181"


def test_run_dashboard_server_restores_none_stdio(monkeypatch):
    import src.dashboard_server as dashboard_server
    from src.restart_control import restart_control

    monkeypatch.setattr(dashboard_server.sys, "stdout", None)
    monkeypatch.setattr(dashboard_server.sys, "stderr", None)
    captured: dict[str, bool] = {}

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            captured["stdout_ok"] = dashboard_server.sys.stdout is not None
            captured["stderr_ok"] = dashboard_server.sys.stderr is not None

    restart_control.reset_for_tests()
    monkeypatch.setattr("uvicorn.Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    assert dashboard_server.run_dashboard_server() is False
    assert captured == {"stdout_ok": True, "stderr_ok": True}
    restart_control.reset_for_tests()


def test_run_dashboard_server_reports_supervised_restart(monkeypatch):
    import src.dashboard_server as dashboard_server
    from src.restart_control import RESTART_SUPERVISED_ENV, restart_control

    class FakeServer:
        def __init__(self, _config):
            self.should_exit = False

        def run(self):
            restart_control.begin_restart(lambda: None)
            assert restart_control.request_restart() is True
            assert self.should_exit is True

    restart_control.reset_for_tests()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    monkeypatch.setattr("uvicorn.Config", lambda *_args, **_kwargs: object())
    monkeypatch.setattr("uvicorn.Server", FakeServer)
    assert dashboard_server.run_dashboard_server() is True
    restart_control.reset_for_tests()
