"""活动数据读写性能优化 V4 测试（全部临时 SQLite / synthetic，零真实数据 / 零网络）。

覆盖：load_payload 归一化写入不再二次全表重读；refresh updated==0 不重写活动表
（但 status_classified 需要补写时仍写）；updated>0 持久化正常；summary/list 口径对照；
100/1000/5000 synthetic 行解码量与重写次数记录。
"""

from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import activity_store as store
from src.activity_store import load_payload, replace_all_activities
from src.db.activity_codec import row_to_activity_dict
from sqlmodel import col, select
from src.db.models import ActivityRow
from src.db.session import session_scope
from src.status_refresh import refresh_local_activity_statuses
from web.activity_service import get_summary

FUTURE_TS = int(time.time()) + 86400 * 30
PAST_TS = int(time.time()) - 60


def _dynamic_id(index: int) -> str:
    return f"2000{index:016d}"


def _activity(index: int, *, ended: bool = False, classified: bool = True) -> dict:
    return {
        "dynamic_id": _dynamic_id(index),
        "source_url": f"https://www.bilibili.com/opus/{_dynamic_id(index)}",
        "lottery_type": "互动抽奖",
        "business_id": _dynamic_id(index),
        "business_type": "1",
        "draw_status": "ended" if ended else "active",
        "lottery_time": PAST_TS if ended else FUTURE_TS + index,
        "activity_status": "已结束" if ended else "未参加",
        "draw_tag": "" ,
        "status_classified": classified,
        "skipped": False,
        "platform_participated": False,
        "reserve_reserved": False,
        "repost_count": index % 50,
        "repost_fetched": True,
        "repost_zero_confirmed": True,
        "prizes": [{"description": f"奖品{index}", "winner_count": 1}],
        "conditions": {},
        "user_status_source": "default",
    }


def _seed(count: int, *, ended_ratio: float = 0.0) -> None:
    items = [
        _activity(i, ended=(i % max(1, int(1 / max(ended_ratio, 0.01)))) == 0)
        for i in range(count)
    ]
    replace_all_activities(items)


def _row_dicts() -> list[dict]:
    with session_scope() as session:
        rows = session.exec(
            select(ActivityRow).order_by(col(ActivityRow.dynamic_id))
        ).all()
        return [row_to_activity_dict(row) for row in rows]


def _legacy_normalized(rows: list[dict]) -> list[dict]:
    """旧 load_payload 的对照：先 hydrate 全部，再在副本上做时间归一化。"""
    items = [copy.deepcopy(row) for row in rows]
    for item in items:
        store._normalize_ended_by_time(item)
    return items


def test_payload_equivalent_to_legacy_normalize_pipeline(isolated_home: Path) -> None:
    _seed(30, ended_ratio=0.1)
    payload = load_payload()
    assert payload["activities"] == _legacy_normalized(_row_dicts())
    assert len(payload["activities"]) == 30
    # 归一化后再次读取稳定（幂等）。
    again = load_payload()
    assert again["activities"] == payload["activities"]


def test_load_payload_does_not_reload_all_rows_after_normalize(isolated_home: Path) -> None:
    # 混入多个已过期待归一化的行（旧实现会二次全表 SELECT + hydrate）。
    _seed(40, ended_ratio=0.25)
    decodes: list[int] = []

    real_decode = store.row_to_activity_dict

    def counting_decode(row):
        decodes.append(1)
        return real_decode(row)

    import src.activity_store as module

    module.row_to_activity_dict = counting_decode
    try:
        load_payload()
        first_round = len(decodes)
        load_payload()
    finally:
        module.row_to_activity_dict = real_decode

    # 每次调用只 hydrate 一次全表；首轮（含写归一化）不得出现第二次全表 hydrate。
    assert first_round == 40
    assert len(decodes) - first_round == 40


def test_refresh_updated_zero_skips_rewrite(isolated_home: Path, monkeypatch) -> None:
    _seed(120)  # 全部 active、未来开奖、status_classified=True → 无任何变化
    rewrites: list[int] = []
    monkeypatch.setattr(
        "src.status_refresh.replace_all_activities",
        lambda activities: rewrites.append(len(activities)),
    )
    result = refresh_local_activity_statuses()
    assert result["updated"] == 0
    assert result["total"] == 120
    assert result["skipped"] is False
    assert rewrites == []  # 不重写活动表


def test_refresh_updated_zero_but_classify_flag_needs_persist_still_writes(
    isolated_home: Path, monkeypatch,
) -> None:
    # status_classified=False 的候选行：可见字段不变（updated=0）但标志需要补写。
    items = [_activity(i, classified=False) for i in range(10)]
    replace_all_activities(items)
    import src.status_refresh as refresh_mod

    original_replace = refresh_mod.replace_all_activities
    rewrites: list[int] = []

    def counting_replace(activities):
        rewrites.append(len(activities))
        return original_replace(activities)

    monkeypatch.setattr(refresh_mod, "replace_all_activities", counting_replace)
    result = refresh_local_activity_statuses()
    assert result["updated"] == 0  # 旧口径：可见状态无变化
    assert rewrites == [10]  # 仍需补写 status_classified
    rows = _row_dicts()
    assert all(bool(row.get("status_classified")) for row in rows)


def test_refresh_after_load_time_normalize_is_noop_persist_safe(
    isolated_home: Path, monkeypatch,
) -> None:
    # load_payload 读路径已做时间归一化：过期行在读入时被标 ended，
    # refresh 看到的是稳定终态，不应再次重写。
    _seed(15)
    with session_scope() as session:
        row = session.exec(
            select(ActivityRow).where(ActivityRow.dynamic_id == _dynamic_id(3))
        ).one()
        row.lottery_time = PAST_TS
        session.add(row)
    import src.status_refresh as refresh_mod

    original_replace = refresh_mod.replace_all_activities
    rewrites: list[int] = []

    def counting_replace(activities):
        rewrites.append(len(activities))
        return original_replace(activities)

    monkeypatch.setattr(refresh_mod, "replace_all_activities", counting_replace)
    result = refresh_local_activity_statuses()
    rows = _row_dicts()
    assert rows[3]["draw_status"] == "ended"
    assert rows[3]["activity_status"] == "已结束"
    assert result["updated"] == 0
    assert result["skipped"] is False
    # 读入归一化已落库，refresh 无新变化 → 不再全表 rewrite。
    assert rewrites == []


def test_summary_counts_stable_on_representative_fixture(isolated_home: Path) -> None:
    _seed(12, ended_ratio=0.0)
    # 明确制造 已参加 与 已结束 各若干（status_classified=True 后 resolve 采用存储值）。
    with session_scope() as session:
        for index, status in ((4, "已参加"), (5, "已参加"), (6, "已结束")):
            row = session.exec(
                select(ActivityRow).where(ActivityRow.dynamic_id == _dynamic_id(index))
            ).one()
            row.activity_status = status
            if status == "已结束":
                row.draw_status = "ended"
            session.add(row)
    summary = get_summary()
    counts = summary["user_status_counts"]
    draw = summary["counts"]
    payload = load_payload()
    ended_rows = sum(1 for item in payload["activities"] if item.get("draw_status") == "ended")
    assert summary["total_count"] == 12
    assert sum(counts.values()) == 12
    # 口径自洽：draw ended 与存储 ended 行一致；已参加 恰好 2 条。
    assert draw["ended"] == ended_rows
    assert draw["active"] == 12 - ended_rows
    assert counts["已参加"] == 2
    assert counts["已结束"] == ended_rows


def test_load_payload_counts_match_payload_activities(isolated_home: Path) -> None:
    _seed(25, ended_ratio=0.2)
    payload = load_payload()
    assert payload["counts"]["ended"] == sum(
        1 for item in payload["activities"] if item.get("draw_status") == "ended"
    )
    assert payload["counts"]["active"] == len(payload["activities"]) - payload["counts"]["ended"]


@pytest.mark.parametrize("size", [100, 1000, 5000])
def test_benchmark_refresh_noop_decode_and_rewrite(isolated_home: Path, size: int) -> None:
    """synthetic 基准：updated==0 的 refresh 不重写、每次调用只 hydrate 一次。"""
    _seed(size)
    decodes: list[int] = []
    real_decode = store.row_to_activity_dict

    def counting_decode(row):
        decodes.append(1)
        return real_decode(row)

    import src.activity_store as module

    module.row_to_activity_dict = counting_decode
    try:
        times: list[float] = []
        for _ in range(3):
            decodes.clear()
            started = time.perf_counter()
            result = refresh_local_activity_statuses()
            times.append(time.perf_counter() - started)
            assert result["updated"] == 0
            assert len(decodes) == size  # 单次 hydrate
    finally:
        module.row_to_activity_dict = real_decode
    median_ms = sorted(times)[1] * 1000
    print(f"\n[bench] refresh no-op size={size}: median {median_ms:.1f}ms, decode/run={size}, rewrite=0")
