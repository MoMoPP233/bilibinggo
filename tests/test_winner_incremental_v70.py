from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from src.message_api import DmSessionPage
from src.message_types import DmSession, MessageEntry, MessageType
from src.winner_scan import service as winmod


def _entry(
    item_id: int,
    *,
    text: str = "恭喜中奖，请填写地址",
    seqno: int | None = None,
    kind: MessageType = MessageType.AT,
) -> MessageEntry:
    raw = {"msg_seqno": seqno} if seqno is not None else {}
    return MessageEntry(
        msg_type=kind,
        id=str(item_id),
        talker_id=7,
        talker_name="用户7",
        timestamp=item_id,
        text=text,
        raw=raw,
    )


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", 101))
    monkeypatch.setattr(winmod, "_load_nickname_cache", lambda: {})
    return tmp_path


class _Notify:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dispatch(self, event_id, *args, **kwargs):
        self.calls.append(event_id)
        return {}


def _empty_dm(monkeypatch):
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: [])


def test_mention_fetches_cursor_pages_from_latest_and_stops_at_old_cursor(profile, monkeypatch):
    state = winmod._load_state()
    state["mention"].update({"cursor_id": 99, "cursor_time": 9, "last_success_at": 1})
    winmod._save_state(state)
    calls = []
    pages = [
        ([_entry(index) for index in range(1, 21)], {"is_end": False, "id": 20, "time": 2}),
        ([_entry(index) for index in range(21, 41)], {"is_end": False, "id": 40, "time": 4}),
        ([_entry(99, text="普通历史消息")], {"is_end": False, "id": 99, "time": 9}),
    ]

    def feed(client, msg_type, **kwargs):
        if msg_type is MessageType.AT:
            calls.append(dict(kwargs))
            return pages[len(calls) - 1]
        return [], {"is_end": True, "id": 1, "time": 1}

    monkeypatch.setattr(winmod, "list_feed", feed)
    _empty_dm(monkeypatch)
    notify = _Notify()
    result = winmod.WinnerScanService(notification=notify).run(None)

    assert result["sources"]["mention"]["found"] == 40
    assert calls == [
        {},
        {"cursor_id": 20, "cursor_time": 2},
        {"cursor_id": 40, "cursor_time": 4},
    ]
    assert len(notify.calls) == 40
    saved = winmod._load_state()["mention"]
    assert saved["cursor_id"] == 99
    assert saved["cursor_time"] == 9


def test_feed_cursor_loop_is_incomplete_and_does_not_advance_source(profile, monkeypatch):
    state = winmod._load_state()
    state["mention"].update({"cursor_id": 77, "cursor_time": 7, "last_success_at": 1})
    winmod._save_state(state)
    calls = []

    def feed(client, msg_type, **kwargs):
        if msg_type is MessageType.AT:
            calls.append(dict(kwargs))
            return [_entry(len(calls))], {"is_end": False, "id": 10, "time": 1}
        return [], {"is_end": True, "id": 1, "time": 1}

    monkeypatch.setattr(winmod, "list_feed", feed)
    _empty_dm(monkeypatch)
    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["partial"] is True
    assert result["sources"]["mention"]["advance"] is False
    assert len(calls) == 2
    assert winmod._load_state()["mention"]["cursor_id"] == 77


def test_one_source_page_failure_does_not_block_other_sources(profile, monkeypatch):
    state = winmod._load_state()
    state["mention"].update({"cursor_id": 77, "cursor_time": 7, "last_success_at": 1})
    winmod._save_state(state)

    def feed(client, msg_type, **kwargs):
        if msg_type is MessageType.AT:
            if kwargs:
                raise RuntimeError("page2 network failure")
            return [_entry(1)], {"is_end": False, "id": 10, "time": 1}
        return [_entry(2)], {"is_end": True, "id": 20, "time": 2}

    monkeypatch.setattr(winmod, "list_feed", feed)
    _empty_dm(monkeypatch)
    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["mention"]["advance"] is False
    assert result["sources"]["reply"]["advance"] is True
    assert winmod._load_state()["mention"]["cursor_id"] == 77
    assert winmod._load_state()["reply"]["cursor_id"] == 20


def test_dm_message_pages_use_end_seqno_and_session_checkpoint(profile, monkeypatch):
    state = winmod._load_state()
    state["dm"].update({"last_success_at": 1, "session_checkpoints": {"7": 90}})
    winmod._save_state(state)
    calls: list[int] = []
    page1 = [_entry(seqno, seqno=seqno, kind=MessageType.DM) for seqno in range(101, 121)]
    page2 = [_entry(seqno, seqno=seqno, kind=MessageType.DM) for seqno in range(100, 89, -1)]

    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True, "id": 1, "time": 1}))
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions",
        lambda *args, **kwargs: [SimpleNamespace(talker_id=7, unread_count=25, talker_name="用户7", is_follow=True)],
    )

    def messages(client, talker_id, *, session_type=1, size=20, end_seqno=0):
        calls.append(end_seqno)
        return page1 if end_seqno == 0 else page2

    monkeypatch.setattr(winmod, "fetch_dm_messages", messages)
    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["dm"]["advance"] is True
    assert calls == [0, 101]
    assert result["sources"]["dm"]["found"] == 30
    assert winmod._load_state()["dm"]["session_checkpoints"]["7"] == 90


def _dm_session(talker_id: int, session_ts: int, *, unread_count: int = 1) -> DmSession:
    return DmSession(
        talker_id=talker_id,
        talker_name=f"用户{talker_id}",
        unread_count=unread_count,
        timestamp=session_ts,
        last_text="",
        is_follow=True,
        system_msg_type=0,
        session_type=1,
        session_ts=session_ts,
    )


def test_dm_session_list_pages_reach_30_and_use_exact_session_ts(profile, monkeypatch):
    page1 = DmSessionPage([_dm_session(index, 1000 + index) for index in range(1, 21)], has_more=True)
    page2 = DmSessionPage([_dm_session(index, 2000 + index) for index in range(21, 31)], has_more=False)
    end_ts_calls: list[int] = []
    scanned: list[int] = []

    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)

    def dm_page(client, *, size=20, end_ts=None, **kwargs):
        end_ts_calls.append(end_ts)
        assert size == 20
        return page2

    monkeypatch.setattr(winmod, "list_dm_sessions_page", dm_page)
    monkeypatch.setattr(
        winmod,
        "fetch_dm_messages",
        lambda client, talker_id, **kwargs: scanned.append(talker_id) or [],
    )

    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["dm"]["status"] == "success"
    assert result["sources"]["dm"]["sessions_collected"] == 30
    assert end_ts_calls == [page1[-1].session_ts]
    assert scanned == list(range(1, 31))
    assert "end_ts" not in winmod._load_state()["dm"]


def test_dm_session_list_page3_chain_and_cross_page_dedup(profile, monkeypatch):
    duplicate = _dm_session(2, 1002)
    page1 = DmSessionPage([_dm_session(1, 1001), duplicate], has_more=True)
    page2 = DmSessionPage([duplicate, _dm_session(3, 1003)], has_more=True)
    page3 = DmSessionPage([_dm_session(4, 1004)], has_more=False)
    calls: list[int] = []

    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions_page",
        lambda client, *, end_ts=None, **kwargs: calls.append(end_ts) or (page2 if len(calls) == 1 else page3),
    )

    sessions, pagination = winmod.WinnerScanService(notification=_Notify())._collect_dm_sessions(object())

    assert calls == [1002, 1003]
    assert [session.talker_id for session in sessions] == [1, 2, 3, 4]
    assert pagination == {"session_pages_fetched": 3, "session_list_complete": True}


def test_dm_session_list_has_more_false_does_not_request_page2(profile, monkeypatch):
    page = DmSessionPage([_dm_session(1, 1001)], has_more=False)
    page2_calls: list[int] = []
    scanned: list[int] = []
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page)
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions_page",
        lambda *args, **kwargs: page2_calls.append(1) or page,
    )
    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda client, talker_id, **kwargs: scanned.append(talker_id) or [])

    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["dm"]["status"] == "success"
    assert page2_calls == []
    assert scanned == [1]


def test_dm_session_list_reloads_page2_when_first_20_stay_unread(profile, monkeypatch):
    page1 = DmSessionPage([_dm_session(index, 1000 + index) for index in range(1, 21)], has_more=True)
    page2 = DmSessionPage([_dm_session(index, 2000 + index) for index in range(21, 31)], has_more=False)
    page2_calls: list[int] = []

    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions_page",
        lambda client, *, end_ts=None, **kwargs: page2_calls.append(end_ts) or page2,
    )
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *args, **kwargs: [])
    service = winmod.WinnerScanService(notification=_Notify())

    first = service.run(None)
    second = service.run(None)

    assert first["sources"]["dm"]["advance"] is True
    assert second["sources"]["dm"]["advance"] is True
    assert page2_calls == [page1[-1].session_ts, page1[-1].session_ts]


def test_dm_session_list_missing_session_ts_is_fail_closed_before_message_scan(profile, monkeypatch):
    page = DmSessionPage([_dm_session(1, 0)], has_more=True)
    scanned: list[int] = []
    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page)
    monkeypatch.setattr(winmod, "list_dm_sessions_page", lambda *args, **kwargs: pytest.fail("page2 must not be requested"))
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *args, **kwargs: scanned.append(1) or [])

    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["dm"]["status"] == "incomplete"
    assert result["sources"]["dm"]["stop_reason"] == "missing_next_end_ts"
    assert result["sources"]["dm"]["advance"] is False
    assert scanned == []
    assert winmod._load_state()["dm"].get("last_success_at") is None


def test_dm_session_list_repeated_end_ts_is_fail_closed(profile, monkeypatch):
    page1 = DmSessionPage([_dm_session(1, 1001)], has_more=True)
    page2 = DmSessionPage([_dm_session(2, 1001)], has_more=True)
    calls: list[int] = []
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions_page",
        lambda client, *, end_ts=None, **kwargs: calls.append(end_ts) or page2,
    )
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *args, **kwargs: pytest.fail("message scan must not start"))

    with pytest.raises(winmod.DmSessionListIncomplete, match="repeated_end_ts"):
        winmod.WinnerScanService(notification=_Notify())._collect_dm_sessions(object())
    assert calls == [1001]


def test_dm_session_list_page2_failure_scans_zero_messages_and_does_not_advance(profile, monkeypatch):
    page1 = DmSessionPage([_dm_session(1, 1001)], has_more=True)
    scanned: list[int] = []
    state = winmod._load_state()
    state["dm"].update({"last_success_at": 123, "session_checkpoints": {"1": 7}})
    winmod._save_state(state)
    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)
    monkeypatch.setattr(winmod, "list_dm_sessions_page", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("page2 down")))
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *args, **kwargs: scanned.append(1) or [])

    result = winmod.WinnerScanService(notification=_Notify()).run(None)

    assert result["sources"]["dm"]["status"] == "incomplete"
    assert result["sources"]["dm"]["stop_reason"] == "provider_failure"
    assert result["sources"]["dm"]["advance"] is False
    assert scanned == []
    assert result["sources"]["mention"]["advance"] is True
    assert result["sources"]["reply"]["advance"] is True
    saved = winmod._load_state()["dm"]
    assert saved["last_success_at"] == 123
    assert saved["session_checkpoints"] == {"1": 7}


def test_dm_session_list_risk_error_preserves_global_risk_behavior(profile, monkeypatch):
    page1 = DmSessionPage([_dm_session(1, 1001)], has_more=True)

    class RiskError(RuntimeError):
        code = -352

    monkeypatch.setattr(winmod, "list_feed", lambda *args, **kwargs: ([], {"is_end": True}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: page1)
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions_page",
        lambda *args, **kwargs: (_ for _ in ()).throw(RiskError("risk")),
    )
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *args, **kwargs: pytest.fail("message scan must not start"))

    with pytest.raises(RiskError):
        winmod.WinnerScanService(notification=_Notify()).run(None)


def test_dm_session_list_hard_cap_does_not_request_page_21(profile, monkeypatch):
    calls: list[int] = []
    first = DmSessionPage([_dm_session(1, 1001)], has_more=True)
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *args, **kwargs: first)

    def page(client, *, end_ts=None, **kwargs):
        calls.append(end_ts)
        index = len(calls) + 1
        return DmSessionPage([_dm_session(index, 1000 + index)], has_more=True)

    monkeypatch.setattr(winmod, "list_dm_sessions_page", page)

    with pytest.raises(winmod.DmSessionListIncomplete, match="hard_cap_reached"):
        winmod.WinnerScanService(notification=_Notify())._collect_dm_sessions(object())
    assert len(calls) == winmod.MAX_DM_SESSION_LIST_PAGES - 1

