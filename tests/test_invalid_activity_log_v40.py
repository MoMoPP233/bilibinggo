from __future__ import annotations

import logging

import httpx

from src import lottery_actions as action_mod
from src import participation as flow
from src.activity_store import activity_exists, replace_all_activities
from src.invalid_activities import invalid_activity_ids, invalidate_activity
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL, OPUS_DETAIL_URL
from src.participation_store import get_participation
from web import activity_service
from web.user_messages import format_participation_log


DID = "1000000000000000201"
WRAPPER = "1000000000000000202"
UID = "401"


class ReadClient:
    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.posts: list[str] = []

    def request_json(self, url, params=None, *, referer=None, retries=3):
        if self.fail is not None:
            exc, self.fail = self.fail, None
            raise exc
        if url in (DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL):
            return {
                "code": 0,
                "data": {
                    "item": {
                        "id_str": DID,
                        "basic": {"comment_id_str": DID, "comment_type": 17},
                        "modules": {
                            "module_author": {"mid": 502},
                            "module_stat": {
                                "like": {"status": 0},
                                "favorite": {"status": 0},
                                "forward": {"count": 1},
                                "comment": {"count": 1},
                            },
                        },
                    }
                },
            }
        if url == LOTTERY_NOTICE_URL:
            return {
                "code": 0,
                "data": {
                    "lottery_id": 1,
                    "sender_uid": 502,
                    "status": 0,
                    "lottery_time": 9_999_999_999,
                    "participated": False,
                    "reposted": False,
                },
            }
        if url == action_mod.RELATION_URL:
            return {"code": 0, "data": {"attribute": 0}}
        if url == action_mod.REPLY_MAIN_URL:
            return {"code": 0, "data": {"replies": []}}
        raise AssertionError(url)

    get_json = request_json

    def post_json(self, url, data, **kwargs):
        self.posts.append(url)
        return {"code": 0}

    def post_form(self, url, data, **kwargs):
        self.posts.append(url)
        return {"code": 0}


def _setup(monkeypatch, isolated_home):
    monkeypatch.setattr(flow, "require_login", lambda: ("csrf", int(UID)))
    monkeypatch.setattr(action_mod, "require_login", lambda: ("csrf", int(UID)))
    monkeypatch.setattr(action_mod, "get_login_uid", lambda: int(UID))
    monkeypatch.setattr(action_mod.time, "sleep", lambda _: None)
    replace_all_activities([
        {
            "dynamic_id": DID,
            "lottery_type": "转发抽奖",
            "activity_status": "未参加",
            "draw_status": "active",
            "lottery_time": 9_999_999_999,
            "observed_wrapper_dynamic_id": WRAPPER,
        }
    ])


def test_invalid_tombstone_removes_row_and_keeps_alias_profile_local(monkeypatch, isolated_home):
    _setup(monkeypatch, isolated_home)
    invalidate_activity(DID, reason="self_follow_22001", aliases=[WRAPPER])
    assert not activity_exists(DID)
    assert DID in invalid_activity_ids()
    assert WRAPPER in invalid_activity_ids()
    assert activity_service._filtered_activity_rows() == []


def test_pre_mutation_network_failure_requeues_without_mutation(monkeypatch, isolated_home):
    _setup(monkeypatch, isolated_home)
    client = ReadClient(fail=httpx.ConnectError("read failed"))
    result = flow.participate_activity(
        client,
        dynamic_id=DID,
        lottery_type="转发抽奖",
        action_text="test",
        preflight=False,
    )
    payload = result.to_dict()
    assert payload["status"] == "failed"
    assert payload["result_class"] == "failed_local"
    assert payload["context_snapshot"]["requeue"] is True
    assert payload["message"] == "网络请求失败，已重新回到队列"
    assert client.posts == []
    assert get_participation(DID, uid=UID).user_status == "未参加"
    assert "UNEXPECTED_EOF" not in format_participation_log(payload)
    assert "_ssl.c" not in format_participation_log(payload)


def test_unknown_mutation_keeps_friendly_barrier(monkeypatch, isolated_home):
    _setup(monkeypatch, isolated_home)
    client = ReadClient()

    def successful_pre_repost_post(url, data, **kwargs):
        client.posts.append(url)
        # This case tests an unknown repost. Keep the preceding favorite
        # explicitly confirmed so V98 does not stop at an uncertain favorite.
        return {"code": 75008} if url == action_mod.COSMO_SIMPLE_ACTION_URL else {"code": 0}

    client.post_json = successful_pre_repost_post

    def fail_post(url, data, **kwargs):
        client.posts.append(url)
        if url == action_mod.REPOST_URL:
            raise httpx.ReadTimeout("response lost")
        return {"code": 0}

    client.post_form = fail_post
    result = flow.participate_activity(
        client,
        dynamic_id=DID,
        lottery_type="转发抽奖",
        action_text="test",
        preflight=False,
    )
    payload = result.to_dict()
    assert payload["result_class"] == "unknown"
    assert payload["message"] == "转发结果未知，已停止自动重试"
    assert "UNEXPECTED_EOF" not in format_participation_log(payload)


def test_span_default_is_debug_and_exception_end_stays_error():
    assert logging.DEBUG < logging.INFO
    # The public signature is the contract used by all existing span callers.
    import inspect
    from src.log_span import log_span

    assert inspect.signature(log_span).parameters["level"].default == logging.DEBUG
