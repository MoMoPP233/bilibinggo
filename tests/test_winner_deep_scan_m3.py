"""中奖深检（M3）离线测试：全 mock，零真实 Bilibili/通知渠道/LLM/网络。

A/B/C 三来源发现新候选；D 无新消息；E cursor 独立 + partial；F 失败不伪装空结果；
G/H/I 同消息只保存/通知一次；J 不同消息不同 event_id 均通知；
K/L/M 通知 sent/failed/delivery_unknown 均不阻碍 checkpoint 推进；
N winning_candidate 通知关闭：仍保存线索、推进 cursor、0 webhook；
O Profile 隔离；P 私信绝不 mark_dm_read；Q 普通网络失败只影响当前来源；
R/S/T -352/-509/HTTP429 0 retry + 停止后续来源；U 关键词未命中不创建；
V 命中只产生 candidate；W cancel/身份不一致停止后续请求。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.message_types import MessageEntry, MessageType
from src.winner_scan import service as winmod

UID = 101


def _feed_entry(item_id: str, text: str, *, mid: int = 202, ts: int = 1000, uri: str = "") -> MessageEntry:
    return MessageEntry(
        msg_type=MessageType.AT,
        id=str(item_id),
        talker_id=mid,
        talker_name=f"用户{mid}",
        timestamp=ts,
        title="",
        text=text,
        raw={"uri": uri} if uri else {},
    )


def _dm_entry(talker_id: int, seqno: int, text: str, *, ts: int = 2000) -> MessageEntry:
    return MessageEntry(
        msg_type=MessageType.DM,
        id=str(seqno),
        talker_id=talker_id,
        talker_name="私信用户",
        timestamp=ts,
        text=text,
        raw={"msg_seqno": seqno},
    )


def _session(talker_id: int, unread: int, seqno: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        talker_id=talker_id,
        talker_name=f"会话{talker_id}",
        unread_count=unread,
        timestamp=2000,
        last_text="",
        is_follow=True,
        system_msg_type=0,
        msg_type=MessageType.DM,
        raw={"last_msg_seqno": seqno},
    )


class _FakeNotify:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.status = "sent"

    def dispatch(self, event_id, title, body="", *, channels=None, force=False, event_type=None):
        self.calls.append((event_id, event_type))
        return {"fake": {"status": self.status}}


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: target)
    # 通知配置/outbox 同 Profile 目录（M2 模块）
    from src.notification import service as nsvc

    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: target)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", UID))
    return target


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    at: list[MessageEntry] | None = None,
    at_error: Exception | None = None,
    at_cursor: dict | None = None,
    reply: list[MessageEntry] | None = None,
    reply_error: Exception | None = None,
    reply_cursor: dict | None = None,
    sessions: list[SimpleNamespace] | None = None,
    dm_messages: dict[int, list[MessageEntry]] | None = None,
    dm_error: Exception | None = None,
    fake_notify: _FakeNotify | None = None,
) -> dict[str, int]:
    counters: dict[str, int] = {"at": 0, "reply": 0, "dm_session": 0, "dm_msg": 0}

    def fake_feed(client, msg_type):
        counters[msg_type.value] += 1
        if msg_type == MessageType.AT:
            if at_error is not None:
                raise at_error
            return list(at or []), dict(at_cursor or {"id": "11", "time": 111})
        if msg_type == MessageType.REPLY:
            if reply_error is not None:
                raise reply_error
            return list(reply or []), dict(reply_cursor or {"id": "22", "time": 222})
        raise AssertionError(msg_type)

    def fake_sessions(client, *, size=20, begin_ts=0, include_system=False):
        counters["dm_session"] += 1
        if dm_error is not None:
            raise dm_error
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


def _run(monkeypatch, *, expected_uid: int | None = None, cancel=None):
    return winmod.service.run(None, cancel_check=cancel, expected_uid=expected_uid)


WIN_TEXT = "恭喜你被抽中，获得奖品一份，请核对信息填写地址"
PLAIN_TEXT = "今天天气不错，转发一下"

# ---------- A/B/C：三来源各自发现 ----------


def test_mention_discovers_candidate(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("101", WIN_TEXT)])
    result = _run(monkeypatch)
    assert result["partial"] is False
    assert result["sources"]["mention"]["found"] == 1
    candidates = winmod.list_candidates()
    assert len(candidates) == 1
    assert candidates[0]["source_type"] == "mention"
    assert candidates[0]["status"] == "candidate"
    assert candidates[0]["notification_event_id"] == "winning_candidate:mention:101"
    assert fake["notify"].calls == [("winning_candidate:mention:101", "winning_candidate")]


def test_reply_discovers_candidate(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, reply=[_feed_entry("202", WIN_TEXT)])
    result = _run(monkeypatch)
    assert result["sources"]["reply"]["found"] == 1
    assert winmod.list_candidates()[0]["key"] == "reply:202"


def test_dm_discovers_candidate(profile: Path, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        sessions=[_session(3001, unread=2)],
        dm_messages={3001: [_dm_entry(3001, 88, PLAIN_TEXT), _dm_entry(3001, 89, WIN_TEXT)]},
    )
    result = _run(monkeypatch)
    assert result["sources"]["dm"]["found"] == 1
    assert winmod.list_candidates()[0]["key"] == "dm:3001:89"
    assert fake["notify"].calls == [("winning_candidate:dm:3001:89", "winning_candidate")]


def test_no_new_messages_three_sources_idle(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", PLAIN_TEXT)], reply=[], sessions=[])
    result = _run(monkeypatch)
    assert result["partial"] is False
    assert sum(item["found"] for item in result["sources"].values()) == 0
    assert winmod.list_candidates() == []
    assert fake["notify"].calls == []


# ---------- E/F/Q：cursor 独立 + partial + 普通失败不伪装 ----------


def test_partial_failure_advances_only_successful_sources(profile: Path, monkeypatch) -> None:
    pre = winmod._load_state()
    pre["reply"]["cursor_id"] = 99
    winmod._save_state(pre)
    fake = _install(
        monkeypatch,
        at=[_feed_entry("1", WIN_TEXT)],
        reply_error=httpx.ReadTimeout("timeout", request=None),
        dm_messages={1: [_dm_entry(1, 5, WIN_TEXT)]},
        sessions=[_session(1, unread=1)],
    )
    result = _run(monkeypatch)
    assert result["partial"] is True
    assert result["sources"]["mention"]["advance"] is True
    assert result["sources"]["dm"]["advance"] is True
    assert result["sources"]["reply"]["status"] == "failed"
    assert result["sources"]["reply"]["advance"] is False
    state = winmod._load_state()
    assert state["reply"]["cursor_id"] == 99  # 失败来源 checkpoint 不变
    assert state["reply"]["last_error"] != ""
    assert state["mention"]["cursor_id"] == 11
    assert state["mention"]["last_error"] == ""


def test_failure_never_faked_as_empty_success(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at_error=RuntimeError("网络请求失败: reset"), at=[])
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["status"] == "failed"
    assert result["sources"]["mention"]["advance"] is False
    state = winmod._load_state()
    assert state["mention"]["last_error"] != ""
    assert winmod.list_candidates() == []


# ---------- G/H/I：去重（同消息只保存 + 只通知一次） ----------


@pytest.mark.parametrize("source_setup", ["mention", "reply", "dm"])
def test_same_message_scanned_twice_only_notifies_once(profile: Path, monkeypatch, source_setup) -> None:
    if source_setup == "mention":
        entries = [_feed_entry("41", WIN_TEXT)]
        setup = dict(at=entries)
        key = "mention:41"
    elif source_setup == "reply":
        entries = [_feed_entry("42", WIN_TEXT)]
        setup = dict(reply=entries)
        key = "reply:42"
    else:
        entries = [_dm_entry(7001, 7, WIN_TEXT)]
        setup = dict(sessions=[_session(7001, unread=1)], dm_messages={7001: entries})
        key = "dm:7001:7"
    fake = _install(monkeypatch, **setup)
    r1 = _run(monkeypatch)
    assert r1["sources"][source_setup]["found"] == 1
    r2 = _run(monkeypatch)
    assert r2["sources"][source_setup]["found"] == 0
    assert len([c for c in winmod.list_candidates() if c["key"] == key]) == 1
    assert fake["notify"].calls.count((f"winning_candidate:{key}", "winning_candidate")) == 1


def test_two_different_winners_get_different_event_ids(profile: Path, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        at=[_feed_entry("a1", WIN_TEXT), _feed_entry("a2", WIN_TEXT)],
    )
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["found"] == 2
    event_ids = [event for event, _ in fake["notify"].calls]
    assert event_ids == ["winning_candidate:mention:a1", "winning_candidate:mention:a2"]
    assert len(set(event_ids)) == 2


# ---------- K/L/M：通知状态不影响 checkpoint 推进 ----------


@pytest.mark.parametrize("notify_status", ["sent", "failed", "delivery_unknown"])
def test_checkpoint_advances_regardless_of_notify_status(profile: Path, monkeypatch, notify_status) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("61", WIN_TEXT)])
    fake["notify"].status = notify_status
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["advance"] is True
    assert winmod._load_state()["mention"]["cursor_id"] == 11
    assert winmod.list_candidates()[0]["status"] == "candidate"


# ---------- N：通知关闭：仍保存线索/推进 cursor，0 webhook ----------


def test_event_disabled_still_scans_and_advances_without_webhook(
    profile: Path, monkeypatch,
) -> None:
    from src.notification import service as nsvc

    _configure_channel_spy(profile, monkeypatch)
    nsvc.set_event_enabled("winning_candidate", False)
    posts = []

    class SpyClient:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, **kwargs):
            posts.append(url)
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: SpyClient())
    fake = _install(monkeypatch, at=[_feed_entry("71", WIN_TEXT)])
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["found"] == 1
    assert winmod.list_candidates()
    assert winmod._load_state()["mention"]["cursor_id"] == 11
    assert posts == []  # 0 webhook


def _configure_channel_spy(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    nsvc.save_channel_config(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-test"})
    nsvc.set_channel_enabled(nsvc.CHANNEL_SERVERCHAN, True)


# ---------- J 真实 outbox 集成：同 event 只发一次 ----------


def test_real_outbox_dedup_for_repeated_scan(profile: Path, monkeypatch) -> None:
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

    _configure_channel_spy(profile, monkeypatch)
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: SpyClient())
    # 真实 NotificationService（复用 M2 outbox）：验证实例级 event_id 去重
    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=nsvc.NotificationService()))
    monkeypatch.setattr(winmod, "list_feed", lambda client, msg_type: ([_feed_entry("81", WIN_TEXT)] if msg_type == MessageType.AT else [], {"id": "1", "time": 1}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *a, **k: [])
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *a, **k: [])
    winmod.service.run(None)
    winmod.service.run(None)
    assert len(posts) == 1  # 同一 (event_id,channel) 只投递一次


# ---------- O：Profile 隔离 ----------


def test_profile_isolation(tmp_path: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    a = tmp_path / "profile-a"
    b = tmp_path / "profile-b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", UID))
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: a)
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: a)
    monkeypatch.setattr(winmod, "list_feed", lambda client, msg_type: ([_feed_entry("1", WIN_TEXT)], {"id": "1"}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *a, **k: [])
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *a, **k: [])
    winmod.service.run(None)

    monkeypatch.setattr(winmod, "get_profile_dir", lambda: b)
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: b)
    assert winmod.scan_status_view()["candidates"] == []  # B 读不到 A 的线索
    assert winmod._load_state()["mention"]["last_success_at"] is None


# ---------- P：绝不 mark_dm_read ----------


def test_dm_scan_never_marks_read(profile: Path, monkeypatch) -> None:
    source = Path(winmod.__file__).read_text(encoding="utf-8")
    assert "def mark_dm_read" not in source
    assert "mark_dm_read(" not in source
    assert "update_ack" not in source
    assert "ack_seqno" not in source


# ---------- Q：普通网络失败只影响当前来源 ----------


def test_network_failure_isolated_to_current_source(profile: Path, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        at=[_feed_entry("9", WIN_TEXT)],
        reply_error=httpx.ConnectError("refused"),
        dm_messages={5: [_dm_entry(5, 1, WIN_TEXT)]},
        sessions=[_session(5, unread=1)],
    )
    result = _run(monkeypatch)
    assert result["partial"] is True
    assert result["sources"]["reply"]["advance"] is False
    assert result["sources"]["mention"]["advance"] is True
    assert result["sources"]["dm"]["advance"] is True


# ---------- R/S/T：风控 0 retry + 停止后续来源 ----------


@pytest.mark.parametrize("risk_error", [
    RuntimeError("API error -352: 风控校验失败"),
    RuntimeError("API error -509: 访问过于频繁"),
    httpx.HTTPStatusError(
        "429",
        request=httpx.Request("GET", "https://api.bilibili.com/x/msgfeed/at"),
        response=httpx.Response(429, request=httpx.Request("GET", "https://api.bilibili.com/x/msgfeed/at")),
    ),
])
def test_structured_risk_stops_all_following_requests(profile: Path, monkeypatch, risk_error) -> None:
    fake = _install(monkeypatch, at_error=risk_error)
    with pytest.raises((RuntimeError, httpx.HTTPStatusError)):
        _run(monkeypatch)
    assert fake["counters"]["at"] == 1
    assert fake["counters"]["reply"] == 0  # 停止后续 B 请求
    assert fake["counters"]["dm_session"] == 0


def test_risk_in_middle_source_stops_remaining_after_committed_source(profile: Path, monkeypatch) -> None:
    fake = _install(
        monkeypatch,
        at=[_feed_entry("3", WIN_TEXT)],
        reply_error=RuntimeError("API error -509: 访问过于频繁"),
    )
    with pytest.raises(RuntimeError):
        _run(monkeypatch)
    assert fake["counters"]["dm_session"] == 0  # dm 未执行
    # mention 的成功结果不回滚（已提交）
    assert winmod._load_state()["mention"]["last_success_at"] is not None


# ---------- U/V：关键词语义 ----------


def test_no_keyword_hit_no_candidate(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", PLAIN_TEXT)])
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["found"] == 0
    assert winmod.list_candidates() == []


def test_keyword_hit_only_produces_candidate_never_confirmed(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", "恭喜中奖")])
    _run(monkeypatch)
    candidates = winmod.list_candidates()
    assert len(candidates) == 1
    assert candidates[0]["status"] == "candidate"
    assert all("confirmed" not in str(item) for item in candidates)
    assert all("已中奖" not in str(item.get("summary")) for item in candidates)


def test_blacklist_keyword_rule_respected(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", "预约成功，预约主题已上线")])
    result = _run(monkeypatch)
    assert result["sources"]["mention"]["found"] == 0


# ---------- W：cancel / 身份不一致 ----------


def test_cancel_stops_following_requests(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", WIN_TEXT)])
    calls = {"n": 0}

    def cancel_check():
        calls["n"] += 1
        return calls["n"] >= 2  # mention 之后取消

    with pytest.raises(RuntimeError, match="任务已取消"):
        _run(monkeypatch, cancel=cancel_check)
    assert fake["counters"]["reply"] == 0
    assert fake["counters"]["dm_session"] == 0
    assert winmod._load_state()["mention"]["last_success_at"] is not None  # 已成功来源不回滚


def test_expected_uid_mismatch_stops_before_any_request(profile: Path, monkeypatch) -> None:
    fake = _install(monkeypatch, at=[_feed_entry("1", WIN_TEXT)])
    with pytest.raises(RuntimeError, match="登录账号与预期不符"):
        _run(monkeypatch, expected_uid=999)
    assert fake["counters"]["at"] == 0

# ===========================================================================
# P1 补修：crash consistency（candidate→claim→HTTP→terminal→checkpoint 顺序）
# ===========================================================================


def _real_notify_run(profile, monkeypatch, *, feed_entries=None, feed_error=None):
    """用真实 NotificationService（M2）跑一轮 mention-only 扫描。"""
    from src.notification import service as nsvc

    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=nsvc.NotificationService()))
    counters = {"posts": 0}

    class SpyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            counters["posts"] += 1
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: SpyClient())
    nsvc.save_channel_config(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-crash"})
    nsvc.set_channel_enabled(nsvc.CHANNEL_SERVERCHAN, True)
    entries = feed_entries if feed_entries is not None else [_feed_entry("c1", WIN_TEXT)]

    def feed(client, msg_type):
        if feed_error is not None:
            raise feed_error
        return (entries if msg_type == MessageType.AT else []), {"id": "7", "time": 7}

    monkeypatch.setattr(winmod, "list_feed", feed)
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *a, **k: [])
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *a, **k: [])
    return counters


def test_crash_after_candidate_save_before_dispatch_no_advance(profile: Path, monkeypatch) -> None:
    class BoomNotify:
        def dispatch(self, *a, **k):
            raise winmod.NotificationLocalError("模拟崩溃：dispatch 前进程死亡")

    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=BoomNotify()))
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([_feed_entry("c1", WIN_TEXT)], {"id": "9"}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *a, **k: [])
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *a, **k: [])
    result = winmod.service.run(None)
    assert result["sources"]["mention"]["advance"] is False
    assert winmod.list_candidates()  # candidate 已 durable
    assert winmod._load_state()["mention"]["cursor_id"] == 0  # checkpoint 未推进


def test_claim_persist_failure_zero_http_no_advance(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)
    monkeypatch.setattr(
        nsvc, "_claim_records",
        lambda *a, **k: (_ for _ in ()).throw(winmod.NotificationLocalError("claim 写入失败")),
    )
    result = winmod.service.run(None)
    assert counters["posts"] == 0  # 0 HTTP
    assert result["sources"]["mention"]["advance"] is False
    assert winmod.list_candidates()  # 候选已保存


def test_crash_after_claim_before_http_no_auto_resend_on_restart(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)

    def boom_provider(channel, entry, title, body):
        raise SystemExit("模拟 HTTP 前进程死亡")

    monkeypatch.setattr(nsvc, "_provider_send", boom_provider)
    with pytest.raises(SystemExit):
        winmod.service.run(None)
    # claim 已 durable（dispatching）
    assert nsvc._existing_status("winning_candidate:mention:c1", nsvc.CHANNEL_SERVERCHAN) == "dispatching"
    # “重启”：再扫描同消息 → 不产生任何新 HTTP（候选去重 + claim fail-closed）
    monkeypatch.setattr(nsvc, "_provider_send", lambda *a, **k: "sent")
    result = winmod.service.run(None)
    assert counters["posts"] == 0
    assert result["sources"]["mention"]["advance"] is True  # 本地状态已可安全推进
    # 遗留 dispatching 在后续 dispatch/重扫中被落为 delivery_unknown
    nsvc.NotificationService().dispatch(
        "winning_candidate:mention:c1", "t", "b", event_type="winning_candidate"
    )
    assert nsvc._existing_status("winning_candidate:mention:c1", nsvc.CHANNEL_SERVERCHAN) == "delivery_unknown"


def test_crash_after_http_before_result_persist_no_resend(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)
    real_finalize = nsvc._finalize_record

    def crash_finalize(event_id, channel, status, summary):
        raise winmod.NotificationLocalError("模拟结果落盘前崩溃")

    monkeypatch.setattr(nsvc, "_finalize_record", crash_finalize)
    result = winmod.service.run(None)
    assert counters["posts"] == 1  # HTTP 已执行
    assert result["sources"]["mention"]["advance"] is False
    # claim 保持 dispatching（保守）
    assert nsvc._existing_status("winning_candidate:mention:c1", nsvc.CHANNEL_SERVERCHAN) == "dispatching"
    # restart：0 自动 resend
    monkeypatch.setattr(nsvc, "_finalize_record", real_finalize)
    winmod.service.run(None)
    assert counters["posts"] == 1


def test_sent_then_crash_before_checkpoint_advance_no_duplicate(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)
    state = winmod._load_state()
    state["mention"]["cursor_id"] = 0
    winmod._save_state(state)
    first = winmod.service.run(None)
    assert first["sources"]["mention"]["advance"] is True
    assert counters["posts"] == 1
    # 模拟“sent 已落盘但 checkpoint 未推进”的另一种形态：直接回退 cursor
    state = winmod._load_state()
    state["mention"]["cursor_id"] = 0
    winmod._save_state(state)
    second = winmod.service.run(None)  # 同消息再次可见
    assert second["sources"]["mention"]["found"] == 0  # 候选已去重
    assert counters["posts"] == 1  # 0 重复 HTTP（outbox sent 挡住）
    assert winmod._load_state()["mention"]["cursor_id"] == 7  # 最终安全推进


def test_multi_channel_partial_durable_and_advance(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)
    nsvc.save_channel_config(nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/x/f"})
    nsvc.set_channel_enabled(nsvc.CHANNEL_FEISHU, True)

    original = nsvc.create_notify_client

    class MultiSpyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            counters["posts"] += 1
            if "open.feishu.cn" in url:
                raise httpx.ReadTimeout("t", request=None)
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: MultiSpyClient())
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([_feed_entry("m2", WIN_TEXT)] if m == MessageType.AT else [], {"id": "8"}))
    result = winmod.service.run(None)
    assert result["sources"]["mention"]["advance"] is True
    assert counters["posts"] == 2
    assert nsvc._existing_status("winning_candidate:mention:m2", nsvc.CHANNEL_SERVERCHAN) == "sent"
    assert nsvc._existing_status("winning_candidate:mention:m2", nsvc.CHANNEL_FEISHU) == "delivery_unknown"
    # restart：0 重复
    monkeypatch.setattr(nsvc, "create_notify_client", original)
    winmod.service.run(None)
    assert counters["posts"] == 2


def test_candidate_persist_failure_no_advance_no_http(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _real_notify_run(profile, monkeypatch)

    def boom_save(candidates):
        raise OSError("磁盘满")

    monkeypatch.setattr(winmod, "_save_candidates", boom_save)
    result = winmod.service.run(None)
    assert result["sources"]["mention"]["advance"] is False
    assert counters["posts"] == 0  # 候选未落库 → 不发 HTTP

# ===========================================================================
# atomic batch claim：多渠道 claim 必须“全有或全无”单次落盘
# ===========================================================================


def _three_channel_run(profile, monkeypatch, *, provider_hook=None, claim_raise=None, save_fail_on_dispatching=False):
    from src.notification import service as nsvc

    counters = {"posts": 0}
    for channel, values in [
        (nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-abc"}),
        (nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/x/f"}),
        (nsvc.CHANNEL_DINGTALK, {"token": "tok", "secret": ""}),
    ]:
        nsvc.save_channel_config(channel, values)
        nsvc.set_channel_enabled(channel, True)

    class SpyClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, **kwargs):
            counters["posts"] += 1
            if provider_hook is not None:
                provider_hook(url, nsvc._load_outbox())
            return httpx.Response(200, json={"code": 0}, request=httpx.Request("POST", url))

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: SpyClient())

    original_save = nsvc._save_outbox

    if save_fail_on_dispatching:
        def broken_save(records):
            if any(r.get("status") == "dispatching" for r in records):
                raise OSError("disk full")
            original_save(records)

        monkeypatch.setattr(nsvc, "_save_outbox", broken_save)

    if claim_raise is not None:
        def boom(*a, **k):
            raise winmod.NotificationLocalError(claim_raise)

        monkeypatch.setattr(nsvc, "_claim_records", boom)

    monkeypatch.setattr(winmod, "service", winmod.WinnerScanService(notification=nsvc.NotificationService()))
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([_feed_entry("cc1", WIN_TEXT)] if m == MessageType.AT else [], {"id": "6"}))
    monkeypatch.setattr(winmod, "list_dm_sessions", lambda *a, **k: [])
    monkeypatch.setattr(winmod, "fetch_dm_messages", lambda *a, **k: [])
    return counters


def test_atomic_claims_durable_before_first_http(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    snapshots: list[list] = []

    def on_first_post(url, outbox):
        if len(snapshots) == 0:
            snapshots.append(list(outbox))
            # 第一个 HTTP 发生时：三个 dispatching 都必须已经 durable
            channels_now = {
                r["channel"]
                for r in snapshots[0]
                if r["status"] == "dispatching" and r.get("event_id") == "winning_candidate:mention:cc1"
            }
            assert channels_now == {nsvc.CHANNEL_SERVERCHAN, nsvc.CHANNEL_FEISHU, nsvc.CHANNEL_DINGTALK}

    counters = _three_channel_run(profile, monkeypatch, provider_hook=on_first_post)
    result = winmod.service.run(None)
    assert result["sources"]["mention"]["advance"] is True
    assert counters["posts"] == 3


def test_batch_claim_write_failure_leaves_no_dispatching(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _three_channel_run(profile, monkeypatch, save_fail_on_dispatching=True)
    result = winmod.service.run(None)
    assert counters["posts"] == 0  # 0 HTTP
    assert result["sources"]["mention"]["advance"] is False
    event = "winning_candidate:mention:cc1"
    leftovers = [r for r in nsvc._load_outbox() if r.get("event_id") == event]
    assert leftovers == []  # 三个 channel 都没有 dispatching（全有或全无）


def test_crash_before_atomic_claim_persist_no_unknown_and_retryable(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    real_claim = nsvc._claim_records
    counters = _three_channel_run(profile, monkeypatch, claim_raise="claim 前崩溃")
    result = winmod.service.run(None)
    assert counters["posts"] == 0
    assert result["sources"]["mention"]["advance"] is False
    event = "winning_candidate:mention:cc1"
    # claim 从未落盘：不存在 dispatching，也不存在 delivery_unknown
    records = [r for r in nsvc._load_outbox() if r.get("event_id") == event]
    assert records == []
    # 下一轮（新消息）允许正常发送（此前该候选 0 HTTP、claim 无残留）
    monkeypatch.setattr(nsvc, "_claim_records", real_claim)
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([_feed_entry("cc2", WIN_TEXT)] if m == MessageType.AT else [], {"id": "6"}))
    result2 = winmod.service.run(None)
    assert result2["sources"]["mention"]["advance"] is True
    assert counters["posts"] == 3


def test_crash_after_full_atomic_claims_before_http_stays_fail_closed(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _three_channel_run(profile, monkeypatch)

    def boom(channel, entry, title, body):
        raise SystemExit("HTTP 前进程死亡")

    monkeypatch.setattr(nsvc, "_provider_send", boom)
    with pytest.raises(SystemExit):
        winmod.service.run(None)
    event = "winning_candidate:mention:cc1"
    claims = [r for r in nsvc._load_outbox() if r.get("event_id") == event and r.get("status") == "dispatching"]
    assert {r["channel"] for r in claims} == {nsvc.CHANNEL_SERVERCHAN, nsvc.CHANNEL_FEISHU, nsvc.CHANNEL_DINGTALK}
    # restart / 再次 dispatch：全部转 delivery_unknown，0 自动 resend
    nsvc.NotificationService().dispatch(event, "t", "b", event_type="winning_candidate")
    statuses = {r["channel"]: r["status"] for r in nsvc._load_outbox() if r.get("event_id") == event}
    assert set(statuses.values()) == {"delivery_unknown"}
    assert counters["posts"] == 0


def test_no_half_batch_claim_under_internal_failure(profile: Path, monkeypatch) -> None:
    from src.notification import service as nsvc

    counters = _three_channel_run(profile, monkeypatch)
    real_mutate = nsvc._mutate_outbox
    calls = {"n": 0}

    def flaky_mutate(mutator):
        calls["n"] += 1
        if calls["n"] == 1:
            raise winmod.NotificationLocalError("内部异常")  # 原子写前崩溃
        return real_mutate(mutator)

    monkeypatch.setattr(nsvc, "_mutate_outbox", flaky_mutate)
    result = winmod.service.run(None)
    assert result["sources"]["mention"]["advance"] is False
    event = "winning_candidate:mention:cc1"
    channels = {r["channel"] for r in nsvc._load_outbox() if r.get("event_id") == event}
    assert channels == set()  # 全无；不允许半批

    # 恢复后新消息可以正常发送（之前 0 HTTP、无残留 claim）
    monkeypatch.setattr(nsvc, "_mutate_outbox", real_mutate)
    monkeypatch.setattr(winmod, "list_feed", lambda c, m: ([_feed_entry("cc3", WIN_TEXT)] if m == MessageType.AT else [], {"id": "6"}))
    result2 = winmod.service.run(None)
    assert result2["sources"]["mention"]["advance"] is True
    assert counters["posts"] == 3
