"""V84 crash/restart boundaries: temporary JSON, no real HTTP or Bilibili."""
from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from src.notification import service as notify
from src.winner_scan import service as winner
from tests.test_winner_deep_scan_m3 import _feed_entry, WIN_TEXT
from src.message_types import MessageType


KEY = "mention:84"
EVENT = f"winning_candidate:{KEY}"


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(winner, "get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(notify, "get_profile_dir", lambda: tmp_path)
    for channel in notify.ALL_CHANNELS:
        notify.set_channel_enabled(channel, channel == "serverchan")
    calls = []
    monkeypatch.setattr(notify, "_provider_send", lambda channel, *args: calls.append(channel) or "sent")

    def forbidden(*args, **kwargs):
        pytest.fail("local recovery must not access Bilibili or authentication")

    for name in ("list_feed", "list_dm_sessions", "list_dm_sessions_page", "fetch_dm_messages", "require_login"):
        monkeypatch.setattr(winner, name, forbidden)
    monkeypatch.setattr("src.bilibili_client.BilibiliClient", forbidden)
    return calls


def record(key=KEY):
    return {"key": key, "notification_event_id": f"winning_candidate:{key}",
            "summary": WIN_TEXT, "source_type": "mention", "discovered_at": 84}


def prepare(*, enabled=True, key=KEY):
    winner.WinnerScanService()._save_new_candidates({key: record(key)}, [key], notify=enabled)


def restart():
    winner.WinnerScanService(notification=notify.NotificationService()).recover_notification_preparations()


def state():
    return winner._load_candidates()[KEY]["notification_preparation"]["status"]


def prune_logs():
    notify._mutate_outbox(lambda records: records + [
        {"event_id": f"other:{i}", "channel": "serverchan", "status": "sent"} for i in range(205)
    ])
    assert not any(row["event_id"] == EVENT for row in notify._load_outbox())


def test_candidate_and_preparation_share_atomic_save(local, monkeypatch):
    real_write = winner._write_json
    def crash_before_replace(path, payload):
        assert payload["candidates"][0]["notification_preparation"]["status"] == "pending"
        raise SystemExit("before candidate replace")
    monkeypatch.setattr(winner, "_write_json", crash_before_replace)
    with pytest.raises(SystemExit):
        prepare()
    assert winner._load_candidates() == {}
    assert local == []
    monkeypatch.setattr(winner, "_write_json", real_write)
    prepare()
    restart()
    assert local == ["serverchan"]


def test_restart_after_candidate_save_recovers_same_candidate_without_feed(local):
    prepare()
    assert state() == "pending"
    assert notify._load_outbox() == []
    checkpoint = winner._load_state()
    checkpoint["mention"].update(cursor_id=999, checkpoint_keys=["mention:999"],
                                 catch_up={"request_cursor": {"id": 500}})
    winner._save_state(checkpoint)
    original = winner._state_path().read_bytes()
    restart()
    restart()
    assert local == ["serverchan"]
    assert state() == "claimed"
    assert winner._state_path().read_bytes() == original
    assert notify._load_outbox()[0]["event_id"] == EVENT


def test_claim_write_failure_zero_http_remains_recoverable(local, monkeypatch):
    prepare()
    write = notify._write_json
    monkeypatch.setattr(notify, "_write_json", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(notify.NotificationLocalError):
        restart()
    assert state() == "pending"
    assert local == []
    assert notify._load_outbox() == []
    monkeypatch.setattr(notify, "_write_json", write)
    restart()
    assert local == ["serverchan"]


@pytest.mark.parametrize("crash_point", ["before_transition", "before_http"])
def test_claim_success_crash_never_reclaims_even_after_pruning(local, monkeypatch, crash_point):
    prepare()
    claim = notify._claim_records
    calls = []
    def crashing_claim(*args, **kwargs):
        calls.append(1)
        claim(*args, **kwargs)
        if crash_point == "before_transition":
            raise SystemExit("after durable claim")
    monkeypatch.setattr(notify, "_claim_records", crashing_claim)
    send = notify._provider_send
    monkeypatch.setattr(notify, "_provider_send", lambda *a: (_ for _ in ()).throw(SystemExit("before HTTP")))
    with pytest.raises(SystemExit):
        restart()
    assert notify._winner_claims()[EVENT] == ["serverchan"]
    prune_logs()
    monkeypatch.setattr(notify, "_provider_send", send)
    restart()
    assert calls == [1]
    assert local == []
    assert state() == "claimed"


@pytest.mark.parametrize("result", ["sent", "delivery_unknown", "failed"])
def test_terminal_never_resends_after_restart_and_pruning(local, monkeypatch, result):
    prepare()
    monkeypatch.setattr(notify, "_provider_send", lambda channel, *a: local.append(channel) or result)
    restart()
    prune_logs()
    restart()
    # Even a stale candidate snapshot cannot erase durable claim evidence.
    prepare()
    restart()
    assert local == ["serverchan"]


def test_result_write_failure_after_http_never_resends(local, monkeypatch):
    prepare()
    real_finalize = notify._finalize_record
    monkeypatch.setattr(notify, "_finalize_record", lambda *a: (_ for _ in ()).throw(notify.NotificationLocalError("disk full")))
    with pytest.raises(notify.NotificationLocalError):
        restart()
    monkeypatch.setattr(notify, "_finalize_record", real_finalize)
    restart()
    assert local == ["serverchan"]


def test_transition_write_failure_after_claim_is_not_retryable(local, monkeypatch):
    prepare()
    save = winner._save_candidates
    monkeypatch.setattr(winner, "_save_candidates", lambda *a: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(notify.NotificationLocalError):
        restart()
    assert local == []
    assert state() == "pending"
    prune_logs()
    monkeypatch.setattr(winner, "_save_candidates", save)
    restart()
    assert state() == "claimed"
    assert local == []


@pytest.mark.parametrize("disabled", ["legacy", "event", "channel", "notify"])
def test_legacy_and_disabled_candidates_are_never_backfilled(local, disabled):
    if disabled == "event":
        notify.set_event_enabled("winning_candidate", False)
    if disabled == "channel":
        notify.set_channel_enabled("serverchan", False)
    if disabled == "legacy":
        winner._save_candidates({KEY: record()})
    else:
        prepare(enabled=disabled != "notify")
    notify.set_event_enabled("winning_candidate", True)
    notify.set_channel_enabled("serverchan", True)
    restart()
    assert local == []
    assert notify._load_outbox() == []


def test_channel_snapshot_does_not_backfill_later_enabled_channel(local):
    prepare()
    notify.set_channel_enabled("feishu", True)
    restart()
    winner.WinnerScanService()._dispatch_new(winner._load_candidates(), [KEY], notify=True)
    assert local == ["serverchan"]


def test_disabled_discovery_cannot_send_if_enabled_before_dispatch(local):
    notify.set_channel_enabled("serverchan", False)
    prepare()
    notify.set_channel_enabled("serverchan", True)
    winner.WinnerScanService()._dispatch_new(winner._load_candidates(), [KEY], notify=True)
    assert local == []


@pytest.mark.parametrize("reason", ["cancel", "identity"])
def test_run_checks_identity_and_cancellation_before_recovery_http(local, monkeypatch, reason):
    prepare()
    monkeypatch.setattr(winner, "require_login", lambda: ("csrf", 101))
    with pytest.raises(RuntimeError, match="已取消|登录账号与预期不符"):
        winner.WinnerScanService().run(
            object(), expected_uid=999 if reason == "identity" else 101,
            cancel_check=lambda: reason == "cancel",
        )
    assert state() == "pending"
    assert local == []


def test_two_concurrent_recoveries_only_claim_once(local, monkeypatch):
    prepare()
    claim = notify._claim_records
    claims = []
    def counted(*args, **kwargs):
        claims.append(1)
        return claim(*args, **kwargs)
    monkeypatch.setattr(notify, "_claim_records", counted)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: restart(), range(2)))
    assert claims == [1]
    assert local == ["serverchan"]


def test_cross_process_recovery_serializes_with_scan_dispatch(local, tmp_path, monkeypatch):
    prepare()
    # Hold the actual OS lease in another process. No network or real Profile.
    script = """
import sys
from pathlib import Path
from src.profile_writer_lease import _ProfileWriterLease
lease = _ProfileWriterLease(Path(sys.argv[1]))
assert lease.acquire()
print('held', flush=True)
sys.stdin.readline()
lease.release()
"""
    child = subprocess.Popen([sys.executable, "-c", script, str(tmp_path / "winner_notification.lock")],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(notify.NotificationLocalError):
            winner.WinnerScanService()._dispatch_new(winner._load_candidates(), [KEY], notify=True)
        assert local == []
    finally:
        child.communicate("release\n", timeout=10)
    restart()
    assert local == ["serverchan"]


def test_profile_local_same_candidate(local, tmp_path, monkeypatch):
    prepare()
    other = tmp_path / "other"
    monkeypatch.setattr(winner, "get_profile_dir", lambda: other)
    monkeypatch.setattr(notify, "get_profile_dir", lambda: other)
    notify.set_channel_enabled("serverchan", True)
    restart()
    assert local == []
    prepare()
    restart()
    monkeypatch.setattr(winner, "get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(notify, "get_profile_dir", lambda: tmp_path)
    assert state() == "pending"
    restart()
    assert local == ["serverchan", "serverchan"]


def test_mixed_channels_terminal_preserved_batch_claim_before_http(local, monkeypatch):
    for channel in notify.ALL_CHANNELS:
        notify.set_channel_enabled(channel, True)
    prepare()
    notify._save_outbox([
        {"event_id": EVENT, "channel": "serverchan", "status": "sent"},
        {"event_id": EVENT, "channel": "dingtalk", "status": "delivery_unknown"},
    ])
    def send(channel, *args):
        assert set(notify._winner_claims()[EVENT]) == set(notify.ALL_CHANNELS)
        assert state() == "claimed"
        local.append(channel)
        return "sent"
    monkeypatch.setattr(notify, "_provider_send", send)
    restart()
    assert local == ["wecom", "feishu"]
    prune_logs()
    prepare()
    restart()
    assert local == ["wecom", "feishu"]


def test_pending_survives_candidate_retention(local):
    candidates = {KEY: record()}
    candidates.update({f"old:{i}": {"key": f"old:{i}", "discovered_at": 0} for i in range(501)})
    winner.WinnerScanService()._save_new_candidates(candidates, [KEY], notify=True)
    assert state() == "pending"
    restart()
    assert local == ["serverchan"]


def test_scan_recovers_old_preparation_before_remote_failure(local, monkeypatch):
    prepare()
    monkeypatch.setattr(winner, "require_login", lambda: ("csrf", 101))
    monkeypatch.setattr(winner, "list_feed", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("offline feed")))
    monkeypatch.setattr(winner, "list_dm_sessions", lambda *a, **k: [])
    result = winner.WinnerScanService().run(object())
    assert result["partial"]
    assert local == ["serverchan"]


def test_run_save_then_crash_recovers_same_candidate_on_new_instance(local, monkeypatch):
    monkeypatch.setattr(winner, "require_login", lambda: ("csrf", 101))
    monkeypatch.setattr(winner, "list_feed", lambda c, kind: (
        [_feed_entry("84", WIN_TEXT)] if kind == MessageType.AT else [], {"is_end": True, "id": "84"}))
    monkeypatch.setattr(winner, "list_dm_sessions", lambda *a, **k: [])
    service = winner.WinnerScanService()
    monkeypatch.setattr(service, "_dispatch_new", lambda *a, **k: (_ for _ in ()).throw(SystemExit("saved")))
    with pytest.raises(SystemExit):
        service.run(object())
    assert state() == "pending"
    assert local == []
    restart()
    assert local == ["serverchan"]


def test_corrupt_claim_evidence_fails_closed(local, tmp_path):
    prepare()
    (tmp_path / "notify_outbox.json").write_text('{"winner_claims": null}', encoding="utf-8")
    with pytest.raises(notify.NotificationLocalError):
        restart()
    assert local == []
    assert state() == "pending"
