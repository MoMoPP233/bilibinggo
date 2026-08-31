from __future__ import annotations

import json
from unittest.mock import Mock

import httpx
import pytest

from src import lottery_actions as actions
from src.bilibili_client import BilibiliClient
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL, OPUS_DETAIL_URL
from src.participation_guard import get_guard, mark_repost_suspected, record_pending


DYNAMIC_ID = "1000000000000000001"
UID = 42
REFERER = f"https://www.bilibili.com/opus/{DYNAMIC_ID}"


def _detail(*, liked=False, favorite=False, include_favorite=True):
    stat = {"like": {"status": liked}, "forward": {"count": 1}, "comment": {"count": 1}}
    if include_favorite:
        stat["favorite"] = {"status": favorite}
    return {
        "basic": {"comment_id_str": DYNAMIC_ID, "comment_type": 17},
        "modules": {"module_author": {"mid": 123}, "module_stat": stat},
    }


class ReadClient:
    def __init__(self, *, detail=None, opus=None, notice=None):
        self.detail = detail if detail is not None else _detail()
        self.opus = opus if opus is not None else _detail()
        self.notice = notice if notice is not None else {}
        self.calls = []

    def request_json(self, url, params=None, *, referer=None, retries=3):
        self.calls.append((url, params, retries))
        assert "feed/space" not in url
        if url == DYNAMIC_DETAIL_URL:
            return {"code": 0, "data": {"item": self.detail}}
        if url == OPUS_DETAIL_URL:
            return {"code": 0, "data": {"item": self.opus}}
        if url == LOTTERY_NOTICE_URL:
            return {"code": 0, "data": self.notice}
        if url == actions.RELATION_URL:
            return {"code": 0, "data": {"attribute": 2}}
        if url == actions.REPLY_MAIN_URL:
            return {"code": 0, "data": {"replies": []}}
        raise AssertionError(url)


def _repost(client):
    return actions.repost_dynamic(
        client, dynamic_id=DYNAMIC_ID, my_uid=UID,
        csrf="test-csrf", referer=REFERER, content="test",
    )


def _wrapped_network_error():
    error = RuntimeError("网络请求失败")
    error.__cause__ = httpx.ReadTimeout("response lost")
    return error


def test_snapshot_reuses_detail_notice_and_opus_without_history_reads():
    raw = ReadClient(detail=_detail(include_favorite=False))
    client = actions.ParticipationReadClient(raw)
    detail = client.get_json(DYNAMIC_DETAIL_URL, {"id": DYNAMIC_ID}, referer=REFERER)["data"]["item"]
    client.get_json(DYNAMIC_DETAIL_URL, {"id": DYNAMIC_ID}, referer=REFERER)
    context = actions.build_dynamic_context(
        client, dynamic_id=DYNAMIC_ID, action_text="test", detail_item=detail,
        notice=None, check_comment=False,
    )
    assert context.liked is False
    assert context.favorited is False
    assert context.reposted is None
    assert sum(url == OPUS_DETAIL_URL for url, _, _ in raw.calls) == 1
    assert sum(url == DYNAMIC_DETAIL_URL for url, _, _ in raw.calls) == 1
    assert sum(url == LOTTERY_NOTICE_URL for url, _, _ in raw.calls) == 0
    assert all(retries == 0 for _, _, retries in raw.calls)


def test_ordinary_dynamic_has_unknown_repost_state_without_scanning_history():
    raw = ReadClient()
    assert actions.is_reposted(raw, dynamic_id=DYNAMIC_ID, referer=REFERER) is None
    assert [url for url, _, _ in raw.calls] == [LOTTERY_NOTICE_URL]


def test_context_can_defer_follow_and_comment_reads_until_participation_is_needed():
    raw = ReadClient(detail=_detail(liked=True, favorite=True))
    context = actions.build_dynamic_context(
        raw, dynamic_id=DYNAMIC_ID, action_text="test", notice=None,
        check_follow=False, check_comment=False,
    )
    assert context.liked is True
    assert context.favorited is True
    assert context.followed is False
    assert context.commented is False
    assert [url for url, _, _ in raw.calls] == [DYNAMIC_DETAIL_URL]


@pytest.mark.parametrize("value,expected", [(True, True), (1, True), (False, False), (0, False),
                                            (None, None), ("true", None), ("false", None), (2, None)])
def test_repost_field_is_strictly_tristate(value, expected):
    raw = Mock()
    assert actions.is_reposted(
        raw, dynamic_id=DYNAMIC_ID, referer=REFERER, notice={"reposted": value}
    ) is expected
    raw.request_json.assert_not_called()


@pytest.mark.parametrize("status", [None, "false", "true", 2])
def test_unknown_like_status_stops_before_other_reads(status):
    raw = ReadClient(detail=_detail(liked=status))
    with pytest.raises(actions.ParticipationReadError, match="点赞状态未知"):
        actions.build_dynamic_context(raw, dynamic_id=DYNAMIC_ID, action_text="test")
    assert len(raw.calls) == 1


def test_missing_favorite_status_is_unknown_not_false():
    raw = ReadClient(detail=_detail(favorite=None), opus=_detail(favorite=None))
    with pytest.raises(actions.ParticipationReadError, match="收藏状态未知"):
        actions.build_dynamic_context(raw, dynamic_id=DYNAMIC_ID, action_text="test")


@pytest.mark.parametrize("code", [-352, -509, -799, -9999])
def test_read_error_stops_all_followup_requests_and_never_retries(code):
    raw = Mock()
    raw.request_json.return_value = {"code": code, "message": "blocked"}
    client = actions.ParticipationReadClient(raw)
    for url in (DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL, LOTTERY_NOTICE_URL):
        with pytest.raises(actions.ParticipationReadError, match=str(code)):
            client.get_json(url, {"id": DYNAMIC_ID}, retries=99)
    raw.request_json.assert_called_once()
    assert raw.request_json.call_args.kwargs["retries"] == 0


def test_dynamic_api_swallowed_error_cannot_trigger_fallback_network_request():
    raw = Mock()
    raw.request_json.return_value = {"code": -352}
    with pytest.raises(actions.ParticipationReadError, match="-352"):
        actions.build_dynamic_context(raw, dynamic_id=DYNAMIC_ID, action_text="test")
    raw.request_json.assert_called_once()


def test_follow_read_failure_cannot_fall_through_to_follow_or_reserve_post():
    raw = Mock()
    raw.request_json.return_value = {"code": -352}
    client = actions.ParticipationReadClient(raw)
    assert actions.is_following(client, uid=123, referer=REFERER) is False
    with pytest.raises(actions.ParticipationReadError, match="-352"):
        actions.follow_user(client, uid=123, csrf="test", referer=REFERER)
    with pytest.raises(actions.ParticipationReadError, match="-352"):
        client.post_json("https://api.bilibili.com/x/dynamic/feed/reserve/click", {})
    raw.post_form.assert_not_called()
    raw.post_json.assert_not_called()


def test_unexpected_read_error_is_reraised_without_fallback_network_request():
    raw = Mock()
    raw.request_json.side_effect = ValueError("unexpected")
    with pytest.raises(ValueError, match="unexpected"):
        actions.build_dynamic_context(raw, dynamic_id=DYNAMIC_ID, action_text="test")
    raw.request_json.assert_called_once()


def test_repost_pending_is_committed_before_post_and_success_is_immediate(isolated_home):
    raw = Mock()

    def post(*args, **kwargs):
        assert get_guard(UID, DYNAMIC_ID).repost_status == "pending"
        assert kwargs["retries"] == 0
        return {"code": 0}

    raw.post_form.side_effect = post
    result = _repost(raw)
    assert result.ok is True
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    assert _repost(raw).ok is True
    raw.post_form.assert_called_once()


@pytest.mark.parametrize("status", ["pending", "suspected", "unknown"])
def test_existing_guard_blocks_repost_post(isolated_home, status):
    if status == "suspected":
        mark_repost_suspected(UID, DYNAMIC_ID)
    else:
        record_pending(UID, DYNAMIC_ID)
        if status == "unknown":
            actions.mark_repost_unknown(UID, DYNAMIC_ID)
    raw = Mock()
    assert _repost(raw).ok is False
    raw.post_form.assert_not_called()
    assert get_guard(UID, DYNAMIC_ID).repost_status == status


@pytest.mark.parametrize("payload", [
    {"code": -1, "message": "活动已经关闭"},
    {"code": -2, "message": "重复请求"},
    {"code": False}, {"code": "0"}, {}, [], None,
])
def test_non_success_or_invalid_response_never_guesses_repost_success(isolated_home, payload):
    raw = Mock()
    raw.post_form.return_value = payload
    assert _repost(raw).ok is False
    assert get_guard(UID, DYNAMIC_ID).repost_status == "unknown"
    assert _repost(raw).ok is False
    raw.post_form.assert_called_once()


@pytest.mark.parametrize("error", [
    httpx.ReadTimeout("response lost"), _wrapped_network_error(),
    json.JSONDecodeError("invalid", "", 0),
])
def test_ambiguous_post_failure_becomes_unknown_without_retry(isolated_home, error):
    raw = Mock()
    raw.post_form.side_effect = error
    assert _repost(raw).ok is False
    assert get_guard(UID, DYNAMIC_ID).repost_status == "unknown"
    assert _repost(raw).ok is False
    raw.post_form.assert_called_once()


def test_pending_write_failure_prevents_any_post(isolated_home, monkeypatch):
    def fail(*args):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(actions, "record_pending", fail)
    raw = Mock()
    with pytest.raises(RuntimeError, match="database unavailable"):
        _repost(raw)
    raw.post_form.assert_not_called()


def test_concurrent_pending_insert_loss_prevents_post(isolated_home, monkeypatch):
    def lose_claim(*args):
        raise actions.ParticipationGuardBlocked(str(UID), DYNAMIC_ID, "pending")

    monkeypatch.setattr(actions, "record_pending", lose_claim)
    raw = Mock()
    assert _repost(raw).ok is False
    raw.post_form.assert_not_called()


def test_unknown_state_write_failure_keeps_pending_protection(isolated_home, monkeypatch):
    def fail(*args):
        raise RuntimeError("unknown write failed")

    monkeypatch.setattr(actions, "mark_repost_unknown", fail)
    raw = Mock()
    raw.post_form.side_effect = httpx.ReadTimeout("response lost")
    with pytest.raises(RuntimeError, match="unknown write failed"):
        _repost(raw)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "pending"
    assert _repost(raw).ok is False
    raw.post_form.assert_called_once()


@pytest.mark.parametrize("error", [ValueError("unexpected"), RuntimeError("unexpected")])
def test_unknown_programming_failure_marks_unknown_and_raises(isolated_home, error):
    raw = Mock()
    raw.post_form.side_effect = error
    with pytest.raises(type(error), match="unexpected"):
        _repost(raw)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "unknown"


@pytest.mark.parametrize("payload", [[], None])
def test_real_client_invalid_json_shape_marks_unknown_without_retry(isolated_home, monkeypatch, payload):
    response = httpx.Response(
        200, content=json.dumps(payload), headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", actions.REPOST_URL),
    )
    with BilibiliClient(warmup=False) as client:
        http_post = Mock(return_value=response)
        monkeypatch.setattr(client, "_http_post", http_post)
        with pytest.raises(AttributeError):
            _repost(client)
        assert get_guard(UID, DYNAMIC_ID).repost_status == "unknown"
        assert _repost(client).ok is False
        http_post.assert_called_once()


def test_process_exit_during_post_keeps_pending(isolated_home):
    raw = Mock()
    raw.post_form.side_effect = SystemExit(1)
    with pytest.raises(SystemExit):
        _repost(raw)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "pending"


def test_confirmation_write_failure_leaves_pending(isolated_home, monkeypatch):
    def fail(*args):
        raise RuntimeError("confirmation failed")

    monkeypatch.setattr(actions, "confirm_repost", fail)
    raw = Mock()
    raw.post_form.return_value = {"code": 0}
    with pytest.raises(RuntimeError, match="confirmation failed"):
        _repost(raw)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "pending"
    assert _repost(raw).ok is False
    raw.post_form.assert_called_once()


def test_favorite_uses_snapshot_before_post_and_one_fresh_after_read():
    raw = Mock()
    raw.post_json.return_value = {"code": 0}
    raw.get_json.return_value = {"code": 0, "data": {"item": _detail(favorite=True)}}
    wrapper = actions.ParticipationReadClient(raw)
    result = actions.favorite_dynamic(
        wrapper, dynamic_id=DYNAMIC_ID, csrf="test", referer=REFERER, known_status=False
    )
    assert result.ok is True
    raw.get_json.assert_called_once()
    raw.request_json.assert_not_called()
    assert raw.get_json.call_args.kwargs["retries"] == 0


def test_unknown_favorite_state_never_posts():
    raw = Mock()
    with pytest.raises(actions.ParticipationReadError, match="收藏状态未知"):
        actions.favorite_dynamic(
            raw, dynamic_id=DYNAMIC_ID, csrf="test", referer=REFERER, known_status=None
        )
    raw.post_json.assert_not_called()


def test_comment_failure_preserves_repost_confirmation_and_partial_actions(isolated_home, monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("test", UID))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)
    context = actions.DynamicContext(
        DYNAMIC_ID, 123, REFERER, DYNAMIC_ID, 17, True, True, True, True, None, False
    )
    raw = Mock()
    raw.post_form.side_effect = [{"code": 0}, RuntimeError("comment unavailable")]
    partial = []
    with pytest.raises(RuntimeError, match="comment unavailable"):
        actions.execute_full_participation(
            raw, dynamic_id=DYNAMIC_ID, context=context, on_action=partial.append
        )
    assert [item.action for item in partial] == ["like", "follow", "favorite", "repost"]
    assert all(item.ok for item in partial)
    assert get_guard(UID, DYNAMIC_ID).repost_status == "confirmed"
    raw.request_json.assert_not_called()
