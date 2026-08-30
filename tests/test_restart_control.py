from __future__ import annotations

import threading

import pytest

from src.restart_control import (
    RESTART_SUPERVISED_ENV,
    RestartControl,
    RestartPendingError,
    RestartUnavailableError,
)


class _FakeServer:
    should_exit = False


def test_restart_transition_closes_work_gate_and_requests_graceful_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = RestartControl()
    server = _FakeServer()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    control.register_server(server)

    assert control.begin_restart(lambda: "account-2") == "account-2"
    assert server.should_exit is False
    with pytest.raises(RestartPendingError):
        with control.new_work_guard():
            pass

    assert control.request_restart() is True
    assert server.should_exit is True
    assert control.finish_server(server) is True


def test_restart_transition_refuses_unsupervised_entry_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = RestartControl()
    called = False
    monkeypatch.delenv(RESTART_SUPERVISED_ENV, raising=False)

    def transition() -> None:
        nonlocal called
        called = True

    with pytest.raises(RestartUnavailableError):
        control.begin_restart(transition)

    assert called is False
    assert control.is_restart_pending() is False


def test_failed_transition_leaves_work_gate_open(monkeypatch: pytest.MonkeyPatch) -> None:
    control = RestartControl()
    server = _FakeServer()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    control.register_server(server)

    def fail_transition() -> None:
        raise OSError("write failed")

    with pytest.raises(OSError, match="write failed"):
        control.begin_restart(fail_transition)

    assert control.is_restart_pending() is False
    with control.new_work_guard():
        pass


def test_inflight_work_claim_wins_before_restart_idle_transition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = RestartControl()
    server = _FakeServer()
    monkeypatch.setenv(RESTART_SUPERVISED_ENV, "1")
    control.register_server(server)
    work_holds_gate = threading.Event()
    release_work = threading.Event()
    work_running = False
    restart_errors: list[BaseException] = []

    def claim_work() -> None:
        nonlocal work_running
        with control.new_work_guard():
            work_holds_gate.set()
            assert release_work.wait(timeout=2)
            work_running = True

    def begin_restart() -> None:
        def require_idle() -> None:
            if work_running:
                raise RuntimeError("busy")

        try:
            control.begin_restart(require_idle)
        except BaseException as exc:  # capture from the worker thread for assertion
            restart_errors.append(exc)

    work_thread = threading.Thread(target=claim_work)
    restart_thread = threading.Thread(target=begin_restart)
    work_thread.start()
    assert work_holds_gate.wait(timeout=2)
    restart_thread.start()
    release_work.set()
    work_thread.join(timeout=2)
    restart_thread.join(timeout=2)

    assert not work_thread.is_alive()
    assert not restart_thread.is_alive()
    assert len(restart_errors) == 1
    assert isinstance(restart_errors[0], RuntimeError)
    assert str(restart_errors[0]) == "busy"
    assert control.is_restart_pending() is False
