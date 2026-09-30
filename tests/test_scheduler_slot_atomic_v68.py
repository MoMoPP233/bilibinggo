"""V68：scheduler slot claim 的真实跨进程原子性（仅本地状态，无网络）。"""

from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path

import web.auto_scheduler as scheduler_mod


def _claim_worker(
    root: str,
    task: str,
    slot: str,
    ready,
    release,
    results,
) -> None:
    """spawn 子进程入口：统一 release 后执行一次 slot claim。"""
    scheduler_mod._slot_state_path = lambda: Path(root) / "scheduler_slots.json"
    ready.put(os.getpid())
    release.wait(30)
    try:
        result = scheduler_mod._persist_slot_claimed(task, slot)
        results.put((result, ""))
    except BaseException as exc:  # pragma: no cover - 仅用于把子进程错误带回父进程
        results.put((None, repr(exc)))


def _claim_after_persist_worker(root: str, task: str, slot: str, results) -> None:
    scheduler_mod._slot_state_path = lambda: Path(root) / "scheduler_slots.json"
    try:
        results.put((scheduler_mod._persist_slot_claimed(task, slot), ""))
    except BaseException as exc:  # pragma: no cover - 仅用于把子进程错误带回父进程
        results.put((None, repr(exc)))


def _crash_while_holding_lock_worker(root: str, ready) -> None:
    scheduler_mod._slot_state_path = lambda: Path(root) / "scheduler_slots.json"
    path = scheduler_mod._slot_state_path()
    lock = scheduler_mod._slot_state_process_lock(path)
    lock.__enter__()
    ready.send(os.getpid())
    os._exit(17)


def _run_competing_claims(
    root_a: Path,
    root_b: Path,
    *,
    task: str,
    slot: str,
) -> list[bool | None]:
    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    results = context.Queue()
    processes = [
        context.Process(
            target=_claim_worker,
            args=(str(root_a), task, slot, ready, release, results),
        ),
        context.Process(
            target=_claim_worker,
            args=(str(root_b), task, slot, ready, release, results),
        ),
    ]
    for process in processes:
        process.start()
    try:
        assert ready.get(timeout=30)
        assert ready.get(timeout=30)
        release.set()
        values: list[bool | None] = []
        errors: list[str] = []
        for _ in processes:
            value, error = results.get(timeout=30)
            values.append(value)
            if error:
                errors.append(error)
        assert not errors
        return values
    finally:
        release.set()
        for process in processes:
            process.join(timeout=30)
            assert process.exitcode == 0


def test_same_profile_same_task_same_slot_has_exactly_one_winner(tmp_path: Path) -> None:
    root = tmp_path / "profile"
    values = _run_competing_claims(root, root, task="refresh", slot="2026-09-30-06")

    assert values.count(True) == 1
    assert values.count(False) == 1
    state = json.loads((root / "scheduler_slots.json").read_text(encoding="utf-8"))
    assert state == {"refresh": "2026-09-30-06"}


def test_different_profiles_can_claim_same_task_and_slot(tmp_path: Path) -> None:
    root_a = tmp_path / "profile-a"
    root_b = tmp_path / "profile-b"
    values = _run_competing_claims(root_a, root_b, task="refresh", slot="2026-09-30-06")

    assert values == [True, True]
    for root in (root_a, root_b):
        assert json.loads((root / "scheduler_slots.json").read_text(encoding="utf-8")) == {
            "refresh": "2026-09-30-06"
        }


def test_same_profile_different_slots_and_tasks_remain_independent(
    tmp_path: Path, monkeypatch
) -> None:
    root = tmp_path / "profile"
    values = _run_competing_claims(root, root, task="refresh", slot="2026-09-30-06")
    assert values.count(True) == 1
    assert values.count(False) == 1

    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: root / "scheduler_slots.json")
    assert scheduler_mod._persist_slot_claimed("refresh", "2026-09-30-12") is True
    assert scheduler_mod._persist_slot_claimed("following", "2026-09-30-06") is True
    state = json.loads((root / "scheduler_slots.json").read_text(encoding="utf-8"))
    assert state["refresh"] == "2026-09-30-12"
    assert state["following"] == "2026-09-30-06"


def test_process_exit_releases_slot_lock(tmp_path: Path, monkeypatch) -> None:
    context = multiprocessing.get_context("spawn")
    parent_ready, child_ready = context.Pipe(duplex=False)
    process = context.Process(
        target=_crash_while_holding_lock_worker,
        args=(str(tmp_path / "profile"), child_ready),
    )
    process.start()
    assert parent_ready.poll(30)
    assert parent_ready.recv()
    process.join(timeout=30)
    assert process.exitcode == 17

    monkeypatch.setattr(
        scheduler_mod,
        "_slot_state_path",
        lambda: tmp_path / "profile" / "scheduler_slots.json",
    )
    assert scheduler_mod._persist_slot_claimed("refresh", "2026-09-30-06") is True


def test_persisted_claim_is_not_reclaimed_after_restart(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "profile"
    monkeypatch.setattr(scheduler_mod, "_slot_state_path", lambda: root / "scheduler_slots.json")
    assert scheduler_mod._persist_slot_claimed("winner", "2026-09-30-23") is True

    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    process = context.Process(
        target=_claim_after_persist_worker,
        args=(str(root), "winner", "2026-09-30-23", results),
    )
    process.start()
    value, error = results.get(timeout=30)
    process.join(timeout=30)
    assert process.exitcode == 0
    assert not error
    assert value is False

