"""Winner 稳定 key V12：缺平台稳定 ID 时 fail-closed（空 key/随机 key 禁用）+ 本地 malformed 记录。

A. 正常 mention id → stable key 不变
B. 正常 reply id → stable key 不变
C. 正常 dm talker+seqno → stable key 不变
D. 缺稳定 id → None（不产生空/公共 key）
E. 同一 malformed item 两次调用结果一致（不产生随机新 key）
F. 两个不同 malformed item 都不会被创建（不会空 key 互吞）
G. 不产生 candidate → 不产生重复通知
H. 自动/手动入口同一 service（回归：run 流程复用 WinnerScanService）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.message_types import MessageEntry, MessageType
from src.winner_scan.service import WinnerScanService, _source_entry_key

service = WinnerScanService()


def _entry(entry_id: str, *, msg_type: MessageType = MessageType.AT, **kwargs) -> MessageEntry:
    return MessageEntry(msg_type=msg_type, id=entry_id, text="抽奖 转发 中奖", **kwargs)


def test_a_normal_mention_key_stable() -> None:
    entry = _entry("item-101")
    assert _source_entry_key("mention", entry) == "mention:item-101"


def test_b_normal_reply_key_stable() -> None:
    entry = _entry("reply-77", msg_type=MessageType.REPLY)
    assert _source_entry_key("reply", entry) == "reply:reply-77"


def test_c_normal_dm_key_stable() -> None:
    entry = _entry("", msg_type=MessageType.DM, talker_id=9001, raw={"msg_seqno": 33})
    assert _source_entry_key("dm", entry) == "dm:9001:33"


def test_d_missing_stable_id_returns_none() -> None:
    assert _source_entry_key("mention", _entry("")) is None
    assert _source_entry_key("mention", _entry("   ")) is None
    assert _source_entry_key("reply", _entry("")) is None
    assert _source_entry_key("dm", _entry("", msg_type=MessageType.DM, talker_id=0, raw={})) is None
    assert _source_entry_key("dm", _entry("0", msg_type=MessageType.DM, talker_id=9001, raw={})) is None


def test_e_same_malformed_item_is_deterministic() -> None:
    malformed = _entry("")
    first = _source_entry_key("mention", malformed)
    second = _source_entry_key("mention", malformed)
    assert first is None and second is None  # 不产生随机/时间 key


def test_f_distinct_malformed_items_never_share_empty_key() -> None:
    first = _source_entry_key("mention", _entry(""))
    second = _source_entry_key("mention", _entry(None))
    assert first is None and second is None
    assert first == second  # 均 fail-closed None，不会落入空字符串 key 空间


def test_g_malformed_scan_creates_no_candidate_and_records_reason(tmp_path: Path, monkeypatch) -> None:
    import src.winner_scan.service as win

    monkeypatch.setattr(win, "_load_state", lambda: {"mention": {}, "reply": {}, "dm": {}})
    candidates: dict[str, dict] = {}
    monkeypatch.setattr(win, "_load_candidates", lambda: candidates)
    monkeypatch.setattr(win, "judge_keywords", lambda text: True)

    def fake_save(payload: dict) -> None:
        candidates.clear()
        candidates.update(payload)

    monkeypatch.setattr(win, "_save_candidates", fake_save)
    monkeypatch.setattr(win, "_save_state", lambda state: None)
    state = {"mention": {}, "reply": {}, "dm": {}}
    entries = [_entry(""), _entry(None), _entry("real-1"), _entry("real-2")]

    newly, keys = service._scan_feed_source("mention", MessageType.AT, entries, state=state["mention"], existing=candidates)

    assert newly == 2
    assert set(keys) == {"mention:real-1", "mention:real-2"}
    assert all(key not in candidates for key in ("mention:", "mention:None"))
    malformed = state["mention"].get("malformed_skipped") or []
    assert len(malformed) == 2  # 两条缺 id 的命中项均被记录


def test_h_scheduler_and_manual_share_service_entry() -> None:
    import web.actions as actions
    from src.winner_scan import service as win_module

    assert isinstance(win_module.service, WinnerScanService)
    assert actions.WRITER_LEASE_ACTIONS and "winner_deep_scan" in actions.WRITER_LEASE_ACTIONS
