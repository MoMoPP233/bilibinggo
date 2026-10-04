from __future__ import annotations

import importlib
from unittest.mock import Mock

import pytest

from src import comment_confirmation as confirmation
from src import lottery_actions as actions
from src import participation as flow
from src.comment_breaker import comment_mutation_gate, trigger_comment_limit
from src.participation_log import load_action_entries_for_uid
from src.participation_store import get_participation
from web import activity_service


DYNAMIC_ID = "1251573729479098372"
UID = 42


@pytest.fixture(autouse=True)
def _local_identity(isolated_home, monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID))
    monkeypatch.setattr(flow, "require_login", lambda: ("fake-csrf", UID))
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)


def _seed(status: str, *, uid: int = UID) -> None:
    with comment_mutation_gate():
        confirmation._set_comment_status_unlocked(DYNAMIC_ID, uid=uid, status=status)


def _row(lottery_type: str = "转发抽奖") -> dict:
    return {
        "dynamic_id": DYNAMIC_ID,
        "activity_title": "synthetic lottery",
        "activity_status": "未参加",
        "lottery_type": lottery_type,
        "can_participate": True,
    }


def _network_forbidden_client() -> Mock:
    client = Mock()
    for name in ("get_json", "request_json", "get_text", "post_form", "post_json"):
        getattr(client, name).side_effect = AssertionError(f"unexpected remote operation: {name}")
    return client


def _assert_no_network(client: Mock) -> None:
    for name in ("get_json", "request_json", "get_text", "post_form", "post_json"):
        getattr(client, name).assert_not_called()


def _context(*, other_actions_complete: bool = True) -> actions.DynamicContext:
    return actions.DynamicContext(
        dynamic_id=DYNAMIC_ID,
        sender_uid=77,
        referer=f"https://www.bilibili.com/opus/{DYNAMIC_ID}",
        comment_rid=DYNAMIC_ID,
        comment_type=17,
        liked=other_actions_complete,
        followed=other_actions_complete,
        favorited=other_actions_complete,
        favorite_available=True,
        reposted=True,
        commented=False,
    )


@pytest.mark.parametrize("status", ["pending", "unknown"])
@pytest.mark.parametrize("lottery_type", ["互动抽奖", "转发抽奖"])
def test_auto_picker_excludes_durable_uncertain_comments_without_log(status, lottery_type):
    _seed(status)
    assert load_action_entries_for_uid(str(UID)) == []
    assert activity_service._pick_triple_from_rows([_row(lottery_type)]) == []


@pytest.mark.parametrize("status", ["pending", "unknown"])
def test_auto_picker_reads_current_uid_not_another_accounts_guard(monkeypatch, status):
    _seed(status, uid=UID + 1)
    row = _row()
    assert activity_service._pick_triple_from_rows([row]) == [row]
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID + 1))
    assert activity_service._pick_triple_from_rows([row]) == []


@pytest.mark.parametrize("lottery_type", ["互动抽奖", "转发抽奖"])
def test_auto_picker_keeps_confirmed_comment_eligible_for_remaining_actions(lottery_type):
    _seed("confirmed")
    row = _row(lottery_type)
    assert activity_service._pick_triple_from_rows([row]) == [row]


@pytest.mark.parametrize("status", ["pending", "unknown", "confirmed"])
def test_comment_guard_does_not_exclude_reserve_from_auto_picker(status):
    _seed(status)
    row = _row("预约抽奖")
    assert activity_service._pick_triple_from_rows([row]) == [row]


@pytest.mark.parametrize("include_comment", [False, True])
def test_corrupt_comment_ledger_fails_closed_only_for_comment_candidates(include_comment):
    path = confirmation._state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{broken ledger", encoding="utf-8")
    reserve = _row("预约抽奖")
    rows = [_row(), reserve] if include_comment else [reserve]
    assert activity_service._pick_triple_from_rows(rows) == [reserve]
    assert path.read_text(encoding="utf-8") == "{broken ledger"


@pytest.mark.parametrize("new_status", [None, "confirmed"])
def test_existing_unknown_participation_log_remains_a_barrier(new_status):
    if new_status:
        _seed(new_status)
    row = {
        **_row(),
        "last_participation": {
            "recorded_at": 1,
            "context_snapshot": {"result_class": "unknown", "unknown_action": "comment"},
        },
    }
    assert activity_service._pick_triple_from_rows([row]) == []
    # Reading legacy logs must not guess a new permanent unknown record.
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == new_status


@pytest.mark.parametrize("status", ["pending", "unknown"])
@pytest.mark.parametrize("lottery_type", ["互动抽奖", "转发抽奖"])
@pytest.mark.parametrize("provide_client", [True, False])
def test_shared_manual_auto_cli_entry_stops_locally_before_preflight(
    monkeypatch, status, lottery_type, provide_client,
):
    _seed(status)
    client = _network_forbidden_client()
    constructor = Mock(side_effect=AssertionError("local barrier must not construct client"))
    monkeypatch.setattr(flow, "BilibiliClient", constructor)
    result = flow.participate_activity(
        client if provide_client else None,
        dynamic_id=DYNAMIC_ID,
        lottery_type=lottery_type,
        action_text="changed text",
        preflight=True,
    )
    assert result.status == "skipped"
    assert result.to_dict()["skip_reason"] == f"comment_{status}"
    assert result.actions == []
    assert get_participation(DYNAMIC_ID, uid=str(UID)) is None
    constructor.assert_not_called()
    _assert_no_network(client)


@pytest.mark.parametrize("status", ["pending", "unknown", "confirmed"])
def test_shared_entry_preserves_reserve_routing(monkeypatch, status):
    _seed(status)
    client = _network_forbidden_client()
    expected = flow.ParticipateResult(
        DYNAMIC_ID, "预约抽奖", "joined", "fake reserve result", "", [], {},
    )
    reserve = Mock(return_value=expected)
    monkeypatch.setattr(flow, "participate_reserve_lottery", reserve)
    result = flow.participate_activity(
        client, dynamic_id=DYNAMIC_ID, lottery_type="预约抽奖", persist=False,
    )
    assert result is expected
    reserve.assert_called_once()
    _assert_no_network(client)


def test_confirmed_comment_still_allows_other_safe_actions_but_no_comment_post(monkeypatch):
    _seed("confirmed")
    client = _network_forbidden_client()
    other_actions = {}
    for action_name, function_name in (
        ("like", "like_dynamic"), ("follow", "follow_user"), ("favorite", "favorite_dynamic"),
    ):
        helper = Mock(return_value=actions.ActionResult(action_name, True, "fake success"))
        monkeypatch.setattr(actions, function_name, helper)
        other_actions[action_name] = helper
    result, context = actions.execute_full_participation(
        client,
        dynamic_id=DYNAMIC_ID,
        action_text="different text after previous confirmation",
        # Deliberately stale false comment snapshot: the final mutation gate
        # must still read durable confirmed, not trust this snapshot.
        context=_context(other_actions_complete=False),
    )
    assert context.commented is False
    assert [item.action for item in result] == ["like", "follow", "favorite", "repost", "comment"]
    assert all(item.ok for item in result)
    assert "跳过" in result[-1].detail
    for helper in other_actions.values():
        helper.assert_called_once()
    _assert_no_network(client)
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "confirmed"


@pytest.mark.parametrize("status", ["pending", "unknown", "confirmed"])
def test_probe_cannot_bypass_normal_comment_barrier_at_mutation_boundary(monkeypatch, status):
    _seed(status)
    trigger_comment_limit()
    client = _network_forbidden_client()
    result = actions.comment_dynamic_probe(
        client, rid=DYNAMIC_ID, comment_type=17, message="probe text", csrf="fake-csrf",
        referer=f"https://www.bilibili.com/opus/{DYNAMIC_ID}", dynamic_id=DYNAMIC_ID, uid=UID,
    )
    assert result.ok is (status == "confirmed")
    _assert_no_network(client)
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == status
    monkeypatch.setattr(activity_service, "_filtered_activity_rows", lambda: [_row()])
    assert activity_service.pick_comment_probe_candidate() is None


def test_reply_transport_id_is_not_guessed_from_dynamic_identity():
    client = Mock()
    client.post_form.return_value = {"code": 0}
    result = actions.comment_dynamic(
        client, rid="1234567", comment_type=1, message="video comment", csrf="fake-csrf",
        referer=f"https://www.bilibili.com/opus/{DYNAMIC_ID}", dynamic_id=DYNAMIC_ID, uid=UID,
    )
    assert result.ok
    assert client.post_form.call_args.args[1]["oid"] == "1234567"
    assert client.post_form.call_args.args[1]["type"] == 1
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "confirmed"
    assert confirmation.get_comment_status("1234567", uid=UID) is None


def test_confirmed_write_failure_persists_unknown_result_and_cannot_requeue(monkeypatch):
    client = _network_forbidden_client()
    client.post_form.side_effect = None
    client.post_form.return_value = {"code": 0}
    monkeypatch.setattr(flow, "fetch_dynamic_detail", lambda *_: {"id_str": DYNAMIC_ID})
    monkeypatch.setattr(flow, "build_dynamic_context", lambda *_, **__: _context())
    monkeypatch.setattr(flow, "is_following", lambda *_, **__: True)
    monkeypatch.setattr(flow, "has_comment", lambda *_, **__: False)
    real_writer = confirmation._set_comment_status_unlocked

    def fail_confirmed(dynamic_id, *, uid, status):
        if status == "confirmed":
            raise OSError("confirmed storage unavailable after successful POST")
        return real_writer(dynamic_id, uid=uid, status=status)

    monkeypatch.setattr(confirmation, "_set_comment_status_unlocked", fail_confirmed)
    monkeypatch.setattr(actions, "_set_comment_status_unlocked", fail_confirmed)
    with pytest.raises(OSError, match="confirmed storage unavailable"):
        flow.participate_activity(
            client, dynamic_id=DYNAMIC_ID, lottery_type="转发抽奖", action_text="first text",
        )
    assert client.post_form.call_count == 1
    assert client.post_form.call_args.args[0] == actions.COMMENT_URL
    assert client.post_form.call_args.kwargs["retries"] == 0
    assert confirmation.get_comment_status(DYNAMIC_ID, uid=UID) in {"pending", "unknown"}
    entry = load_action_entries_for_uid(str(UID))[-1]
    assert entry["context_snapshot"]["result_class"] == "unknown"
    assert entry["context_snapshot"]["unknown_action"] == "comment"
    assert "backoff_until" not in entry["context_snapshot"]
    assert get_participation(DYNAMIC_ID, uid=str(UID)) is None

    # Reload the ledger as after restart; discard the UI/action-log snapshot.
    # The durable barrier alone must prevent short-backoff/changed-text retry.
    importlib.reload(confirmation)
    assert activity_service._pick_triple_from_rows([_row()]) == []
    next_client = _network_forbidden_client()
    second = flow.participate_activity(
        next_client, dynamic_id=DYNAMIC_ID, lottery_type="转发抽奖",
        action_text="entirely new comment after restart", preflight=True,
    )
    assert second.to_dict()["skip_reason"] in {"comment_pending", "comment_unknown"}
    _assert_no_network(next_client)
    assert client.post_form.call_count == 1
