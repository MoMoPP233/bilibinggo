from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from sqlalchemy import delete

from src import favorite_confirmation as guard
from src import lottery_actions as actions
from src import participation as flow
from src.db.models import ParticipationActionRow
from src.db.session import session_scope
from web import activity_service

UID = 42
TARGET = "1251573729479098372"
OTHER_TARGET = "1251573729479098373"
REFERER = f"https://www.bilibili.com/opus/{TARGET}"


@pytest.fixture(autouse=True)
def _isolated_account(isolated_home, monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID))
    monkeypatch.setattr(flow, "require_login", lambda: ("fake-csrf", UID))


def _favorite(client: Mock, *, target: str = TARGET):
    return actions.favorite_dynamic(
        client, dynamic_id=target, csrf="fake-csrf", referer=REFERER, known_status=False,
    )


def _remote_status(value: bool | None) -> dict:
    return {
        "code": 0,
        "data": {"item": {"modules": {"module_stat": {"favorite": {"status": value}}}}},
    }


def _seed(status: str, *, target: str = TARGET, uid: int = UID) -> None:
    with guard.favorite_mutation_gate():
        guard._set_favorite_status_unlocked(target, uid=uid, status=status)


def _legacy_unknown(*, target: str = TARGET, uid: int = UID) -> None:
    with session_scope() as session:
        session.add(ParticipationActionRow(
            uid=str(uid), dynamic_id=target, recorded_at=1, status="failed",
            context_snapshot_json=json.dumps({"result_class": "unknown", "unknown_action": "favorite"}),
        ))


def test_explicit_success_confirms_and_uses_exact_target():
    client = Mock()
    client.post_json.return_value = {"code": 0}
    client.get_json.return_value = _remote_status(True)
    assert _favorite(client).ok is True
    assert guard.get_favorite_status(TARGET, uid=UID) == "confirmed"
    assert client.post_json.call_args.args[1]["entity"]["object_id_str"] == TARGET
    assert client.post_json.call_args.kwargs["retries"] == 0
    assert _favorite(client).ok is True
    client.post_json.assert_called_once()


def test_explicit_auth_rejection_is_definite_failure():
    client = Mock()
    client.post_json.return_value = {"code": -101, "message": "not logged in"}
    with pytest.raises(actions.ParticipationAuthError):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "failed"
    client.post_json.assert_called_once()


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timed out"), RuntimeError("connection lost")])
def test_transport_exception_keeps_unknown(error):
    client = Mock()
    client.post_json.side_effect = error
    with pytest.raises(type(error)):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    with pytest.raises(RuntimeError, match="阻止自动重试"):
        _favorite(client)
    client.post_json.assert_called_once()


def test_process_exit_after_pending_preserves_barrier():
    client = Mock()
    client.post_json.side_effect = SystemExit(1)
    with pytest.raises(SystemExit):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"
    with pytest.raises(RuntimeError, match="阻止自动重试"):
        _favorite(client)
    client.post_json.assert_called_once()


def test_missing_or_stale_success_confirmation_is_unknown():
    client = Mock()
    client.post_json.return_value = {"code": 0}
    client.get_json.return_value = _remote_status(False)
    with pytest.raises(RuntimeError, match="远端状态未确认"):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    client.post_json.assert_called_once()


def test_malformed_response_keeps_unknown():
    client = Mock()
    client.post_json.return_value = {"message": "missing code"}
    with pytest.raises(RuntimeError, match="响应缺少有效 code"):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"


def test_persistence_failure_prevents_post(monkeypatch):
    client = Mock()
    monkeypatch.setattr(guard, "_write_state_unlocked", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        _favorite(client)
    client.post_json.assert_not_called()


def test_confirmation_write_failure_leaves_pending(monkeypatch):
    client = Mock()
    client.post_json.return_value = {"code": 75008, "message": "already favored"}
    original = guard._write_state_unlocked

    def fail_confirmed(state):
        if state["guards"][str(UID)][TARGET]["status"] == "confirmed":
            raise OSError("confirmation failed")
        original(state)

    monkeypatch.setattr(guard, "_write_state_unlocked", fail_confirmed)
    with pytest.raises(OSError, match="confirmation failed"):
        _favorite(client)
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"
    client.post_json.assert_called_once()


def test_uncertain_guard_survives_action_log_prune_and_reload():
    _seed("pending")
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"
    with session_scope() as session:
        session.execute(delete(ParticipationActionRow))
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"
    _seed("unknown")
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"


def test_same_target_concurrent_calls_send_one_post():
    started, release = threading.Event(), threading.Event()
    client = Mock()

    def post(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return {"code": 75008}

    client.post_json.side_effect = post
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_favorite, client)
        assert started.wait(5)
        second = pool.submit(_favorite, client)
        release.set()
        assert first.result(timeout=5).ok is True
        assert second.result(timeout=5).ok is True
    client.post_json.assert_called_once()


def test_profile_uid_target_isolation(monkeypatch, tmp_path):
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    monkeypatch.setattr(guard, "get_profile_dir", lambda: profile_a)
    _seed("pending")
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"
    assert guard.get_favorite_status(TARGET, uid=UID + 1) is None
    assert guard.get_favorite_status(OTHER_TARGET, uid=UID) is None
    monkeypatch.setattr(guard, "get_profile_dir", lambda: profile_b)
    assert guard.get_favorite_status(TARGET, uid=UID) is None
    monkeypatch.setattr(guard, "get_profile_dir", lambda: profile_a)
    assert guard.get_favorite_status(TARGET, uid=UID) == "pending"


def test_unknown_remote_precheck_never_posts():
    client = Mock()
    client.get_json.side_effect = RuntimeError("read unavailable")
    with pytest.raises(actions.ParticipationReadError, match="收藏状态未知"):
        actions.favorite_dynamic(client, dynamic_id=TARGET, csrf="x", referer=REFERER)
    client.post_json.assert_not_called()
    assert guard.get_favorite_status(TARGET, uid=UID) is None


def test_legacy_unknown_materializes_and_survives_log_removal():
    _legacy_unknown()
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    with session_scope() as session:
        session.execute(delete(ParticipationActionRow))
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    assert guard.get_favorite_status(TARGET, uid=UID + 1) is None


def test_legacy_code_zero_unconfirmed_read_is_also_uncertain():
    with session_scope() as session:
        session.add(ParticipationActionRow(
            uid=str(UID), dynamic_id=TARGET, recorded_at=1, status="failed",
            context_snapshot_json='{"result_class":"failed_local"}',
            actions_json=json.dumps([{
                "action": "favorite", "ok": False,
                "detail": "收藏状态未确认，未继续查询",
            }]),
        ))
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"


def test_legacy_unknown_blocks_shared_entry_before_remote():
    _legacy_unknown()
    client = Mock()
    result = flow.participate_activity(client, dynamic_id=TARGET, lottery_type="互动抽奖")
    assert result.status == "skipped"
    assert result.to_dict()["skip_reason"] == "favorite_unknown"
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    client.get_json.assert_not_called()
    client.post_json.assert_not_called()


def test_unknown_favorite_stops_later_repost_and_comment(monkeypatch):
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)
    context = actions.DynamicContext(
        TARGET, 123, REFERER, TARGET, 17, True, False, True, True, None, False,
    )
    client = Mock()
    client.post_json.side_effect = httpx.ReadTimeout("response lost")
    with pytest.raises(httpx.ReadTimeout):
        actions.execute_full_participation(client, dynamic_id=TARGET, context=context)
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"
    client.post_json.assert_called_once()
    client.post_form.assert_not_called()


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_auto_picker_excludes_durable_barrier_after_logs_are_gone(monkeypatch, status):
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID))
    _seed("pending")
    if status == "unknown":
        _seed("unknown")

    def row(target, lottery_type="转发抽奖"):
        return {
            "dynamic_id": target, "lottery_type": lottery_type,
            "activity_status": "未参加", "can_participate": True,
        }

    assert activity_service._pick_triple_from_rows([
        row(TARGET), row(OTHER_TARGET),
    ]) == [row(OTHER_TARGET)]
    assert activity_service._pick_triple_from_rows([row(TARGET, "预约抽奖")]) == [
        row(TARGET, "预约抽奖"),
    ]


def test_formal_risk_propagates_and_nonrisk_codes_do_not():
    client = Mock()
    client.post_json.return_value = {"code": -352, "message": "risk"}
    with pytest.raises(actions.ParticipationRiskError) as caught:
        _favorite(client)
    assert caught.value.code == -352
    assert guard.get_favorite_status(TARGET, uid=UID) == "failed"
    for code in (-315, -412):
        other = Mock()
        other.post_json.return_value = {"code": code, "message": "other"}
        with pytest.raises(RuntimeError, match="收藏结果未知"):
            _favorite(other, target=OTHER_TARGET if code == -315 else "1251573729479098374")


def test_http_429_propagates_without_becoming_business_code():
    request = httpx.Request("POST", actions.COSMO_SIMPLE_ACTION_URL)
    response = httpx.Response(429, request=request)
    client = Mock()
    client.post_json.side_effect = httpx.HTTPStatusError("rate limited", request=request, response=response)
    with pytest.raises(actions.ParticipationRiskError) as caught:
        _favorite(client)
    assert caught.value.code == 429
    assert guard.get_favorite_status(TARGET, uid=UID) == "unknown"


def test_new_target_still_can_favorite_and_invalid_target_cannot_post():
    client = Mock()
    client.post_json.return_value = {"code": 75008}
    assert _favorite(client).ok is True
    with pytest.raises(guard.FavoriteGuardStateError, match="目标动态 ID"):
        _favorite(client, target="not-a-dynamic")
    client.post_json.assert_called_once()


def test_corrupt_ledger_fails_closed():
    path: Path = guard._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    client = Mock()
    with pytest.raises(guard.FavoriteGuardStateError, match="无法读取"):
        _favorite(client)
    client.post_json.assert_not_called()
