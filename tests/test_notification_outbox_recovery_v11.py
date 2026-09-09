"""Notification outbox dispatching 启动收敛（V11）：崩溃遗留 → delivery_unknown，
0 HTTP / 0 resend；sent/failed/delivery_unknown 不变。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import data_paths
from src.notification import service as notify


@pytest.fixture
def proot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}),
        encoding="utf-8",
    )
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    assert data_paths.get_runtime_profile_id() == "account-1"
    yield base
    data_paths.reset_runtime_profile_for_tests()


def _outbox_path(root: Path) -> Path:
    return root / "profiles" / "account-1" / "notify_outbox.json"


def _write_outbox(root: Path, records: list[dict]) -> None:
    _outbox_path(root).write_text(json.dumps({"version": 1, "records": records}), encoding="utf-8")


def _read_outbox(root: Path) -> list[dict]:
    return json.loads(_outbox_path(root).read_text(encoding="utf-8"))["records"]


def _record(event_id: str, channel: str, status: str, summary: str = "") -> dict:
    return {"event_id": event_id, "channel": channel, "status": status, "summary": summary, "sent_at": 1}


def test_a_crash_leftover_dispatching_converted_to_unknown(proot: Path) -> None:
    _write_outbox(proot, [_record("winning_candidate:mention:1", "serverchan", "dispatching")])

    converted = notify.recover_stale_dispatches_on_startup()

    assert converted == 1
    records = _read_outbox(proot)
    assert records[0]["status"] == "delivery_unknown"
    assert "不再自动重试" in records[0]["summary"]


def test_c_sent_unchanged_by_recovery(proot: Path) -> None:
    _write_outbox(proot, [_record("e1", "serverchan", "sent", "ok")])
    notify.recover_stale_dispatches_on_startup()
    assert _read_outbox(proot)[0]["status"] == "sent"


def test_d_failed_unchanged_by_recovery(proot: Path) -> None:
    _write_outbox(proot, [_record("e1", "dingtalk", "failed", "4xx")])
    notify.recover_stale_dispatches_on_startup()
    assert _read_outbox(proot)[0]["status"] == "failed"


def test_e_unknown_unchanged_by_recovery(proot: Path) -> None:
    _write_outbox(proot, [_record("e1", "feishu", "delivery_unknown", "timeout")])
    notify.recover_stale_dispatches_on_startup()
    assert _read_outbox(proot)[0]["status"] == "delivery_unknown"


def test_b_recovery_performs_zero_http(proot: Path) -> None:
    import unittest.mock as um

    _write_outbox(proot, [_record("e1", "serverchan", "dispatching")])
    with um.patch.object(notify, "create_notify_client") as client_mock:
        notify.recover_stale_dispatches_on_startup()
        client_mock.assert_not_called()


def test_mixed_records_only_dispatching_converted(proot: Path) -> None:
    records = [
        _record("e1", "serverchan", "sent"),
        _record("e2", "dingtalk", "failed"),
        _record("e3", "feishu", "delivery_unknown"),
        _record("e4", "wecom", "dispatching"),
    ]
    _write_outbox(proot, records)
    assert notify.recover_stale_dispatches_on_startup() == 1
    statuses = {r["event_id"]: r["status"] for r in _read_outbox(proot)}
    assert statuses == {"e1": "sent", "e2": "failed", "e3": "delivery_unknown", "e4": "delivery_unknown"}


def test_empty_outbox_recovery_is_noop(proot: Path) -> None:
    _write_outbox(proot, [])
    assert notify.recover_stale_dispatches_on_startup() == 0
    assert _read_outbox(proot) == []


def test_startup_wiring_calls_recovery_and_never_blocks(proot: Path, monkeypatch) -> None:
    import web.app as app_module

    def boom():
        raise RuntimeError("simulated")

    monkeypatch.setattr("web.app.logger", type("L", (), {"exception": lambda *a, **k: None})())
    monkeypatch.setattr("src.notification.service.recover_stale_dispatches_on_startup", boom)
    app_module._startup_notification_recovery()  # 失败不抛错 = 不阻塞启动

    source = Path(app_module.__file__).read_text(encoding="utf-8")
    assert "_startup_notification_recovery()" in source
