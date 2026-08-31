from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import sqlite3

import httpx
import pytest

from src import lottery_actions as actions
from src import participation as flow
from src.activity_store import replace_all_activities
from src.db.engine import db_path, reset_engine_for_tests
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL, OPUS_DETAIL_URL, reset_detail_api_state
from src.participation_guard import (
    confirm_repost, get_guard, mark_repost_suspected, mark_repost_unknown, record_pending,
)
from src.participation_log import load_action_entries_for_uid
from src.participation_store import get_participation, set_participation_unlocked


DID = "1000000000000000001"
UID = "101"


class TargetClient:
    """No network: exercise the real preflight, actions and persistence together."""

    def __init__(self, *, liked=False, favorited=False, notice=None, comment_error=None, repost_error=None):
        self.liked = liked
        self.favorited = favorited
        self.notice = notice or {
            "lottery_id": 123, "sender_uid": 202, "status": 0,
            "lottery_time": 9_999_999_999, "participated": False, "reposted": False,
        }
        self.comment_error = comment_error
        self.repost_error = repost_error
        self.reads = []
        self.posts = []
        self.on_network = None

    def detail(self):
        return {
            "id_str": DID, "basic": {"comment_id_str": DID, "comment_type": 17},
            "modules": {"module_author": {"mid": 202}, "module_stat": {
                "like": {"status": self.liked}, "favorite": {"status": self.favorited},
                "forward": {"count": 1}, "comment": {"count": 1},
            }},
        }

    def request_json(self, url, params=None, *, referer=None, retries=3):
        self.reads.append((url, params, retries))
        assert retries == 0
        if self.on_network:
            self.on_network()
        if url in (DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL):
            assert params["id"] == DID
            data = {"item": self.detail()}
        elif url == LOTTERY_NOTICE_URL:
            assert params["business_id"] == DID
            data = self.notice
        elif url == actions.RELATION_URL:
            data = {"attribute": 2}
        elif url == actions.REPLY_MAIN_URL:
            assert params["oid"] == DID
            data = {"replies": []}
        else:
            raise AssertionError(f"Unexpected request (history scanning forbidden): {url}")
        return {"code": 0, "data": data}

    get_json = request_json

    def post_json(self, url, data, **kwargs):
        self.posts.append(url)
        if self.on_network:
            self.on_network()
        if url == actions.LIKE_URL:
            self.liked = True
        elif url == actions.COSMO_SIMPLE_ACTION_URL:
            self.favorited = True
        else:
            raise AssertionError(url)
        return {"code": 0}

    def post_form(self, url, data, **kwargs):
        self.posts.append(url)
        if self.on_network:
            self.on_network()
        if url == actions.REPOST_URL:
            assert get_guard(UID, DID).repost_status == "pending"
            assert kwargs["retries"] == 0
            if self.repost_error:
                raise self.repost_error
        elif url == actions.COMMENT_URL:
            # Confirmation must be committed before the comment, not at final save.
            if self.repost_error is None:
                assert get_guard(UID, DID).repost_status == "confirmed"
            if self.comment_error:
                raise self.comment_error
        else:
            raise AssertionError(url)
        return {"code": 0}


@pytest.fixture
def account(isolated_home, monkeypatch):
    monkeypatch.setattr(flow, "require_login", lambda: ("test-csrf", int(UID)))
    monkeypatch.setattr(actions, "require_login", lambda: ("test-csrf", int(UID)))
    monkeypatch.setattr(actions, "get_login_uid", lambda: int(UID))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)
    reset_detail_api_state()
    replace_all_activities([{
        "dynamic_id": DID, "lottery_type": "转发抽奖", "activity_status": "未参加",
        "draw_status": "active", "lottery_time": 9_999_999_999, "conditions": {},
    }])
    yield isolated_home
    reset_detail_api_state()


def participate(client=None, **kwargs):
    return flow.participate_activity(
        client, dynamic_id=DID, lottery_type=kwargs.pop("lottery_type", "转发抽奖"),
        action_text="test", preflight=True, **kwargs,
    )


def no_client(*args, **kwargs):
    raise AssertionError("Local dedup must not even construct a network client")


@pytest.mark.parametrize("comment_error", [
    RuntimeError("comment unavailable"),
    httpx.HTTPStatusError("comment unavailable", request=httpx.Request("POST", actions.COMMENT_URL),
                          response=httpx.Response(503)),
])
def test_confirmed_then_comment_failure_restart_and_retry_does_not_repost(account, monkeypatch, comment_error):
    client = TargetClient(comment_error=comment_error)
    first = participate(client)
    assert first.status == "failed"
    assert [a.action for a in first.actions] == ["like", "follow", "favorite", "repost"]
    assert get_guard(UID, DID).repost_status == "confirmed"
    assert get_participation(DID, uid=UID) is None
    assert load_action_entries_for_uid(UID)[-1]["actions"][-1]["action"] == "repost"
    assert sum(url == DYNAMIC_DETAIL_URL for url, _, _ in client.reads) == 1

    reset_engine_for_tests()
    second_client = TargetClient(liked=True, favorited=True)
    second = participate(second_client)
    assert second.status == "joined"
    assert second_client.posts == [actions.COMMENT_URL]
    assert get_participation(DID, uid=UID).user_status == "已参加"
    monkeypatch.setattr(flow, "BilibiliClient", no_client)
    assert participate().to_dict()["skip_reason"] == "already_joined"


@pytest.mark.parametrize("lottery_type", ["互动抽奖", "预约抽奖", "转发抽奖"])
def test_old_explicit_participation_skips_without_remote_or_guard(account, monkeypatch, lottery_type):
    set_participation_unlocked(DID, "已参加", uid=UID)
    monkeypatch.setattr(flow, "BilibiliClient", no_client)
    assert participate(lottery_type=lottery_type).to_dict()["skip_reason"] == "already_joined"
    assert get_guard(UID, DID) is None  # No backfill from old participation/actions.


@pytest.mark.parametrize("status", ["pending", "unknown", "suspected"])
def test_blocked_state_survives_restart_and_never_constructs_client(account, monkeypatch, status):
    if status == "suspected":
        mark_repost_suspected(UID, DID)
    else:
        record_pending(UID, DID)
        if status == "unknown":
            mark_repost_unknown(UID, DID)
    previous = get_guard(UID, DID)
    reset_engine_for_tests()
    monkeypatch.setattr(flow, "BilibiliClient", no_client)
    for _ in range(2):
        assert participate().to_dict()["skip_reason"] == f"repost_{status}"
    assert get_guard(UID, DID) == previous
    assert get_participation(DID, uid=UID) is None


def test_like_and_favorite_only_are_suspected_not_whole_participation(account):
    client = TargetClient(liked=True, favorited=True)
    assert participate(client).to_dict()["skip_reason"] == "repost_suspected"
    assert client.posts == []
    assert get_guard(UID, DID).repost_status == "suspected"
    assert get_participation(DID, uid=UID) is None
    assert [url for url, _, _ in client.reads] == [DYNAMIC_DETAIL_URL]


def test_pending_save_failure_forbids_repost_post(account, monkeypatch):
    def fail(*args):
        raise RuntimeError("pending save failed")

    monkeypatch.setattr(actions, "record_pending", fail)
    client = TargetClient()
    assert participate(client).status == "failed"
    assert actions.REPOST_URL not in client.posts
    assert actions.COMMENT_URL not in client.posts
    assert get_guard(UID, DID) is None


def test_network_requests_do_not_hold_a_sqlite_write_transaction(account):
    probes = []

    def probe():
        # A separate SQLite connection can obtain a write lock at every GET/POST.
        with sqlite3.connect(db_path(), timeout=0.2) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.rollback()
            probes.append(True)

    client = TargetClient()
    client.on_network = probe
    assert participate(client).status == "joined"
    assert len(probes) >= 5


@pytest.mark.parametrize("fail_unknown_write", [False, True])
def test_repost_timeout_blocks_next_whole_lifecycle_even_if_unknown_save_fails(account, monkeypatch, fail_unknown_write):
    if fail_unknown_write:
        def fail(*args):
            raise RuntimeError("unknown save failed")
        monkeypatch.setattr(actions, "mark_repost_unknown", fail)
    client = TargetClient(repost_error=httpx.ReadTimeout("response lost"))
    assert participate(client).status == "failed"
    state = "pending" if fail_unknown_write else "unknown"
    assert get_guard(UID, DID).repost_status == state
    assert client.posts.count(actions.REPOST_URL) == 1
    monkeypatch.setattr(flow, "BilibiliClient", no_client)
    assert participate().to_dict()["skip_reason"] == f"repost_{state}"
    assert get_participation(DID, uid=UID) is None


def test_official_repost_positive_is_confirmed_before_other_actions(account):
    client = TargetClient()
    client.notice["reposted"] = True
    result = participate(client, lottery_type="互动抽奖")
    assert result.status == "joined"
    assert actions.REPOST_URL not in client.posts
    assert get_guard(UID, DID).repost_status == "confirmed"
    assert sum(url == LOTTERY_NOTICE_URL for url, _, _ in client.reads) == 1


def test_official_whole_participation_saves_local_record_and_skips_actions(account, monkeypatch):
    client = TargetClient()
    client.notice.update(participated=True, reposted=True)
    assert participate(client, lottery_type="互动抽奖").to_dict()["skip_reason"] == "platform_joined"
    assert client.posts == []
    assert get_guard(UID, DID).repost_status == "confirmed"
    assert get_participation(DID, uid=UID).user_status == "已参加"
    monkeypatch.setattr(flow, "BilibiliClient", no_client)
    assert participate(lottery_type="互动抽奖").to_dict()["skip_reason"] == "already_joined"


def test_gate_covers_local_checks_all_network_steps_and_final_state_save(account, monkeypatch):
    checked = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def competitor():
            result = pool.submit(participate).result(timeout=5)
            assert result.to_dict()["skip_reason"] == "participation_busy"
            checked.append(result)

        monkeypatch.setattr(flow, "BilibiliClient", no_client)
        original_local = flow.get_participation
        original_save = flow.set_participation_unlocked

        def checked_local(*args, **kwargs):
            competitor()
            return original_local(*args, **kwargs)

        def checked_save(*args, **kwargs):
            competitor()
            return original_save(*args, **kwargs)

        monkeypatch.setattr(flow, "get_participation", checked_local)
        monkeypatch.setattr(flow, "set_participation_unlocked", checked_save)
        client = TargetClient()
        client.on_network = competitor
        assert participate(client).status == "joined"
    assert len(checked) >= 8


@pytest.mark.parametrize("field", ["liked", "favorited"])
def test_unknown_interaction_state_causes_no_posts(account, field):
    client = TargetClient(**{field: None})
    with pytest.raises(actions.ParticipationReadError, match="状态未知"):
        participate(client)
    assert client.posts == []
    assert get_guard(UID, DID) is None


def test_confirmed_snapshot_is_not_overwritten_by_stale_notice(account):
    confirm_repost(UID, DID)
    client = TargetClient(liked=True, favorited=True)
    result = participate(client, lottery_type="互动抽奖")
    assert result.status == "joined"
    assert result.context_snapshot["reposted"] is True
    assert actions.REPOST_URL not in client.posts


def test_dry_run_creates_neither_guard_nor_participation_record(account):
    client = TargetClient()
    result = participate(client, dry_run=True)
    assert result.status == "dry_run"
    assert client.posts == []
    assert get_guard(UID, DID) is None
    assert get_participation(DID, uid=UID) is None


def test_unexpected_comment_error_preserves_partial_log_and_reraises(account):
    client = TargetClient(comment_error=ValueError("unexpected program error"))
    with pytest.raises(ValueError, match="unexpected program error"):
        participate(client)
    assert get_guard(UID, DID).repost_status == "confirmed"
    assert load_action_entries_for_uid(UID)[-1]["actions"][-1]["action"] == "repost"
    assert participate(TargetClient(liked=True, favorited=True)).status == "joined"


@pytest.mark.parametrize("button_status", [1, 2])
def test_reserve_missing_notice_never_backfills_stale_platform_evidence(account, button_status):
    replace_all_activities([{
        "dynamic_id": DID, "lottery_type": "预约抽奖", "activity_status": "已参加",
        "platform_participated": True, "reserve_reserved": True,
        "draw_status": "active", "lottery_time": 9_999_999_999, "conditions": {},
    }])

    class ReserveClient(TargetClient):
        def detail(self):
            item = super().detail()
            item["modules"]["module_dynamic"] = {"additional": {"reserve": {
                "rid": int(DID), "button": {"status": button_status}, "reserve_total": 42,
            }}}
            return item

        def post_json(self, url, data, **kwargs):
            assert url == flow.RESERVE_CLICK_URL
            self.posts.append(url)
            return {"code": 0, "data": {"final_btn_status": 2}}

    client = ReserveClient()
    client.notice = {}
    result = participate(client, lottery_type="预约抽奖")
    if button_status == 2:
        assert result.to_dict()["skip_reason"] == "platform_joined"
        assert get_participation(DID, uid=UID) is None
        assert client.posts == []
    else:
        assert result.status == "joined"
        assert client.posts == [flow.RESERVE_CLICK_URL]
        assert get_participation(DID, uid=UID).user_status == "已参加"
    assert get_guard(UID, DID) is None
