"""V25 关注后自动回复过滤专项（全离线 mock/fixture，零真实网络/通知/mutation）。

CASE 1-3   欢迎模板（含“恭喜成为我的粉丝”）→ 0 candidate、0 notification、skipped 计数。
CASE 4-7   强中奖/领奖/链接 → 必须保留 Winner candidate 并通知。
CASE 8-9   is_follow=False / 语义缺失 → 不进行高置信度过滤（fail-safe 保留）。
CASE 10-11 sender 非对方 / system 消息 → 保持既有行为。
CASE 12    同一欢迎 DM 重复扫描 → 不重复计数、不重复通知。
CASE 13    同 talker 后续真正中奖 DM → 正常 candidate + 通知。
CASE 14    Profile 隔离。
CASE 15    mention/reply 行为不变。
CASE 16-17 隐私与 mutation 安全。
CASE 18-19 Server酱/多渠道 fake：auto reply 0 调用，winner 正常。
CASE 20    不依赖“会话首条”判断。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.message_types import MessageEntry, MessageType
from src.winner_scan import service as winmod

UID = 101
WIN_TEXT = "恭喜你被抽中，获得奖品一份，请核对信息填写地址"
WELCOME_TEXT = "感谢关注"


def _dm_entry(
    talker_id: int,
    seqno: int,
    text: str,
    *,
    sender_uid: int | None = None,
    system_msg_type: int = 0,
    msg_type: MessageType | None = None,
) -> MessageEntry:
    raw = {
        "msg_seqno": seqno,
        "sender_uid": talker_id if sender_uid is None else sender_uid,
        "system_msg_type": system_msg_type,
    }
    return MessageEntry(
        msg_type=MessageType.DM if msg_type is None else msg_type,
        id=str(seqno),
        talker_id=talker_id,
        talker_name=f"UP{talker_id}",
        timestamp=2000,
        text=text,
        raw=raw,
    )


def _session(
    talker_id: int,
    unread: int = 1,
    *,
    is_follow: bool | None = True,
    system_msg_type: int = 0,
) -> SimpleNamespace:
    values: dict[str, object] = {
        "talker_id": talker_id,
        "talker_name": f"UP{talker_id}",
        "unread_count": unread,
        "timestamp": 2000,
        "last_text": "",
        "system_msg_type": system_msg_type,
        "msg_type": MessageType.DM,
        "raw": {},
    }
    if is_follow is not None:
        values["is_follow"] = is_follow
    return SimpleNamespace(**values)


class _FakeNotify:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def dispatch(self, event_id, title, body="", *, channels=None, force=False, event_type=None):
        self.calls.append((event_id, event_type))
        return {"fake": {"status": "sent"}}


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: target)
    from src.notification import service as nsvc

    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: target)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", UID))
    return target


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    at: list[MessageEntry] | None = None,
    reply: list[MessageEntry] | None = None,
    sessions: list[SimpleNamespace] | None = None,
    dm_messages: dict[int, list[MessageEntry]] | None = None,
    fake_notify: _FakeNotify | None = None,
) -> dict:
    counters = {"at": 0, "reply": 0, "dm_msg": 0}

    def fake_feed(client, msg_type):
        counters[msg_type.value] += 1
        if msg_type == MessageType.AT:
            return list(at or []), {"id": "11", "time": 111}
        if msg_type == MessageType.REPLY:
            return list(reply or []), {"id": "22", "time": 222}
        raise AssertionError(msg_type)

    def fake_sessions(client, *, size=20, begin_ts=0, include_system=False):
        return list(sessions or [])

    def fake_messages(client, talker_id, *, session_type=1, size=20, end_seqno=0):
        counters["dm_msg"] += 1
        return list((dm_messages or {}).get(int(talker_id), []))

    monkeypatch.setattr(winmod, "list_feed", fake_feed)
    monkeypatch.setattr(winmod, "list_dm_sessions", fake_sessions)
    monkeypatch.setattr(winmod, "fetch_dm_messages", fake_messages)
    notify = fake_notify or _FakeNotify()
    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=notify))
    return {"counters": counters, "notify": notify}


def _run(monkeypatch: pytest.MonkeyPatch) -> dict:
    return winmod.service.run(None)


# ---------- CASE 1-3: 高置信度欢迎自动回复被过滤 ----------


@pytest.mark.parametrize(
    "text",
    ["感谢关注", "恭喜成为我的粉丝", "欢迎关注，谢谢支持"],
    ids=["thanks_follow", "congrats_fan", "welcome_thanks"],
)
def test_case1_2_3_welcome_auto_reply_filtered(text, profile, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, text)]},
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 1
    assert result["sources"]["dm"]["found"] == 0
    assert result["sources"]["dm"]["auto_reply_skipped"] == 1
    assert winmod.list_candidates() == []
    assert fake["notify"].calls == []
    assert "dm:3001:1" in winmod._load_state()["filtered_auto_replies"]


# ---------- CASE 4-7: 强中奖/链接必须保留 ----------


@pytest.mark.parametrize(
    "text",
    [
        "恭喜你中奖了，请填写收货地址",
        "感谢关注，你抽中了奖品",
        "感谢关注，恭喜你 https://example.com/x",
        "欢迎关注，请填写收货地址领取奖品",
    ],
    ids=["win_address", "thanks_prize", "welcome_link", "welcome_claim"],
)
def test_case4_5_6_7_strong_signals_keep_candidate(text, profile, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, text)]},
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0
    assert result["sources"]["dm"]["found"] == 1
    assert len(winmod.list_candidates()) == 1
    assert fake["notify"].calls == [("winning_candidate:dm:3001:1", "winning_candidate")]


# ---------- CASE 8-11: 结构条件缺失时 fail-safe 保留 ----------


def test_case8_is_follow_false_does_not_filter(profile, monkeypatch) -> None:
    _install(
        monkeypatch,
        sessions=[_session(3001, is_follow=False)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT)]},
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0
    assert winmod._load_state()["filtered_auto_replies"] == []


def test_case9_missing_is_follow_fail_safe(profile, monkeypatch) -> None:
    _install(
        monkeypatch,
        sessions=[_session(3001, is_follow=None)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT)]},
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0


def test_case10_outbound_sender_not_filtered(profile, monkeypatch) -> None:
    _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT, sender_uid=UID)]},
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0


def test_case11_system_message_keeps_existing_filter(profile, monkeypatch) -> None:
    _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={
            3001: [
                _dm_entry(
                    3001,
                    1,
                    WELCOME_TEXT,
                    system_msg_type=5,
                    msg_type=MessageType.SYS,
                )
            ]
        },
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0
    assert result["sources"]["dm"]["found"] == 0
    assert winmod.list_candidates() == []


# ---------- CASE 12-13: 去噪与同 talker 后续中奖 ----------


def test_case12_repeat_scan_no_repeat_noise(profile, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT)]},
    )

    first = _run(monkeypatch)
    second = _run(monkeypatch)

    assert first["auto_reply_skipped_count"] == 1
    assert second["auto_reply_skipped_count"] == 0  # 已记录，不重复累加
    assert winmod.list_candidates() == []
    assert fake["notify"].calls == []
    state = winmod._load_state()
    assert state["filtered_auto_replies"].count("dm:3001:1") == 1


def test_case13_same_talker_later_winner_still_notifies(profile, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        sessions=[_session(3001, unread=2)],
        dm_messages={
            3001: [
                _dm_entry(3001, 1, WELCOME_TEXT),
                _dm_entry(3001, 9, WIN_TEXT),
            ]
        },
    )

    first = _run(monkeypatch)

    assert first["auto_reply_skipped_count"] == 1
    assert first["sources"]["dm"]["found"] == 1
    assert winmod.list_candidates()[0]["key"] == "dm:3001:9"
    assert fake["notify"].calls == [("winning_candidate:dm:3001:9", "winning_candidate")]

    second = _run(monkeypatch)
    assert second["auto_reply_skipped_count"] == 0
    assert second["sources"]["dm"]["found"] == 0
    assert len(fake["notify"].calls) == 1


# ---------- CASE 14-15: Profile 隔离 / mention-reply 不变 ----------


def test_case14_profile_isolation(tmp_path: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    profile_a.mkdir(parents=True, exist_ok=True)
    profile_b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", UID))
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([], {"id": "1"}))
    monkeypatch.setattr(
        winmod,
        "list_dm_sessions",
        lambda *a, **k: [_session(3001)],
    )
    monkeypatch.setattr(
        winmod,
        "fetch_dm_messages",
        lambda *a, **k: [_dm_entry(3001, 1, WELCOME_TEXT)],
    )

    monkeypatch.setattr(winmod, "get_profile_dir", lambda: profile_a)
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: profile_a)
    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=_FakeNotify()))
    first = winmod.service.run(None)
    assert first["auto_reply_skipped_count"] == 1

    monkeypatch.setattr(winmod, "get_profile_dir", lambda: profile_b)
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: profile_b)
    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=_FakeNotify()))
    second = winmod.service.run(None)
    assert second["auto_reply_skipped_count"] == 1  # B 独立计数

    assert (profile_a / "winner_scan_state.json").exists()
    assert (profile_b / "winner_scan_state.json").exists()


def test_case15_mention_reply_unchanged(profile, monkeypatch) -> None:
    at_entry = MessageEntry(
        msg_type=MessageType.AT,
        id="55",
        talker_id=777,
        talker_name="at-user",
        timestamp=1000,
        text=WIN_TEXT,
        raw={},
    )
    reply_entry = MessageEntry(
        msg_type=MessageType.REPLY,
        id="66",
        talker_id=888,
        talker_name="reply-user",
        timestamp=1000,
        text=WELCOME_TEXT,
        raw={},
    )
    fake = _install(monkeypatch, at=[at_entry], reply=[reply_entry])

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 0
    assert result["sources"]["mention"]["found"] == 1
    assert result["sources"]["reply"]["found"] == 0  # 欢迎文本无关键词 → 不产生候选
    keys = [call[0] for call in fake["notify"].calls]
    assert keys == ["winning_candidate:mention:55"]
    state = winmod._load_state()
    assert state["filtered_auto_replies"] == []  # 只对 DM 过滤


# ---------- CASE 16-17: 隐私与 mutation 安全 ----------


def test_case16_no_dm_body_in_state_or_result(profile, monkeypatch) -> None:
    text = "感谢关注🎉"
    _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, text)]},
    )

    result = _run(monkeypatch)
    state_text = (profile / "winner_scan_state.json").read_text(encoding="utf-8")

    assert text not in state_text
    assert text not in str(result)
    assert "dm:3001:1" in state_text


def test_case17_no_mutation_calls_in_service_source() -> None:
    source = Path(winmod.__file__).read_text(encoding="utf-8")
    for forbidden in ("mark_dm_read", "update_ack", "ack_seqno", "send_msg", "delete_msg"):
        assert forbidden not in source


# ---------- CASE 18-19: Server酱 / 多渠道 fake ----------


def _configure_channels(monkeypatch, channels) -> list[str]:
    from src.notification import service as nsvc

    posts: list[str] = []

    class SpyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            posts.append(url)
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    for channel, values in channels:
        nsvc.save_channel_config(channel, values)
        nsvc.set_channel_enabled(channel, True)
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: SpyClient())
    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=nsvc.NotificationService()))
    return posts


def test_case18_serverchan_auto_reply_zero_winner_one(profile, monkeypatch) -> None:
    from src.notification import service as nsvc

    _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT)]},
    )
    posts = _configure_channels(monkeypatch, [(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-v25"})])

    auto = _run(monkeypatch)
    assert auto["auto_reply_skipped_count"] == 1
    assert posts == []

    # 同一 talker 的后续真实中奖消息 → 正常 1 次 Server酱
    monkeypatch.setattr(
        winmod,
        "fetch_dm_messages",
        lambda *a, **k: [
            _dm_entry(3001, 1, WELCOME_TEXT),
            _dm_entry(3001, 9, WIN_TEXT),
        ],
    )
    winner = _run(monkeypatch)
    assert winner["sources"]["dm"]["found"] == 1
    assert len(posts) == 1


def test_case19_multi_provider_auto_reply_zero(profile, monkeypatch) -> None:
    from src.notification import service as nsvc

    _install(
        monkeypatch,
        sessions=[_session(3001)],
        dm_messages={3001: [_dm_entry(3001, 1, WELCOME_TEXT)]},
    )
    posts = _configure_channels(
        monkeypatch,
        [
            (nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-v25"}),
            (nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/x/f"}),
            (nsvc.CHANNEL_DINGTALK, {"token": "tok", "secret": ""}),
        ],
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 1
    assert posts == []


# ---------- CASE 20 + helper 单元 ----------


def test_case20_no_first_message_dependency(profile, monkeypatch) -> None:
    """欢迎模板在高 seqno 仍被过滤；中奖在 seqno=1 仍保留 → 不依赖“会话首条”。"""
    fake = _install(
        monkeypatch,
        sessions=[_session(3001, unread=2)],
        dm_messages={
            3001: [
                _dm_entry(3001, 1, WIN_TEXT),
                _dm_entry(3001, 500, WELCOME_TEXT),
            ]
        },
    )

    result = _run(monkeypatch)

    assert result["auto_reply_skipped_count"] == 1
    assert result["sources"]["dm"]["found"] == 1
    assert winmod.list_candidates()[0]["key"] == "dm:3001:1"
    assert [call[0] for call in fake["notify"].calls] == ["winning_candidate:dm:3001:1"]


@pytest.mark.parametrize(
    "text",
    [
        "感谢关注",
        "谢谢关注",
        "欢迎关注",
        "感谢你的关注",
        "感谢您的关注",
        "谢谢你的支持与关注",
        "欢迎成为我的粉丝",
        "感谢成为我的粉丝",
        "恭喜成为我的粉丝",
        "亲爱的，感谢关注！",
        "感谢关注🎉",
        "你好，感谢关注~",
        "感谢关注谢谢支持",
    ],
)
def test_welcome_template_positive(text) -> None:
    assert winmod.is_welcome_template(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        "关注我",
        "恭喜你获得粉丝牌",
        "感谢关注我的动态",
        "今天天气不错",
        "欢迎关注并转发抽奖",
        "感谢关注，恭喜你抽中大奖",
        "请填写收货地址",
    ],
)
def test_welcome_template_negative(text) -> None:
    assert winmod.is_welcome_template(text) is False


@pytest.mark.parametrize(
    "text",
    ["恭喜你中奖了", "你抽中了奖品", "请填写收货地址", "恭喜获奖", "中奖名单公布", "领奖请联系", "兑奖方式", "奖品已寄出"],
)
def test_strong_winner_signal_positive(text) -> None:
    assert winmod.has_strong_winner_signal(text) is True


@pytest.mark.parametrize("text", ["恭喜成为我的粉丝", "感谢关注", "恭喜你获得粉丝牌", "幸运的一天"])
def test_strong_winner_signal_negative(text) -> None:
    assert winmod.has_strong_winner_signal(text) is False


@pytest.mark.parametrize(
    "text",
    ["https://example.com", "http://x.cn/a", "www.bilibili.com/x", "b23.tv/abc", "t.bilibili.com/123"],
)
def test_link_detection_positive(text) -> None:
    assert winmod.contains_link(text) is True


def test_link_detection_negative() -> None:
    assert winmod.contains_link("感谢关注，恭喜成为粉丝") is False


def test_link_veto_in_high_confidence_helper() -> None:
    session = _session(3001)
    assert winmod.is_high_confidence_follow_auto_reply(
        _dm_entry(3001, 1, "感谢关注"), session
    ) is True
    assert winmod.is_high_confidence_follow_auto_reply(
        _dm_entry(3001, 1, "感谢关注 http://x.cn/a"), session
    ) is False
