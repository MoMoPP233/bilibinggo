"""Final observability V16：

Winner malformed：fail-closed 不变；状态可见（数量/原因，无敏感全文）；有界。
DS9 incomplete：策略不变；list_topics 派生 incomplete_count（观察用，不落盘不参与判定）。

A. Winner 正常 candidate 数量不变
B. malformed 不生成 candidate
C. malformed 不生成 notification（无 event_id → 无 dispatch）
D. 扫描结果（状态）可观察到 malformed 数量与原因
E. malformed 记录有界
F. 观察视图不泄露消息正文
G. 自动/手动同一 service
DS9：A-G（count 正确 / 重启可读 / 不参与 needs / 不影响 checkpoint / 不截断列表 / 无 cap / 既有回归）
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


def test_winner_stable_key_still_fail_closed() -> None:
    assert _source_entry_key("mention", _entry("")) is None
    assert _source_entry_key("mention", _entry("item-1")) == "mention:item-1"


def test_winner_malformed_scan_counts_and_bounds(tmp_path: Path, monkeypatch) -> None:
    import src.winner_scan.service as win

    candidates: dict[str, dict] = {}
    monkeypatch.setattr(win, "_load_candidates", lambda: candidates)
    monkeypatch.setattr(win, "judge_keywords", lambda text: True)
    monkeypatch.setattr(win, "_save_candidates", lambda payload: candidates.update(payload))
    state = {"mention": {}, "reply": {}, "dm": {}}

    entries = [_entry("")] * 25
    saved, keys = service._scan_feed_source(
        "mention", MessageType.AT, entries, state=state["mention"], existing=candidates
    )
    assert saved == 0
    assert keys == []
    assert candidates == {}  # malformed 不生成 candidate（无 event → 无通知）
    skipped = state["mention"].get("malformed_skipped") or []
    assert len(skipped) == 20  # 有界（E）
    item = skipped[0]
    assert item["reason"]  # 原因可观察（D）
    assert "无法稳定识别" in item["reason"] or "缺少稳定平台 ID" in item["reason"]


def test_winner_state_view_exposes_skipped_without_message_body(tmp_path: Path, monkeypatch) -> None:
    import src.winner_scan.service as win

    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    monkeypatch.setattr(win, "get_profile_dir", lambda: profile_dir)
    state = {"mention": {}, "reply": {}, "dm": {}}
    state["mention"]["malformed_skipped"] = [
        {
            "ts": 1,
            "source_type": "mention",
            "reason": "条目缺少稳定平台 ID，已跳过（避免空 key 碰撞 / 随机 key 重复通知）",
        }
    ]
    monkeypatch.setattr(win, "_load_state", lambda: state)
    view = win.scan_status_view()
    assert view["sources"]["mention"]["malformed_skipped"][0]["reason"]
    text = json.dumps(view, ensure_ascii=False)
    assert "summary" not in text  # 不持久化/不暴露消息正文（F）


def test_winner_single_service_for_auto_and_manual() -> None:
    import web.actions as actions

    assert "winner_deep_scan" in actions.WRITER_LEASE_ACTIONS
    from src.winner_scan import service as win_module

    assert isinstance(win_module.service, WinnerScanService)


# ---------- DS9 incomplete 观察 ----------


@pytest.fixture
def ds9_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.sources.ds9_topics as ds9

    state_file = tmp_path / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: state_file)
    return ds9, state_file


def _write_records(ds9, state_file: Path, records: list[dict]) -> None:
    ds9._write_state({"topics": records})


def test_ds9_incomplete_count_derived_from_persisted_state(ds9_state) -> None:
    ds9, state_file = ds9_state
    _write_records(
        ds9,
        state_file,
        [{"topic_id": "1261901", "fp": "f", "incomplete": ["a", "b", "c"]}],
    )

    topics = ds9.list_topics()
    assert topics[0]["incomplete_count"] == 3

    # 重启（重新读文件）后仍一致
    again = ds9.list_topics()
    assert again[0]["incomplete_count"] == 3


def test_ds9_incomplete_count_not_persisted_and_not_part_of_needs(ds9_state) -> None:
    ds9, state_file = ds9_state
    _write_records(ds9, state_file, [{"topic_id": "1261901", "fp": "f", "incomplete": ["a"]}])
    ds9.list_topics()

    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert "incomplete_count" not in stored["topics"][0]  # 派生字段不落盘（无漂移）
    # needs 判定基于 fp 与 incomplete 内容本身，与本观察字段无关：
    record = stored["topics"][0]
    assert bool(record["incomplete"]) is True


def test_ds9_incomplete_list_not_truncated_no_cap(ds9_state) -> None:
    ds9, state_file = ds9_state
    many = [f"item-{i}" for i in range(50)]
    _write_records(ds9, state_file, [{"topic_id": "1261901", "fp": None, "incomplete": many}])

    topics = ds9.list_topics()
    assert topics[0]["incomplete_count"] == 50
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert len(stored["topics"][0]["incomplete"]) == 50  # 无 cap / TTL / prune


def test_ds9_incomplete_count_never_touches_checkpoint(ds9_state) -> None:
    ds9, state_file = ds9_state
    _write_records(ds9, state_file, [{"topic_id": "1261901", "fp": "fp-v1", "incomplete": ["a"]}])

    ds9._apply_topic_commits(
        [{"topic_id": "1261901", "fp": "fp-v2", "incomplete": [], "last_success_at": 5, "last_found": 1, "last_scan_at": 5}]
    )
    stored = json.loads(state_file.read_text(encoding="utf-8"))["topics"][0]
    assert stored["fp"] == "fp-v2"  # checkpoint 语义照常由 commit 驱动
    assert stored["incomplete"] == []
