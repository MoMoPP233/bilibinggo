from __future__ import annotations

from src.db.engine import reset_engine_for_tests
from src.participation_guard import confirm_repost, get_guard, mark_repost_suspected, mark_repost_unknown, record_pending
from src.repost_cleanup import auto_maintain, reconciliation_needs_sync, sync_repost_history
from src.repost_history import (
    clear_sync_needed_if_unchanged,
    get_checkpoint,
    mark_sync_needed,
    save_checkpoint,
)

UID = "12345"
DYNAMIC_ID = "1000000000000000001"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )


def test_confirmed_with_sync_needed_success_keeps_normal_path(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    confirm_repost(UID, DYNAMIC_ID)
    assert get_checkpoint(UID).sync_needed is True
    synced = {"called": False}

    def fake_sync(*, on_progress=None, cancel_check=None, client_factory=None):
        synced["called"] = True
        return {"uid": UID, "imported_count": 0}

    monkeypatch.setattr("src.repost_cleanup.sync_repost_history", fake_sync)
    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert synced["called"] is True
    assert result["synced"] is True


def test_reconciliation_detects_missed_confirmed_after_mark_failure(
    isolated_home, monkeypatch,
) -> None:
    _login(monkeypatch)

    def broken_mark(uid, **kwargs):
        raise RuntimeError("mark_sync_needed 失败")

    monkeypatch.setattr("src.repost_history.mark_sync_needed", broken_mark)
    confirm_repost(UID, DYNAMIC_ID)

    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    assert getattr(get_checkpoint(UID), "sync_needed", False) is False
    assert reconciliation_needs_sync(UID) is True

    monkeypatch.setattr(
        "src.repost_cleanup.fetch_space_feed_page",
        lambda *args, **kwargs: {"items": [], "offset": ""},
    )
    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["synced"] is True
    # 同秒先后不确定时保守保留一次 reconciliation；未来一轮成功后闭包。
    checkpoint = get_checkpoint(UID)
    save_checkpoint(
        UID,
        head_dynamic_id=checkpoint.head_dynamic_id,
        head_published_at=checkpoint.head_published_at,
        full_scan_completed=checkpoint.full_scan_completed,
        last_synced_at=checkpoint.last_synced_at + 2,
    )
    assert reconciliation_needs_sync(UID) is False


def test_already_synced_confirmed_does_not_trigger_reconciliation(isolated_home) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    from src.repost_history import clear_sync_needed

    clear_sync_needed(UID, at=int(1_800_000_000))
    assert get_checkpoint(UID).last_synced_at == 1_800_000_000
    assert reconciliation_needs_sync(UID) is False


def test_reconciliation_survives_restart(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)

    def broken_mark(uid, **kwargs):
        raise RuntimeError("mark_sync_needed 失败")

    monkeypatch.setattr("src.repost_history.mark_sync_needed", broken_mark)
    confirm_repost(UID, DYNAMIC_ID)

    reset_engine_for_tests()
    assert reconciliation_needs_sync(UID) is True


def test_reconciliation_is_profile_scoped(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)

    def broken_mark(uid, **kwargs):
        raise RuntimeError("mark_sync_needed 失败")

    monkeypatch.setattr("src.repost_history.mark_sync_needed", broken_mark)
    confirm_repost(UID, DYNAMIC_ID)

    assert reconciliation_needs_sync(UID) is True
    assert reconciliation_needs_sync("54321") is False


def test_pending_unknown_suspected_do_not_trigger_reconciliation(isolated_home) -> None:
    record_pending(UID, DYNAMIC_ID)
    mark_repost_unknown(UID, DYNAMIC_ID)
    mark_repost_suspected(UID, "1000000000000000002")
    assert reconciliation_needs_sync(UID) is False


def test_risk_paused_wins_over_reconciliation(isolated_home, monkeypatch) -> None:
    from src.repost_history import pause_maintenance_risk

    _login(monkeypatch)

    def broken_mark(uid, **kwargs):
        raise RuntimeError("mark_sync_needed 失败")

    monkeypatch.setattr("src.repost_history.mark_sync_needed", broken_mark)
    confirm_repost(UID, DYNAMIC_ID)
    pause_maintenance_risk(UID, "风控暂停", at=100)

    assert reconciliation_needs_sync(UID) is True

    def forbidden_client():
        raise AssertionError("风控暂停中不得联网")

    result = auto_maintain(client_factory=forbidden_client)
    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"
    assert result["synced"] is False


def test_reconciliation_after_completed_sync_never_rescans_every_cycle(isolated_home) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    save_checkpoint(
        UID,
        head_dynamic_id="3000000000000000001",
        head_published_at=100,
        full_scan_completed=True,
        last_synced_at=int(1_800_000_000),
    )
    # confirmed_at 是刚才写入（< 1.8e9），last_synced_at 已在其后 → 不触发
    assert reconciliation_needs_sync(UID) is False


def test_confirmed_equal_to_last_synced_at_is_conservatively_reconciled(
    isolated_home, monkeypatch,
) -> None:
    monkeypatch.setattr("src.participation_guard.time.time", lambda: 1_700_000_000)
    confirm_repost(UID, DYNAMIC_ID)
    checkpoint = get_checkpoint(UID)
    save_checkpoint(
        UID,
        head_dynamic_id=None,
        head_published_at=None,
        full_scan_completed=True,
        last_synced_at=1_700_000_000,
    )
    assert clear_sync_needed_if_unchanged(
        UID,
        expected_sync_needed_at=checkpoint.sync_needed_at,
        at=1_700_000_001,
    ) is True

    assert reconciliation_needs_sync(UID) is True


def test_dirty_cas_clears_only_unchanged_watermark(isolated_home) -> None:
    mark_sync_needed(UID, occurred_at=100)
    snapshot = get_checkpoint(UID).sync_needed_at

    assert clear_sync_needed_if_unchanged(
        UID, expected_sync_needed_at=snapshot, at=101
    ) is True
    assert get_checkpoint(UID).sync_needed is False


def test_dirty_cas_preserves_new_confirmed_during_sync(isolated_home) -> None:
    mark_sync_needed(UID, occurred_at=100)
    snapshot = get_checkpoint(UID).sync_needed_at
    # 独立进程在同秒确认：单调水位也必须变化。
    mark_sync_needed(UID, occurred_at=100)

    assert clear_sync_needed_if_unchanged(
        UID, expected_sync_needed_at=snapshot, at=101
    ) is False
    checkpoint = get_checkpoint(UID)
    assert checkpoint.sync_needed is True
    assert checkpoint.sync_needed_at > snapshot


def test_real_sync_does_not_consume_confirmed_created_during_sync(
    isolated_home, monkeypatch,
) -> None:
    _login(monkeypatch)
    confirm_repost(UID, DYNAMIC_ID)
    initial_watermark = get_checkpoint(UID).sync_needed_at
    second_dynamic_id = "1000000000000000002"
    called = {"feed": 0}

    def fetch_page(*args, **kwargs):
        called["feed"] += 1
        confirm_repost(UID, second_dynamic_id)
        return {"items": [], "offset": ""}

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", fetch_page)

    result = sync_repost_history(client_factory=lambda: _FakeClient())

    assert result["sync_needed_cleared"] is False
    assert called["feed"] == 1
    checkpoint = get_checkpoint(UID)
    assert checkpoint.sync_needed is True
    assert checkpoint.sync_needed_at > initial_watermark


def test_dirty_state_survives_restart(isolated_home) -> None:
    mark_sync_needed(UID, occurred_at=100)
    before = get_checkpoint(UID)
    reset_engine_for_tests()

    after = get_checkpoint(UID)
    assert after.sync_needed is True
    assert after.sync_needed_at == before.sync_needed_at


def test_manual_sync_action_does_not_clear_newer_dirty(
    isolated_home, monkeypatch,
) -> None:
    from web.actions import run_action

    mark_sync_needed(UID, occurred_at=100)
    snapshot = get_checkpoint(UID).sync_needed_at

    def fake_sync(*args, **kwargs):
        mark_sync_needed(UID, occurred_at=100)
        return {"uid": UID, "found_reposts": 0, "imported_count": 0}

    monkeypatch.setattr("src.repost_cleanup.sync_repost_history", fake_sync)

    result = run_action("sync_repost_history")

    assert result["ok"] is True
    checkpoint = get_checkpoint(UID)
    assert checkpoint.sync_needed is True
    assert checkpoint.sync_needed_at > snapshot


def test_repeated_confirmed_idempotent_call_does_not_refresh_dirty_watermark(
    isolated_home, monkeypatch,
) -> None:
    monkeypatch.setattr("src.participation_guard.time.time", lambda: 100)
    confirm_repost(UID, DYNAMIC_ID)
    first = get_checkpoint(UID).sync_needed_at
    monkeypatch.setattr("src.participation_guard.time.time", lambda: 200)
    confirm_repost(UID, DYNAMIC_ID)

    assert get_checkpoint(UID).sync_needed_at == first
