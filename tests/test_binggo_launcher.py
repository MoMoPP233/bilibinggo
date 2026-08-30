from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

import binggo_launcher as launcher
from src.restart_control import RESTART_EXIT_CODE, RESTART_SUPERVISED_ENV


class _FakeProcess:
    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        self.poll_count = 0

    def poll(self) -> int | None:
        self.poll_count += 1
        if self.poll_count == 1:
            return None
        return self.exit_code


def test_supervisor_restarts_only_for_restart_exit_code_and_opens_browser_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    processes = iter([_FakeProcess(RESTART_EXIT_CODE), _FakeProcess(0)])
    spawned: list[_FakeProcess] = []
    browser_urls: list[str] = []
    stop_waits: list[bool] = []

    def fake_spawn() -> _FakeProcess:
        proc = next(processes)
        spawned.append(proc)
        return proc

    monkeypatch.setattr(launcher, "_spawn_server_process", fake_spawn)
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(
        launcher,
        "_wait_for_server_stop",
        lambda: stop_waits.append(True) or True,
    )
    monkeypatch.setattr(launcher.webbrowser, "open", browser_urls.append)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    result = launcher._supervise_server(log_path=tmp_path / "app.log", data_root=tmp_path)

    assert result == 0
    assert len(spawned) == 2
    assert stop_waits == [True]
    assert browser_urls == [launcher.DASHBOARD_URL]


@pytest.mark.parametrize("exit_code", [0, 1, 23])
def test_supervisor_does_not_restart_for_normal_or_failure_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exit_code: int,
) -> None:
    spawned = [_FakeProcess(exit_code)]
    stop_wait_called = False

    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: spawned[0])
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)

    def unexpected_stop_wait() -> bool:
        nonlocal stop_wait_called
        stop_wait_called = True
        return True

    monkeypatch.setattr(launcher, "_wait_for_server_stop", unexpected_stop_wait)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)

    assert (
        launcher._supervise_server(log_path=tmp_path / "app.log", data_root=tmp_path)
        == exit_code
    )
    assert spawned[0].poll_count >= 2
    assert stop_wait_called is False


def test_supervisor_refuses_second_spawn_when_old_port_does_not_close(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spawned = [_FakeProcess(RESTART_EXIT_CODE)]
    messages: list[str] = []

    monkeypatch.setattr(launcher, "_spawn_server_process", lambda: spawned[0])
    monkeypatch.setattr(launcher, "_wait_for_server", lambda: True)
    monkeypatch.setattr(launcher, "_wait_for_server_stop", lambda: False)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda _url: None)
    monkeypatch.setattr(launcher.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(launcher, "_show_error", messages.append)

    assert launcher._supervise_server(log_path=tmp_path / "app.log", data_root=tmp_path) == 1
    assert len(spawned) == 1
    assert messages and "未及时释放" in messages[0]


def test_source_and_frozen_child_commands_are_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    python_executable = r"C:\Python\python.exe"
    monkeypatch.setattr(launcher.sys, "executable", python_executable)
    monkeypatch.delattr(launcher.sys, "frozen", raising=False)

    source_args = launcher._server_process_args()
    assert source_args == [
        python_executable,
        str(launcher.ROOT / "scripts" / "run_dashboard.py"),
        launcher.SERVE_FLAG,
    ]

    monkeypatch.setattr(launcher.sys, "frozen", True, raising=False)
    assert launcher._server_process_args() == [python_executable, launcher.SERVE_FLAG]


def test_spawn_marks_child_as_supervised(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import src.app_paths as app_paths

    captured: dict[str, object] = {}
    parent_value = "parent-value"
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, parent_value)
    monkeypatch.setattr(app_paths, "bundle_root", lambda: tmp_path)
    monkeypatch.setattr(launcher, "_server_process_args", lambda: ["python", "serve.py"])

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _FakeProcess(0)

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)

    launcher._spawn_server_process()

    assert captured["args"] == ["python", "serve.py"]
    child_env = captured["kwargs"]["env"]
    assert child_env[RESTART_SUPERVISED_ENV] == "1"
    assert "creationflags" not in captured["kwargs"]
    assert os.environ[RESTART_SUPERVISED_ENV] == parent_value


def test_source_command_delegates_to_launcher_supervisor(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_dashboard as source_entry

    monkeypatch.setattr(source_entry.sys, "argv", ["scripts/run_dashboard.py"])
    monkeypatch.setattr(launcher, "main", lambda: 17)

    assert source_entry.main() == 17


@pytest.mark.parametrize(
    ("restart_requested", "expected_code"),
    [(False, 0), (True, RESTART_EXIT_CODE)],
)
def test_source_service_child_propagates_restart_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    restart_requested: bool,
    expected_code: int,
) -> None:
    import scripts.run_dashboard as source_entry
    import src.app_logging as app_logging
    import src.app_paths as app_paths
    import src.config_health as config_health
    import src.dashboard_server as dashboard_server
    import src.data_paths as data_paths

    monkeypatch.setattr(
        source_entry.sys,
        "argv",
        ["scripts/run_dashboard.py", launcher.SERVE_FLAG],
    )
    monkeypatch.setattr(data_paths, "ensure_data_root_selected", lambda: Path("data"))
    monkeypatch.setattr(app_paths, "ensure_user_dirs", lambda: None)
    monkeypatch.setattr(app_logging, "setup_logging", lambda: Path("app.log"))
    monkeypatch.setattr(config_health, "log_config_health", lambda **_kwargs: None)
    monkeypatch.setattr(
        dashboard_server,
        "run_dashboard_server",
        lambda: restart_requested,
    )
    monkeypatch.setattr(source_entry.asyncio, "set_event_loop_policy", lambda _policy: None)

    assert source_entry.main() == expected_code
