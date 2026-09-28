"""V53 长关注自动回复与中奖关键词碰撞回归。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.message_types import MessageEntry, MessageType, normalize_dm_message
from src.winner_scan import service as winmod

UID = 101
TALKER_ID = 204408558
REAL_AUTO_REPLY = (
    "感谢您的关注，我会一直坚持帮大家找卡组并测试，也会讲解炉石历史和故事 [给心心]\n\n"
    "共重号【炉石港湾】回复“卡组”有我所有视频的卡组代码汇总！\n\n"
    "东少粉丝群：797146320、866958496"
)


class _FakeNotify:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def dispatch(self, event_id, title, body="", *, channels=None, force=False, event_type=None):
        self.calls.append(str(event_id))
        return {"fake": {"status": "sent"}}


def _entry(seqno: int, text: str, *, raw_extra: dict | None = None) -> MessageEntry:
    raw = {
        "msg_seqno": seqno,
        "sender_uid": TALKER_ID,
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "notify_code": "",
        "msg_source": 8,
        "system_msg_type": 0,
    }
    raw.update(raw_extra or {})
    return MessageEntry(
        msg_type=MessageType.DM,
        id=str(seqno),
        talker_id=TALKER_ID,
        talker_name="炉石东少",
        timestamp=2000,
        text=text,
        raw=raw,
    )


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        talker_id=TALKER_ID,
        talker_name="炉石东少",
        unread_count=1,
        timestamp=2000,
        last_text="",
        is_follow=True,
        system_msg_type=0,
        msg_type=MessageType.DM,
        raw={"session_type": 1},
    )


def _real_api_message() -> dict:
    return {
        "msg_seqno": 2599000000000001,
        "sender_uid": TALKER_ID,
        "receiver_type": 1,
        "msg_type": 1,
        "msg_status": 0,
        "notify_code": "",
        "msg_source": 8,
        "system_msg_type": 0,
        "content": json.dumps({"content": REAL_AUTO_REPLY}, ensure_ascii=False),
    }


@pytest.fixture
def installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: tmp_path)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", UID))
    messages: list[MessageEntry] = []
    notify = _FakeNotify()

    def fake_feed(client, msg_type):
        return [], {"id": "1", "time": 1}

    def fake_sessions(client, *, size=20, begin_ts=0, include_system=False):
        return [_session()] if messages else []

    def fake_messages(client, talker_id, *, session_type=1, size=20, end_seqno=0):
        return list(messages)

    monkeypatch.setattr(winmod, "list_feed", fake_feed)
    monkeypatch.setattr(winmod, "list_dm_sessions", fake_sessions)
    monkeypatch.setattr(winmod, "fetch_dm_messages", fake_messages)
    service = winmod.WinnerScanService(notification=notify)
    monkeypatch.setattr(winmod, "service", service)
    return service, messages, notify


def _run(service, messages, text: str, seqno: int = 1):
    messages[:] = [_entry(seqno, text)]
    return service.run(object())


def test_real_bilibili_auto_reply_is_filtered_before_winner_scan(installed) -> None:
    service, messages, notify = installed
    messages[:] = [normalize_dm_message(_real_api_message(), talker_id=TALKER_ID)]
    result = service.run(object())

    assert winmod.is_follow_auto_reply_template(REAL_AUTO_REPLY) is True
    assert messages[0].raw["msg_source"] == 8
    assert messages[0].text == REAL_AUTO_REPLY
    assert result["sources"]["dm"]["found"] == 0
    assert result["auto_reply_skipped_count"] == 1
    assert winmod.list_candidates() == []
    assert notify.calls == []
    state = winmod._load_state()
    assert "dm:204408558:2599000000000001" in state["filtered_auto_replies"]
    assert result["sources"]["dm"]["advance"] is True


@pytest.mark.parametrize(
    "text",
    [
        "感谢您的关注，今天的你一定很幸运……回复‘福利’……",
        "感谢关注，恭喜你发现宝藏主播，加入粉丝群……",
        "感谢您的关注，中奖活动请看主页，回复‘抽奖’……",
        "感谢关注，奖品信息请回复关键词……",
    ],
)
def test_auto_reply_winner_word_collisions_are_filtered(installed, text: str) -> None:
    service, messages, notify = installed
    result = _run(service, messages, text)

    assert winmod.is_follow_auto_reply_template(text) is True
    assert result["sources"]["dm"]["found"] == 0
    assert result["auto_reply_skipped_count"] == 1
    assert winmod.list_candidates() == []
    assert notify.calls == []


@pytest.mark.parametrize(
    "text",
    [
        "恭喜你中奖了，请回复收件人、电话和地址。",
        "中奖啦，感谢关注，请把收件信息发给我。",
        "恭喜中奖，请回复姓名和手机号。",
        "幸运观众就是你，请提供收件地址。",
    ],
)
def test_real_winner_messages_remain_candidates(installed, text: str) -> None:
    service, messages, notify = installed
    result = _run(service, messages, text)

    assert winmod.is_follow_auto_reply_template(text) is False
    assert result["sources"]["dm"]["found"] == 1
    assert result["auto_reply_skipped_count"] == 0
    assert len(winmod.list_candidates()) == 1
    assert len(notify.calls) == 1


@pytest.mark.parametrize(
    "text",
    [
        "感谢关注我的视频",
        "方便的话加一下粉丝群",
        "回复我一下你的地址",
        "你今天很幸运",
    ],
)
def test_ordinary_human_messages_do_not_match_auto_reply_template(installed, text: str) -> None:
    service, messages, _notify = installed
    _run(service, messages, text)

    entry = messages[0]
    assert winmod.is_follow_auto_reply_template(text) is False
    assert winmod.is_high_confidence_follow_auto_reply(entry, _session()) is False


def test_explicit_auto_reply_flag_stays_highest_confidence(installed) -> None:
    service, messages, notify = installed
    messages[:] = [_entry(9, "恭喜你中奖，奖品已准备", raw_extra={"auto_reply": True})]

    result = service.run(object())

    assert result["auto_reply_skipped_count"] == 1
    assert winmod.list_candidates() == []
    assert notify.calls == []
