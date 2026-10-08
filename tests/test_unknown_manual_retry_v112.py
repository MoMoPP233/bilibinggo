from __future__ import annotations

import json
import importlib
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from src import comment_confirmation, favorite_confirmation, lottery_actions, manual_participation_retry
from src import manual_unknown_retry, participation_guard
from src import participation as participation_flow
from src.data_paths import get_runtime_profile_id
from src.db.engine import reset_engine_for_tests
from src.db.models import ParticipationActionRow
from src.db.session import session_scope
from web import activity_service
from web.job_runner import JobRunner
from web.schemas.jobs import JobRequest

UID = 42
DID = "1251573729479098372"


@pytest.fixture(autouse=True)
def _local_only(isolated_home, monkeypatch):
    identity = lambda: ("fake-csrf", UID)
    monkeypatch.setattr(manual_participation_retry, "require_login", identity)
    monkeypatch.setattr(lottery_actions, "require_login", identity)
    monkeypatch.setattr(manual_participation_retry, "is_risk_paused", lambda: False)
    monkeypatch.setattr(manual_participation_retry, "fetch_dynamic_detail", lambda _client, _id: {
        "id_str": DID, "basic": {"comment_id_str": DID, "comment_type": 17},
    })


def _seed(action: str, status: str = "unknown") -> None:
    if action == "comment":
        with lottery_actions.comment_mutation_gate():
            comment_confirmation._set_comment_status_unlocked(DID, uid=UID, status="pending")
            if status == "unknown":
                comment_confirmation._set_comment_status_unlocked(DID, uid=UID, status="unknown")
    elif action == "favorite":
        with favorite_confirmation.favorite_mutation_gate():
            favorite_confirmation._set_favorite_status_unlocked(DID, uid=UID, status="pending")
            if status == "unknown":
                favorite_confirmation._set_favorite_status_unlocked(DID, uid=UID, status="unknown")
    else:
        participation_guard.record_pending(UID, DID)
        if status == "unknown":
            participation_guard.mark_repost_unknown(UID, DID)


def _run(action: str, client: Mock, *, generation: int = 0):
    return manual_participation_retry.retry_unknown_action(
        DID, action, expected_generation=generation,
        expected_profile_id=get_runtime_profile_id(),
        retry_text="" if action == "favorite" else "人工确认后重试",
        client=client,
    )


@pytest.mark.parametrize("action", ["comment", "favorite", "repost"])
def test_each_unknown_retries_only_its_own_action(action, monkeypatch):
    _seed(action)
    client = Mock()
    client.post_form.return_value = {"code": 0}
    client.post_json.return_value = {"code": 75008}
    if action == "favorite":
        monkeypatch.setattr(lottery_actions, "is_favorited", lambda *_args, **_kwargs: False)
    result = _run(action, client)
    assert result["status"] == "confirmed"
    assert result["action"] == action
    assert client.post_form.call_count == (0 if action == "favorite" else 1)
    assert client.post_json.call_count == (1 if action == "favorite" else 0)
    posts = client.post_form.call_args_list + client.post_json.call_args_list
    assert posts[0].kwargs["retries"] == 0
    assert manual_unknown_retry.generation(UID, DID, action) == 1
    with pytest.raises(RuntimeError, match="未知状态"):
        _run(action, client, generation=1)


@pytest.mark.parametrize("action", ["comment", "favorite", "repost"])
def test_timeout_keeps_original_guard_and_requires_new_confirmation(action, monkeypatch):
    _seed(action)
    client = Mock()
    client.post_form.side_effect = httpx.ReadTimeout("lost")
    client.post_json.side_effect = httpx.ReadTimeout("lost")
    if action == "favorite":
        monkeypatch.setattr(lottery_actions, "is_favorited", lambda *_args, **_kwargs: False)
    if action == "repost":
        assert _run(action, client)["status"] == "unknown"
    else:
        with pytest.raises(httpx.ReadTimeout):
            _run(action, client)
    assert manual_unknown_retry.generation(UID, DID, action) == 1
    assert manual_participation_retry.inspect_unknown_actions(DID)["actions"]
    with pytest.raises(manual_unknown_retry.ManualRetryStateError):
        _run(action, client, generation=0)
    assert client.post_form.call_count + client.post_json.call_count == 1


def test_stale_confirmation_and_storage_failure_send_zero_posts(monkeypatch):
    _seed("comment")
    client = Mock()
    with pytest.raises(manual_unknown_retry.ManualRetryStateError):
        _run("comment", client, generation=1)
    client.post_form.assert_not_called()
    monkeypatch.setattr(manual_unknown_retry, "_write_unlocked", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        _run("comment", client)
    client.post_form.assert_not_called()


def test_risk_pause_and_profile_mismatch_send_zero_posts(monkeypatch):
    _seed("repost")
    client = Mock()
    monkeypatch.setattr(manual_participation_retry, "is_risk_paused", lambda: True)
    with pytest.raises(RuntimeError, match="风控暂停"):
        _run("repost", client)
    with pytest.raises(RuntimeError, match="Profile"):
        manual_participation_retry.retry_unknown_action(
            DID, "repost", expected_generation=0, expected_profile_id="account-999", client=client,
        )
    client.post_form.assert_not_called()
    assert manual_unknown_retry.generation(UID, DID, "repost") == 0


def test_old_unknown_is_visible_without_sidecar_and_audit_survives_restart():
    _seed("repost")
    view = manual_participation_retry.inspect_unknown_actions(DID)
    assert view["actions"] == [{"action": "repost", "label": "转发", "status": "unknown", "generation": 0}]
    with participation_guard.participation_gate(str(UID), DID):
        attempt_id = manual_unknown_retry.begin_attempt(
            UID, DID, "repost", expected_generation=0, prior_status="unknown",
        )
    raw = json.loads(manual_unknown_retry._path().read_text(encoding="utf-8"))
    assert raw["entries"][f"{UID}:{DID}:repost"][0]["status"] == "pending"
    assert raw["entries"][f"{UID}:{DID}:repost"][0]["prior_status"] == "unknown"
    assert attempt_id
    assert manual_unknown_retry.generation(UID, DID, "repost") == 1
    assert participation_guard.get_guard(UID, DID).repost_status == "unknown"


def test_crash_after_pending_keeps_both_durable_barriers():
    _seed("repost")
    client = Mock()
    client.post_form.side_effect = SystemExit("simulated process death")
    with pytest.raises(SystemExit):
        _run("repost", client)
    reset_engine_for_tests()
    assert participation_guard.get_guard(UID, DID).repost_status == "pending"
    assert manual_unknown_retry.generation(UID, DID, "repost") == 1
    raw = json.loads(manual_unknown_retry._path().read_text(encoding="utf-8"))
    assert raw["entries"][f"{UID}:{DID}:repost"][-1]["status"] == "pending"


def test_two_manual_requests_with_same_confirmation_send_one_post():
    _seed("repost")
    entered = threading.Event()
    release = threading.Event()
    client = Mock()

    def post(*_args, **_kwargs):
        entered.set()
        assert release.wait(3)
        return {"code": 0}

    client.post_form.side_effect = post
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(_run, "repost", client)
        assert entered.wait(3)
        second = pool.submit(_run, "repost", client)
        with pytest.raises(participation_guard.ParticipationBusyError):
            second.result(timeout=3)
        release.set()
        assert first.result(timeout=3)["status"] == "confirmed"
    assert client.post_form.call_count == 1


def test_rejected_manual_comment_retains_original_unknown():
    _seed("comment")
    client = Mock()
    client.post_form.return_value = {"code": 12078, "message": "rejected"}
    result = _run("comment", client)
    assert result["status"] == "unknown"
    assert comment_confirmation.get_comment_status(DID, uid=UID) == "unknown"
    assert manual_unknown_retry.generation(UID, DID, "comment") == 1


def test_confirmed_other_action_is_never_posted(monkeypatch):
    _seed("comment")
    with favorite_confirmation.favorite_mutation_gate():
        favorite_confirmation._set_favorite_status_unlocked(DID, uid=UID, status="confirmed")
    participation_guard.confirm_repost(UID, DID)
    client = Mock()
    client.post_form.return_value = {"code": 0}
    assert _run("comment", client)["status"] == "confirmed"
    assert client.post_form.call_count == 1
    assert client.post_form.call_args.args[0] == lottery_actions.COMMENT_URL
    client.post_json.assert_not_called()


def test_other_uid_unknown_does_not_authorize_current_uid():
    with lottery_actions.comment_mutation_gate():
        comment_confirmation._set_comment_status_unlocked(DID, uid=99, status="pending")
        comment_confirmation._set_comment_status_unlocked(DID, uid=99, status="unknown")
    client = Mock()
    with pytest.raises(RuntimeError, match="未知状态"):
        _run("comment", client)
    client.post_form.assert_not_called()


def test_profile_local_audit_paths_are_separate(isolated_home, monkeypatch, tmp_path):
    _seed("repost")
    with participation_guard.participation_gate(str(UID), DID):
        manual_unknown_retry.begin_attempt(
            UID, DID, "repost", expected_generation=0, prior_status="unknown",
        )
    first = manual_unknown_retry._path()
    assert manual_unknown_retry.generation(UID, DID, "repost") == 1
    monkeypatch.setattr(manual_unknown_retry, "_path", lambda: tmp_path / "other-profile" / "manual_unknown_retries.json")
    assert manual_unknown_retry.generation(UID, DID, "repost") == 0
    assert first.exists()


@pytest.mark.parametrize("action", ["comment", "favorite", "repost"])
def test_automatic_or_plain_participation_still_blocks_old_unknown(action, monkeypatch):
    _seed(action)
    monkeypatch.setattr(participation_flow, "require_login", lambda: ("fake-csrf", UID))
    client = Mock()
    result = participation_flow.participate_activity(
        client, dynamic_id=DID, lottery_type="转发抽奖", preflight=True,
    )
    assert result.status == "skipped"
    client.post_form.assert_not_called()
    client.post_json.assert_not_called()
    client.get_json.assert_not_called()


def test_manual_formal_risk_keeps_unknown_and_propagates():
    _seed("comment")
    client = Mock()
    client.post_form.return_value = {"code": -352, "message": "risk"}
    with pytest.raises(lottery_actions.ParticipationRiskError):
        _run("comment", client)
    assert comment_confirmation.get_comment_status(DID, uid=UID) == "unknown"
    raw = json.loads(manual_unknown_retry._path().read_text(encoding="utf-8"))
    assert raw["entries"][f"{UID}:{DID}:comment"][-1]["status"] == "unknown"


def test_favorite_remote_proof_confirms_without_second_post(monkeypatch):
    _seed("favorite")
    monkeypatch.setattr(lottery_actions, "is_favorited", lambda *_args, **_kwargs: True)
    client = Mock()
    result = _run("favorite", client)
    assert result["status"] == "confirmed"
    client.post_json.assert_not_called()


def test_new_manual_confirmation_is_required_after_another_unknown():
    _seed("repost")
    client = Mock()
    client.post_form.side_effect = httpx.ReadTimeout("lost")
    assert _run("repost", client)["status"] == "unknown"
    with pytest.raises(manual_unknown_retry.ManualRetryStateError):
        _run("repost", client, generation=0)
    client.post_form.side_effect = None
    client.post_form.return_value = {"code": 0}
    assert _run("repost", client, generation=1)["status"] == "confirmed"
    assert client.post_form.call_count == 2


def test_invalid_comment_target_never_opens_a_manual_attempt(monkeypatch):
    _seed("comment")
    monkeypatch.setattr(manual_participation_retry, "fetch_dynamic_detail", lambda _client, _id: {
        "id_str": DID, "basic": {"comment_id_str": "invalid", "comment_type": 17},
    })
    client = Mock()
    with pytest.raises(RuntimeError, match="评论目标身份无效"):
        _run("comment", client)
    assert manual_unknown_retry.generation(UID, DID, "comment") == 0
    client.post_form.assert_not_called()


def test_uncertain_repost_is_visible_only_for_manual_retry_not_triple(monkeypatch):
    _seed("repost")
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID))
    monkeypatch.setattr(activity_service, "_load_activities_payload", lambda: {"activities": [{
        "dynamic_id": DID, "activity_title": "待确认抽奖", "lottery_type": "转发抽奖",
        "draw_status": "active", "activity_status": "未参加", "status_classified": True,
    }]})
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    rows = activity_service._filtered_activity_rows()
    assert len(rows) == 1
    assert rows[0]["manual_retry_available"] is True
    assert rows[0]["can_participate"] is False
    assert activity_service._pick_triple_from_rows(rows) == []


def test_suspected_repost_remains_hidden_from_manual_retry(monkeypatch):
    participation_guard.mark_repost_suspected(UID, DID)
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID))
    monkeypatch.setattr(activity_service, "_load_activities_payload", lambda: {"activities": [{
        "dynamic_id": DID, "activity_title": "疑似参与抽奖", "lottery_type": "转发抽奖",
        "draw_status": "active", "activity_status": "未参加", "status_classified": True,
    }]})
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    assert activity_service._filtered_activity_rows() == []


def test_scheduler_source_cannot_start_manual_retry_job():
    runner = JobRunner()
    assert runner.try_start("manual_retry_unknown", {"dynamic_id": DID}, source="auto") is None
    assert runner.try_start(
        "participate", {"dynamic_id": DID, "manual_unknown_retry": True}, source="auto",
    ) is None
    assert runner.get_status().state == "idle"


def test_ui_participate_job_attests_profile_and_uid_without_remote_calls(monkeypatch):
    web_app = importlib.import_module("web.app")
    monkeypatch.setattr(web_app, "get_account_profile", lambda: {"logged_in": True, "mid": UID})
    monkeypatch.setattr(web_app, "require_login", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(web_app, "require_llm_ready", lambda: None)
    submitted = []
    monkeypatch.setattr(web_app.runner, "try_start", lambda action, params, *, source: (
        submitted.append((action, dict(params), source)) or 1
    ))
    monkeypatch.setattr(web_app.runner, "get_status", lambda: SimpleNamespace(
        to_dict=lambda: {"id": 1, "state": "running", "action": "participate"},
    ))
    response = web_app.api_start_job(JobRequest(
        action="participate", params={"dynamic_id": DID, "manual_unknown_retry": True},
    ))
    assert response["job"]["id"] == 1
    assert submitted == [("participate", {
        "dynamic_id": DID, "manual_unknown_retry": True,
        "runtime_profile_id": get_runtime_profile_id(), "uid": str(UID),
    }, "ui")]


def test_read_only_inspection_and_cancel_do_not_create_audit_state():
    _seed("repost")
    assert not manual_unknown_retry._path().exists()
    assert manual_participation_retry.inspect_unknown_actions(DID)["actions"]
    assert not manual_unknown_retry._path().exists()


def test_retained_legacy_favorite_unknown_is_visible_without_migration_write():
    with session_scope() as session:
        session.add(ParticipationActionRow(
            uid=str(UID), dynamic_id=DID, recorded_at=1, status="failed",
            context_snapshot_json=json.dumps({"result_class": "unknown", "unknown_action": "favorite"}),
        ))
    assert not favorite_confirmation._state_path().exists()
    actions = manual_participation_retry.inspect_unknown_actions(DID)["actions"]
    assert any(item["action"] == "favorite" and item["generation"] == 0 for item in actions)
    assert not favorite_confirmation._state_path().exists()


def _full_pipeline_fixture(monkeypatch, *, favorited: bool = False, reposted: bool = False,
                           commented: bool = False):
    monkeypatch.setattr(participation_flow, "require_login", lambda: ("fake-csrf", UID))
    monkeypatch.setattr(lottery_actions.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(participation_flow, "ensure_activity_participatable", lambda *_a, **_k: None)
    monkeypatch.setattr(participation_flow, "fetch_dynamic_detail", lambda *_a: {"id_str": DID})
    monkeypatch.setattr(participation_flow, "build_dynamic_context", lambda *_a, **_k: lottery_actions.DynamicContext(
        dynamic_id=DID, sender_uid=77, referer=f"https://www.bilibili.com/opus/{DID}",
        comment_rid=DID, comment_type=17, liked=True, followed=True,
        favorited=favorited, favorite_available=True, reposted=reposted, commented=commented,
    ))
    monkeypatch.setattr(participation_flow, "is_following", lambda *_a, **_k: True)
    monkeypatch.setattr(participation_flow, "has_comment", lambda *_a, **_k: commented)
    generated = Mock(return_value=("自动生成的参与文案", {"participate_text_source": "fixture"}))
    monkeypatch.setattr(participation_flow, "_resolve_action_text", generated)
    client = Mock()
    client.post_json.return_value = {"code": 75008}
    client.post_form.return_value = {"code": 0}
    return client, generated


def test_favorite_unknown_manual_full_pipeline_continues_to_unattempted_repost(monkeypatch):
    _seed("favorite")
    client, generated = _full_pipeline_fixture(monkeypatch)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    assert [action.action for action in result.actions] == ["like", "follow", "favorite", "repost", "comment"]
    assert client.post_json.call_count == 1
    assert [call.args[0] for call in client.post_form.call_args_list] == [
        lottery_actions.REPOST_URL, lottery_actions.COMMENT_URL,
    ]
    assert client.post_form.call_args_list[0].args[1]["content"] == "自动生成的参与文案"
    assert client.post_form.call_args_list[1].args[1]["message"] == "自动生成的参与文案"
    assert generated.call_args.kwargs["action_text"] is None
    assert favorite_confirmation.get_favorite_status(DID, uid=UID) == "confirmed"
    assert participation_guard.get_guard(UID, DID).repost_status == "confirmed"
    assert comment_confirmation.get_comment_status(DID, uid=UID) == "confirmed"
    assert manual_unknown_retry.generation(UID, DID, "favorite") == 1
    assert manual_unknown_retry.generation(UID, DID, "repost") == 0


def test_retained_old_favorite_unknown_without_sidecar_recovers_full_pipeline(monkeypatch):
    with session_scope() as session:
        session.add(ParticipationActionRow(
            uid=str(UID), dynamic_id=DID, recorded_at=1, status="failed",
            context_snapshot_json=json.dumps({"result_class": "unknown", "unknown_action": "favorite"}),
        ))
    assert not favorite_confirmation._state_path().exists()
    client, _ = _full_pipeline_fixture(monkeypatch)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    assert [call.args[0] for call in client.post_form.call_args_list] == [
        lottery_actions.REPOST_URL, lottery_actions.COMMENT_URL,
    ]
    assert favorite_confirmation.get_favorite_status(DID, uid=UID) == "confirmed"
    assert manual_unknown_retry.generation(UID, DID, "favorite") == 1


def test_old_comment_unknown_without_durable_identity_guard_fails_closed(monkeypatch):
    with session_scope() as session:
        session.add(ParticipationActionRow(
            uid=str(UID), dynamic_id=DID, recorded_at=1, status="failed",
            context_snapshot_json=json.dumps({"result_class": "unknown", "unknown_action": "comment"}),
        ))
    client, _ = _full_pipeline_fixture(monkeypatch)
    with pytest.raises(RuntimeError, match="缺少可验证的持久防重状态"):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
        )
    client.post_json.assert_not_called()
    client.post_form.assert_not_called()
    client.get_json.assert_not_called()


def test_repost_unknown_uses_generated_text_then_continues_to_comment(monkeypatch):
    _seed("repost")
    client, generated = _full_pipeline_fixture(monkeypatch, favorited=True)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    assert [call.args[0] for call in client.post_form.call_args_list] == [
        lottery_actions.REPOST_URL, lottery_actions.COMMENT_URL,
    ]
    assert client.post_form.call_args_list[0].args[1]["content"] == "自动生成的参与文案"
    client.post_json.assert_not_called()
    assert generated.call_args.kwargs["action_text"] is None
    assert manual_unknown_retry.generation(UID, DID, "repost") == 1


def test_comment_unknown_only_retries_comment_after_confirmed_repost(monkeypatch):
    _seed("comment")
    participation_guard.confirm_repost(UID, DID)
    client, generated = _full_pipeline_fixture(monkeypatch, favorited=True, reposted=True)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    assert [call.args[0] for call in client.post_form.call_args_list] == [lottery_actions.COMMENT_URL]
    assert client.post_form.call_args.args[1]["message"] == "自动生成的参与文案"
    client.post_json.assert_not_called()
    assert generated.call_args.kwargs["action_text"] is None
    assert manual_unknown_retry.generation(UID, DID, "comment") == 1


def test_manual_full_pipeline_rejects_uid_or_profile_mismatch_before_network(monkeypatch):
    _seed("favorite")
    client, _ = _full_pipeline_fixture(monkeypatch)
    with pytest.raises(RuntimeError, match="账号"):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid="99", client=client,
        )
    with pytest.raises(RuntimeError, match="Profile"):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id="account-999", expected_uid=str(UID), client=client,
        )
    client.post_json.assert_not_called()
    client.post_form.assert_not_called()


def test_manual_retry_skips_previously_confirmed_repost_and_comment(monkeypatch):
    _seed("favorite")
    participation_guard.confirm_repost(UID, DID)
    with lottery_actions.comment_mutation_gate():
        comment_confirmation._set_comment_status_unlocked(DID, uid=UID, status="confirmed")
    client, _ = _full_pipeline_fixture(monkeypatch, reposted=True, commented=True)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    client.post_form.assert_not_called()
    assert client.post_json.call_count == 1


def test_manual_retry_second_unknown_stops_before_later_actions(monkeypatch):
    _seed("favorite")
    client, _ = _full_pipeline_fixture(monkeypatch)
    client.post_json.side_effect = httpx.ReadTimeout("lost")
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "failed"
    assert result.context_snapshot["unknown_action"] == "favorite"
    client.post_form.assert_not_called()
    assert favorite_confirmation.get_favorite_status(DID, uid=UID) == "unknown"
    assert manual_unknown_retry.generation(UID, DID, "favorite") == 1


def test_remote_favorite_proof_resolves_unknown_without_favorite_post_and_continues(monkeypatch):
    _seed("favorite")
    client, _ = _full_pipeline_fixture(monkeypatch, favorited=True)
    result = manual_participation_retry.retry_unknown_participation(
        DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
    )
    assert result.status == "joined"
    client.post_json.assert_not_called()
    assert [call.args[0] for call in client.post_form.call_args_list] == [
        lottery_actions.REPOST_URL, lottery_actions.COMMENT_URL,
    ]
    assert favorite_confirmation.get_favorite_status(DID, uid=UID) == "confirmed"


def test_full_pipeline_process_death_after_favorite_send_keeps_pending(monkeypatch):
    _seed("favorite")
    client, _ = _full_pipeline_fixture(monkeypatch)
    client.post_json.side_effect = SystemExit("simulated process death")
    with pytest.raises(SystemExit):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
        )
    reset_engine_for_tests()
    assert favorite_confirmation.get_favorite_status(DID, uid=UID) == "pending"
    raw = json.loads(manual_unknown_retry._path().read_text(encoding="utf-8"))
    assert raw["entries"][f"{UID}:{DID}:favorite"][-1]["status"] == "pending"
    client.post_form.assert_not_called()


def test_manual_full_pipeline_risk_pause_and_audit_write_failure_send_no_posts(monkeypatch):
    _seed("favorite")
    client, _ = _full_pipeline_fixture(monkeypatch)
    monkeypatch.setattr(manual_participation_retry, "is_risk_paused", lambda: True)
    with pytest.raises(RuntimeError, match="风控暂停"):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
        )
    client.post_json.assert_not_called()
    monkeypatch.setattr(manual_participation_retry, "is_risk_paused", lambda: False)
    monkeypatch.setattr(manual_unknown_retry, "_write_unlocked", Mock(side_effect=OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        manual_participation_retry.retry_unknown_participation(
            DID, "转发抽奖", expected_profile_id=get_runtime_profile_id(), expected_uid=str(UID), client=client,
        )
    client.post_json.assert_not_called()
    client.post_form.assert_not_called()


def test_uncertain_favorite_row_is_manual_only_not_triple(monkeypatch):
    _seed("favorite")
    monkeypatch.setattr(activity_service, "participation_uid", lambda: str(UID))
    monkeypatch.setattr(activity_service, "_load_activities_payload", lambda: {"activities": [{
        "dynamic_id": DID, "activity_title": "收藏待确认抽奖", "lottery_type": "转发抽奖",
        "draw_status": "active", "activity_status": "未参加", "status_classified": True,
    }]})
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    rows = activity_service._filtered_activity_rows()
    assert len(rows) == 1
    assert rows[0]["manual_retry_available"] is True
    assert rows[0]["can_participate"] is False
    assert activity_service._pick_triple_from_rows(rows) == []
