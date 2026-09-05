"""关注动态补漏 V1（following feed）。

设计约束（V1 收敛版）：
- 只扫“当前登录账号可见的关注动态 Feed”最近一页，绝不逐个访问关注 UP 主页。
- 每轮最多 1 页、约 30 条（以单页实际返回为上限，超出截断）。
- 使用动态 ID 做 checkpoint；只有“正常完成一轮”才推进 last_seen_dynamic_id。
- 风控（-352/-509/429/rate-limit/risk-control/风控/限流）向上抛，由 AutoScheduler 走全局 6h 冷却；
  不把风控当成“空 Feed / 0 条”。
- 复用现有 run_new_links_pipeline（含去重、分类、详情、明确已结束过滤、入库），不另造分类器。
- 开关默认 OFF，本地 per-Profile JSON；开关动作 0 远程。
- 不升 DB Schema（v10 不变）。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from src.bilibili_client import BilibiliClient
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from src.sources.common import opus_link

FOLLOW_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
FOLLOW_FEED_REFERER = "https://t.bilibili.com/"

# 第一版保守上限：单页最多处理 30 条（少于单页则按实际返回）。
FOLLOW_FEED_MAX_ITEMS = 30

_VALID_DYNAMIC_ID = re.compile(r"^\d{18,19}$")

_FILE_NAME = "following_feed_state.json"


def matches_platform_risk(message: object) -> bool:
    """兼容包装：统一走 src.platform_risk 结构化判定，绝不扫描业务正文。"""
    from src.platform_risk import matches_platform_risk as _risk

    return _risk(message)


def _state_path() -> Path:
    from src.data_paths import get_profile_dir

    return get_profile_dir() / _FILE_NAME


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return raw


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_FILE_NAME}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def following_feed_info() -> dict[str, Any]:
    state = _read_state()
    return {
        "last_scan_at": state.get("last_scan_at"),
        "last_seen_dynamic_id": state.get("last_seen_dynamic_id"),
        "last_found": state.get("last_found"),
        "last_added": state.get("last_added"),
        "last_status": str(state.get("last_status") or "waiting"),
        "last_error": str(state.get("last_error") or ""),
    }


def _item_ids(payload: object) -> list[str]:
    """保守解析 feed/all：优先 data.items[] 的 id_str；非合法动态 ID 一律忽略。

    本轮不假定一定需要翻页参数/offset；V1 只处理第一页。
    """
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else payload.get("items")
    if not isinstance(items, list):
        return []
    ids: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        dynamic_id = str(item.get("id_str") or "").strip()
        if not _VALID_DYNAMIC_ID.fullmatch(dynamic_id):
            continue
        if dynamic_id in seen:
            continue
        seen.add(dynamic_id)
        ids.append(dynamic_id)
    return ids


def _slice_before_checkpoint(ids: list[str], checkpoint: str | None) -> list[str]:
    """在 checkpoint（含）之前截断，只留下“尚未推进到 checkpoint”的新动态。"""
    if not checkpoint:
        return ids
    out: list[str] = []
    for dynamic_id in ids:
        if dynamic_id == checkpoint:
            break
        out.append(dynamic_id)
    return out


def next_following_scan_at(
    now_ts: int | None = None,
    *,
    hour_interval: int = 6,
    hour_offset: int = 1,
    minute: int = 3,
) -> int | None:
    """按固定 cadence（每 hour_interval 小时、hour%interval==hour_offset、minute 分）计算下一次时刻。"""
    from datetime import datetime, timedelta, timezone

    now = int(time.time()) if now_ts is None else int(now_ts)
    tz = timezone(timedelta(hours=8))
    base = datetime.fromtimestamp(now, tz).replace(second=0, microsecond=0)
    for offset_min in range(1, 24 * 60 * 7 + 1):
        candidate = base + timedelta(minutes=offset_min)
        if (
            candidate.minute == minute
            and candidate.hour % hour_interval == hour_offset
        ):
            return int(candidate.timestamp())
    return None


def following_feed_summary(now_ts: int | None = None) -> dict[str, Any]:
    """紧凑只读摘要：最近状态/统计 + 下次自动扫描时间（本地 0 远程）。"""
    info = following_feed_info()
    info["next_scan_at"] = next_following_scan_at(now_ts)
    return info


def scan_following_feed(
    *,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, Any]:
    """执行一轮关注动态补漏（V1：仅最近一页）。

    返回统计 dict；风险异常向上抛（不写 checkpoint）。
    """
    state = _read_state()
    if on_progress is not None:
        on_progress(0, 1, "正在读取关注动态 Feed…")

    with client_factory() as client:
        payload = client.get_json(
            FOLLOW_FEED_URL,
            {"type": "all"},
            referer=FOLLOW_FEED_REFERER,
            retries=0,
        )

    # 平台风控只会以「结构化返回码」或「客户端异常信封」出现，正文绝不参与判定。
    if not isinstance(payload, dict):
        raise RuntimeError("关注动态 Feed 响应格式异常")
    code = payload.get("code")
    if code is not None and code != 0:
        message = str(payload.get("message") or payload.get("msg") or "")
        if code in (-352, -509):
            raise RuntimeError(f"API error {code}: {message}")
        raise RuntimeError(f"关注动态 Feed 响应异常（code={code}：{message}）")

    def fail(message: str) -> dict[str, Any]:
        now = int(time.time())
        state["last_status"] = "failed"
        state["last_error"] = message
        state["last_scan_at"] = now
        state["last_found"] = 0
        state["last_added"] = 0
        _write_state(state)
        if on_progress is not None:
            on_progress(1, 1, message)
        return {
            "skipped": False,
            "status": "failed",
            "scanned_items": 0,
            "found": 0,
            "added": 0,
            "message": message,
        }

    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else payload.get("items")
    if not isinstance(data, dict) or not isinstance(items, list):
        return fail("关注动态 Feed 响应缺少有效 data.items 列表，无法解析")
    if items and not _item_ids(payload):
        return fail("关注动态 Feed 返回非空列表但无法解析出任何有效动态 ID")

    page_ids = _item_ids(payload)[:FOLLOW_FEED_MAX_ITEMS]
    checkpoint = state.get("last_seen_dynamic_id")
    processed = _slice_before_checkpoint(page_ids, str(checkpoint) if checkpoint else None)

    scan_started_at = int(time.time())
    urls = [opus_link(dynamic_id) for dynamic_id in processed]
    if on_progress is not None:
        on_progress(0, max(1, len(urls)), f"关注动态共 {len(urls)} 条新候选，正在复用公共流水线…")

    pipeline = run_new_links_pipeline(
        urls,
        workers=1,
        scan_started_at=scan_started_at,
        on_progress=on_progress,
    )

    found = len(processed)
    added = int(getattr(pipeline, "persisted_count", 0) or 0)
    now = int(time.time())

    # 只有正常完成整轮才推进 checkpoint；风险异常在更早处抛出而未写盘。
    new_checkpoint = processed[0] if processed else (checkpoint or None)
    state["last_seen_dynamic_id"] = new_checkpoint
    state["last_scan_at"] = now
    state["last_found"] = found
    state["last_added"] = added
    state["last_status"] = "success"
    state["last_error"] = ""
    _write_state(state)

    if on_progress is not None:
        on_progress(1, 1, f"关注动态补漏完成：发现 {found} 条，新增 {added} 条")

    return {
        "skipped": False,
        "status": "success",
        "scanned_items": len(page_ids),
        "found": found,
        "added": added,
        "new_link_count": int(getattr(pipeline, "new_link_count", 0) or 0),
        "message": f"关注动态补漏完成：发现 {found} 条，新增 {added} 条",
    }
