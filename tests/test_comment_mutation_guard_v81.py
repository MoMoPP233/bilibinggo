from __future__ import annotations

import importlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import httpx
import pytest

from src import comment_confirmation as confirmation
from src import lottery_actions as actions


DYNAMIC_ID = "1251573729479098372"
UID = 42
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _fake_logged_in_identity(monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID))


def _kwargs(*, message: str = "first comment", uid: int = UID) -> dict:
    return {
        "rid": DYNAMIC_ID,
        "comment_type": 17,
        "message": message,
        "csrf": "fake-csrf",
        "referer": f"https://www.bilibili.com/opus/{DYNAMIC_ID}",
        "dynamic_id": DYNAMIC_ID,
        "uid": uid,
    }


def _patch_status_writer(monkeypatch, replacement) -> None:
    # Support the production module's direct helper import without bypassing
    # its public mutation gate or replacing the actual persistence implementation.
    monkeypatch.setattr(confirmation, "_set_comment_status_unlocked", replacement)
    if hasattr(actions, "_set_comment_status_unlocked"):
        monkeypatch.setattr(actions, "_set_comment_status_unlocked", replacement)


def _status() -> str | None:
    return confirmation.get_comment_status(DYNAMIC_ID, uid=UID)


def _assert_next_call_does_not_post(*, message: str = "different text") -> None:
    client = Mock()
    client.post_form.side_effect = AssertionError("durable guard allowed a second POST")
    actions.comment_dynamic(client, **_kwargs(message=message))
    client.post_form.assert_not_called()


class SimulatedCrash(BaseException):
    """Model abrupt process loss without ordinary Exception recovery handlers."""


def test_pending_write_failure_prevents_any_comment_post(isolated_home, monkeypatch) -> None:
    real_writer = confirmation._set_comment_status_unlocked

    def fail_pending(dynamic_id, *, uid, status):
        if status == "pending":
            raise OSError("pending storage unavailable")
        return real_writer(dynamic_id, uid=uid, status=status)

    _patch_status_writer(monkeypatch, fail_pending)
    client = Mock()
    with pytest.raises(OSError, match="pending storage"):
        actions.comment_dynamic(client, **_kwargs())
    client.post_form.assert_not_called()
    assert _status() is None


@pytest.mark.parametrize("success_code", [0, 12051])
def test_durable_pending_precedes_post_and_success_confirms(isolated_home, success_code) -> None:
    client = Mock()

    def post(url, data, **kwargs):
        # Inspect the real persisted file, not a mocked status return value.
        persisted = json.loads(confirmation._state_path().read_text(encoding="utf-8"))
        assert "pending" in json.dumps(persisted)
        assert url == actions.COMMENT_URL
        assert data["oid"] == DYNAMIC_ID
        assert kwargs["retries"] == 0
        return {"code": success_code}

    client.post_form.side_effect = post
    result = actions.comment_dynamic(client, **_kwargs())
    assert result.ok is True
    assert client.post_form.call_count == 1
    assert _status() == "confirmed"
    _assert_next_call_does_not_post()


@pytest.mark.parametrize(
    "exception",
    [
        httpx.ReadTimeout("response lost"),
        httpx.ConnectError("connection failed"),
        httpx.ConnectError("SSL: UNEXPECTED_EOF_WHILE_READING"),
        json.JSONDecodeError("damaged response", "{", 1),
    ],
    ids=["timeout", "connect", "tls-eof", "json-decode"],
)
def test_uncertain_transport_is_durable_and_never_retried(isolated_home, exception) -> None:
    client = Mock()
    client.post_form.side_effect = exception
    with pytest.raises(type(exception)):
        actions.comment_dynamic(client, **_kwargs())
    assert client.post_form.call_count == 1
    assert _status() in {"pending", "unknown"}
    importlib.reload(confirmation)
    assert _status() in {"pending", "unknown"}
    _assert_next_call_does_not_post()


@pytest.mark.parametrize(
    "payload", [None, [], {}, {"code": "0"}, {"code": True}, {"code": None}, {"code": -500}],
)
def test_unverifiable_response_keeps_barrier(isolated_home, payload) -> None:
    client = Mock()
    client.post_form.return_value = payload
    result = actions.comment_dynamic(client, **_kwargs())
    assert not result.ok
    assert _status() in {"pending", "unknown"}
    importlib.reload(confirmation)
    _assert_next_call_does_not_post()
    assert client.post_form.call_count == 1


@pytest.mark.parametrize("status", ["pending", "unknown", "confirmed"])
def test_restart_and_changed_comment_text_cannot_reopen_guard(isolated_home, status) -> None:
    confirmation._set_comment_status_unlocked(DYNAMIC_ID, uid=UID, status=status)
    importlib.reload(confirmation)
    assert _status() == status
    _assert_next_call_does_not_post(message="completely different comment after restart")
    assert _status() == status


@pytest.mark.parametrize("code", [12078, 12014])
def test_explicit_business_rejection_is_not_permanent_unknown(isolated_home, code) -> None:
    client = Mock()
    client.post_form.return_value = {"code": code, "message": "explicit rejection"}
    result = actions.comment_dynamic(client, **_kwargs())
    assert not result.ok
    assert _status() == "failed"
    assert client.post_form.call_count == 1
    if code == 12014:
        from src.comment_breaker import is_comment_breaker_active

        assert is_comment_breaker_active()
    else:
        client.post_form.return_value = {"code": 0}
        assert actions.comment_dynamic(client, **_kwargs(message="later attempt")).ok
        assert client.post_form.call_count == 2
        assert _status() == "confirmed"


def test_success_then_confirmed_write_failure_keeps_durable_barrier(isolated_home, monkeypatch) -> None:
    real_writer = confirmation._set_comment_status_unlocked

    def fail_confirmation(dynamic_id, *, uid, status):
        if status == "confirmed":
            raise OSError("confirmed storage unavailable")
        return real_writer(dynamic_id, uid=uid, status=status)

    _patch_status_writer(monkeypatch, fail_confirmation)
    client = Mock()
    client.post_form.return_value = {"code": 0}
    with pytest.raises(OSError, match="confirmed storage"):
        actions.comment_dynamic(client, **_kwargs())
    assert client.post_form.call_count == 1
    assert _status() in {"pending", "unknown"}
    importlib.reload(confirmation)
    _assert_next_call_does_not_post()


def test_unknown_write_failure_leaves_pending_that_blocks_restart(isolated_home, monkeypatch) -> None:
    real_writer = confirmation._set_comment_status_unlocked

    def fail_unknown(dynamic_id, *, uid, status):
        if status == "unknown":
            raise OSError("unknown storage unavailable")
        return real_writer(dynamic_id, uid=uid, status=status)

    _patch_status_writer(monkeypatch, fail_unknown)
    client = Mock()
    client.post_form.side_effect = httpx.ReadTimeout("response lost")
    with pytest.raises((httpx.ReadTimeout, OSError)):
        actions.comment_dynamic(client, **_kwargs())
    assert client.post_form.call_count == 1
    assert _status() == "pending"
    importlib.reload(confirmation)
    _assert_next_call_does_not_post()


@pytest.mark.parametrize("window", ["before_pending", "after_pending", "after_post", "after_success"])
def test_crash_windows_survive_restart_without_duplicate_mutation(isolated_home, monkeypatch, window) -> None:
    real_writer = confirmation._set_comment_status_unlocked
    mutations = []

    def crash_writer(dynamic_id, *, uid, status):
        if window == "before_pending" and status == "pending":
            raise SimulatedCrash()
        if window == "after_success" and status == "confirmed":
            raise SimulatedCrash()
        value = real_writer(dynamic_id, uid=uid, status=status)
        if window == "after_pending" and status == "pending":
            raise SimulatedCrash()
        return value

    def post(*args, **kwargs):
        mutations.append(True)
        if window == "after_post":
            raise SimulatedCrash()
        return {"code": 0}

    _patch_status_writer(monkeypatch, crash_writer)
    client = Mock()
    client.post_form.side_effect = post
    with pytest.raises(SimulatedCrash):
        actions.comment_dynamic(client, **_kwargs())
    assert len(mutations) == (1 if window in {"after_post", "after_success"} else 0)
    _patch_status_writer(monkeypatch, real_writer)
    importlib.reload(confirmation)
    if window == "before_pending":
        assert _status() is None
        recovered = Mock()
        recovered.post_form.return_value = {"code": 0}
        assert actions.comment_dynamic(recovered, **_kwargs()).ok
        assert recovered.post_form.call_count == 1
    else:
        assert _status() in {"pending", "unknown"}
        _assert_next_call_does_not_post()


def test_confirmed_survives_unrelated_later_persistence_failure(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)
    client = Mock()
    client.post_form.return_value = {"code": 0}

    def persist_action(action):
        if action.action == "comment":
            assert _status() == "confirmed"
            raise OSError("later participation result save failed")

    context = actions.DynamicContext(
        DYNAMIC_ID, 123, _kwargs()["referer"], DYNAMIC_ID, 17,
        True, True, True, True, True, False,
    )
    with pytest.raises(OSError, match="later participation"):
        actions.execute_full_participation(
            client, dynamic_id=DYNAMIC_ID, context=context,
            action_text="first comment", on_action=persist_action,
        )
    assert client.post_form.call_count == 1
    importlib.reload(confirmation)
    assert _status() == "confirmed"
    _assert_next_call_does_not_post()


def test_more_than_500_logs_prune_old_unknown_without_removing_comment_guard(isolated_home) -> None:
    from sqlmodel import select

    from src.db.models import ParticipationActionRow
    from src.db.session import session_scope
    from src.participation_log import (
        ParticipationActionRecord,
        append_action_record_unlocked,
        load_action_entries_for_uid,
    )

    confirmation._set_comment_status_unlocked(DYNAMIC_ID, uid=UID, status="unknown")
    with session_scope() as session:
        # Seed historical records in one transaction, then exercise the real
        # production append/prune path at the 500-entry boundary.
        for number in range(501):
            session.add(ParticipationActionRow(
                uid=str(UID), recorded_at=number,
                dynamic_id=DYNAMIC_ID if number == 0 else str(2000000000000000000 + number),
                lottery_type="转发抽奖", status="failed", message="old result", action_text="old text",
                actions_json="[]",
                context_snapshot_json=json.dumps({"result_class": "unknown", "unknown_action": "comment"}),
            ))
    append_action_record_unlocked(
        ParticipationActionRecord(502, "2999999999999999999", "转发抽奖", "failed", "later", "", [], {}),
        uid=str(UID),
    )
    entries = load_action_entries_for_uid(str(UID))
    assert len(entries) == 500
    assert all(item["dynamic_id"] != DYNAMIC_ID for item in entries)
    importlib.reload(confirmation)
    assert _status() == "unknown"
    _assert_next_call_does_not_post()
    with session_scope() as session:
        for row in session.exec(select(ParticipationActionRow).where(ParticipationActionRow.uid == str(UID))).all():
            session.delete(row)
    assert load_action_entries_for_uid(str(UID)) == []
    assert _status() == "unknown"
    _assert_next_call_does_not_post()


def test_different_uids_same_dynamic_do_not_share_guard(isolated_home, monkeypatch) -> None:
    client = Mock()
    client.post_form.return_value = {"code": 0}
    assert actions.comment_dynamic(client, **_kwargs(uid=UID)).ok
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID + 1) is None
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID + 1))
    assert actions.comment_dynamic(client, **_kwargs(uid=UID + 1)).ok
    assert client.post_form.call_count == 2
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "confirmed"
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID + 1) == "confirmed"


def test_explicit_uid_must_match_current_logged_in_account(isolated_home) -> None:
    client = Mock()
    with pytest.raises(RuntimeError, match="登录账号发生变化"):
        actions.comment_dynamic(client, **_kwargs(uid=UID + 1))
    client.post_form.assert_not_called()
    assert _status() is None
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID + 1) is None


@pytest.mark.parametrize("status", ["pending", "unknown", "confirmed"])
def test_blocking_states_never_expire_or_require_remote_confirmation(isolated_home, monkeypatch, status) -> None:
    confirmation._set_comment_status_unlocked(DYNAMIC_ID, uid=UID, status=status)
    original = confirmation._state_path().read_bytes()
    monkeypatch.setattr(confirmation.time, "time", lambda: 9999999999999)
    client = Mock()
    client.request_json.side_effect = AssertionError("local durable state must not require remote lookup")
    assert actions.has_comment(
        client, rid=DYNAMIC_ID, comment_type=17, action_text="new text", referer=_kwargs()["referer"],
        dynamic_id=DYNAMIC_ID, uid=UID,
    ) is (status == "confirmed")
    client.request_json.assert_not_called()
    _assert_next_call_does_not_post()
    assert confirmation._state_path().read_bytes() == original


def test_different_profiles_same_uid_and_dynamic_do_not_share_guard(isolated_home, monkeypatch) -> None:
    from src import comment_breaker

    profile_a = isolated_home / "synthetic-account-1"
    profile_b = isolated_home / "synthetic-account-2"
    current = [profile_a]
    monkeypatch.setattr(confirmation, "get_profile_dir", lambda: current[0])
    monkeypatch.setattr(comment_breaker, "get_profile_dir", lambda: current[0])
    client = Mock()
    client.post_form.return_value = {"code": 0}
    assert actions.comment_dynamic(client, **_kwargs()).ok
    current[0] = profile_b
    assert _status() is None
    assert actions.comment_dynamic(client, **_kwargs()).ok
    current[0] = profile_a
    _assert_next_call_does_not_post()
    assert client.post_form.call_count == 2


def test_legacy_confirmed_remains_profile_wide_and_survives_v2_write(isolated_home, monkeypatch) -> None:
    from src import comment_breaker

    path = confirmation._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "confirmed": {DYNAMIC_ID: 12345}}), encoding="utf-8")
    client = Mock()
    for uid in [UID, UID + 1]:
        monkeypatch.setattr(actions, "require_login", lambda uid=uid: ("fake-csrf", uid))
        assert confirmation.get_comment_status(DYNAMIC_ID, uid=uid) == "confirmed"
        assert actions.comment_dynamic(client, **_kwargs(uid=uid)).ok
    client.post_form.assert_not_called()

    confirmation.confirm_comment("1251573729479098373", uid=UID)
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["version"] == 2
    assert persisted["confirmed"] == {DYNAMIC_ID: 12345}
    importlib.reload(confirmation)
    for uid in [UID, UID + 1]:
        assert confirmation.get_comment_status(DYNAMIC_ID, uid=uid) == "confirmed"
    other = isolated_home / "legacy-isolated-account-2"
    monkeypatch.setattr(confirmation, "get_profile_dir", lambda: other)
    monkeypatch.setattr(comment_breaker, "get_profile_dir", lambda: other)
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) is None


@pytest.mark.parametrize(
    "raw", ["{", "[]", '{"version":99,"confirmed":{}}', '{"version":2,"confirmed":{},"guards":{"42":[]}}'],
)
def test_unreadable_or_unsupported_guard_fails_closed(isolated_home, raw) -> None:
    path = confirmation._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    client = Mock()
    with pytest.raises(confirmation.CommentGuardStateError):
        actions.comment_dynamic(client, **_kwargs())
    client.post_form.assert_not_called()
    assert path.read_text(encoding="utf-8") == raw


def test_canonical_dynamic_urls_share_barrier_but_raw_video_id_does_not(isolated_home) -> None:
    confirmation.confirm_comment(f"https://t.bilibili.com/{DYNAMIC_ID}", uid=UID)
    assert confirmation.get_comment_status(f"https://www.bilibili.com/opus/{DYNAMIC_ID}", uid=UID) == "confirmed"
    _assert_next_call_does_not_post()

    client = Mock()
    client.post_form.return_value = {"code": 0}
    raw_reply = {**_kwargs(), "dynamic_id": None, "comment_type": 1}
    assert actions.comment_dynamic(client, **raw_reply).ok
    assert client.post_form.call_count == 1
    assert confirmation.get_comment_status(f"reply:1:{DYNAMIC_ID}", uid=UID) == "confirmed"


def test_two_threads_same_identity_send_at_most_one_comment(isolated_home) -> None:
    client = Mock()
    client.post_form.return_value = {"code": 0}
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: actions.comment_dynamic(client, **_kwargs()), range(2)))
    assert all(result.ok for result in results)
    assert client.post_form.call_count == 1
    assert _status() == "confirmed"


_COMMENT_CHILD = r"""
import json
import sys
import time
from pathlib import Path
from tests.offline_guard import install_offline_guard
install_offline_guard()
from src import comment_confirmation as confirmation, comment_breaker, lottery_actions as actions
profile = Path(sys.argv[1])
confirmation.get_profile_dir = lambda: profile
comment_breaker.get_profile_dir = lambda: profile
actions.require_login = lambda: ('fake', 42)
class FakeClient:
    mutations = 0
    def post_form(self, url, data, **kwargs):
        assert kwargs['retries'] == 0
        persisted = json.loads(confirmation._state_path().read_text(encoding='utf-8'))
        assert 'pending' in json.dumps(persisted)
        self.mutations += 1
        time.sleep(0.2)
        return {'code': 0}
client = FakeClient()
result = actions.comment_dynamic(
    client, rid=sys.argv[2], comment_type=17, message='process text', csrf='fake',
    referer='https://www.bilibili.com/', dynamic_id=sys.argv[2], uid=42,
)
assert result.ok
print('V81_RESULT=' + json.dumps({'mutations': client.mutations, 'status': confirmation.get_comment_status(sys.argv[2], uid=42)}))
"""


def test_two_processes_share_durable_gate_without_real_requests(isolated_home) -> None:
    profile = isolated_home / "subprocess-shared-profile"

    def run_child(index):
        # Keep unrelated first-run layout migration out of the concurrency
        # test. Both workers still use exactly the same real guard and gate.
        child_root = isolated_home / f"subprocess-runtime-{index}"
        env = dict(os.environ)
        for name in ("BINGGO_DATA_ROOT", "BINGGO_LEGACY_DATA_ROOT", "BINGGO_HOME"):
            env[name] = str(child_root)
        env["BINGGO_DATA_ROOT_LOCATOR"] = str(child_root / "locator.json")
        result = subprocess.run(
            [sys.executable, "-c", _COMMENT_CHILD, str(profile), DYNAMIC_ID],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        line = next(line for line in result.stdout.splitlines() if line.startswith("V81_RESULT="))
        return json.loads(line.removeprefix("V81_RESULT="))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(run_child, range(2)))
    assert sum(item["mutations"] for item in results) == 1
    assert all(item["status"] == "confirmed" for item in results)
    assert run_child(2)["mutations"] == 0
