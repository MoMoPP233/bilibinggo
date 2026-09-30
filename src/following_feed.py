"""关注动态补漏（following feed）。

设计约束（V1 收敛版）：
- 只扫“当前登录账号可见的关注动态 Feed”，绝不逐个访问关注 UP 主页。
- 每轮从 page=1 开始，在同一轮内按 endpoint 契约继续读取，最多 20 页。
- 使用动态 ID 做 checkpoint；只有“正常完成一轮”才推进 last_seen_dynamic_id。
- 风控（-352/-509/429/rate-limit/risk-control/风控/限流）向上抛，由 AutoScheduler 走全局 6h 冷却；
  不把风控当成“空 Feed / 0 条”。
- 复用现有 run_new_links_pipeline（含去重、分类、详情、明确已结束过滤、入库），不另造分类器。
- Following Feed 只补当前账号关注 Feed 中的非 forward 原创动态；自身动态与所有转发动态均跳过。
- checkpoint 推进语义（V16）：只要本轮存在“仍需要以后重试的未完成候选”
  （正文不可读取），就保留进入本轮前的旧 checkpoint，让该候选下一轮可再次进入流水线；
  终态安全项（404 链接失效、充电/非抽奖等 skip、详情提取失败、扫描开始前已结束过滤、
  DB 去重命中）不阻塞推进，坏动态只隔离自身、不拖垮整轮。
- 开关默认 OFF，本地 per-Profile JSON；开关动作 0 远程。
- 不升 DB Schema（v10 不变）。
"""

from __future__ import annotations

import json
import inspect
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from src.bilibili_client import BilibiliClient
from src.bilibili_auth import get_login_uid
from src.canonical_identity import CanonicalIdentity, resolve_dynamic_identity
from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from src.sources.common import opus_link

FOLLOW_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/all"
FOLLOW_FEED_REFERER = "https://t.bilibili.com/"

# 同轮分页最多读取 20 页。旧 payload（没有分页字段）保留历史 30 条兼容上限。
FOLLOW_FEED_MAX_PAGES = 20
# Compatibility cap for legacy callers/fixtures that predate the pagination
# fields.  Real pagination responses carry ``has_more``/``offset`` and are
# collected across the full page window.
FOLLOW_FEED_MAX_ITEMS = 30
ANCHOR_LIMIT = 50

_VALID_DYNAMIC_ID = re.compile(r"^\d{18,19}$")

_FILE_NAME = "following_feed_state.json"

# 仍需以后重试的失败：动态仍可能存在，只是本轮未能成功处理完毕。
# checkpoint 不得越过这类候选。其余 pipeline skip 均属终态安全项，不阻塞推进。
CHECKPOINT_BLOCKING_SKIP_REASONS = frozenset({"正文不可读取"})


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


def _read_state_checked() -> tuple[dict[str, Any], bool, str]:
    """Return state, whether it is valid, and a safe diagnostic reason.

    A missing file is a legal first run.  Existing malformed JSON/schema is
    fail-closed: callers must not issue a feed request or overwrite it.
    """
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, True, ""
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}, False, "following_feed_state_corrupt"
    if not isinstance(raw, dict):
        return {}, False, "following_feed_state_corrupt"
    checkpoint = raw.get("last_seen_dynamic_id")
    if checkpoint is not None and (
        not isinstance(checkpoint, str) or not _VALID_DYNAMIC_ID.fullmatch(checkpoint)
    ):
        return {}, False, "following_feed_state_corrupt"
    anchors = raw.get("anchors")
    if anchors is not None:
        if not isinstance(anchors, list):
            return {}, False, "following_feed_state_corrupt"
        if any(
            not isinstance(value, str) or not _VALID_DYNAMIC_ID.fullmatch(value)
            for value in anchors
        ):
            return {}, False, "following_feed_state_corrupt"
    return raw, True, ""


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
    state, valid, reason = _read_state_checked()
    if not valid:
        return {
            "last_scan_at": None,
            "last_seen_dynamic_id": None,
            "last_found": 0,
            "last_added": 0,
            "accepted": 0,
            "inserted": 0,
            "last_status": "failed",
            "last_error": reason,
            "state_corrupt": True,
            "anchors": [],
        }
    return {
        "last_scan_at": state.get("last_scan_at"),
        "last_seen_dynamic_id": state.get("last_seen_dynamic_id"),
        "last_found": state.get("last_found"),
        "last_added": state.get("last_added"),
        "accepted": int(state.get("accepted") or 0),
        "inserted": int(state.get("inserted") or 0),
        "last_status": str(state.get("last_status") or "waiting"),
        "last_error": str(state.get("last_error") or ""),
        "self_skipped": int(state.get("self_skipped") or 0),
        "forward_unresolved_skipped": int(
            state.get("forward_unresolved_skipped") or 0
        ),
        "forward_skipped": int(state.get("forward_skipped") or 0),
        "canonical_duplicate_skipped": int(
            state.get("canonical_duplicate_skipped") or 0
        ),
        "anchors": list(state.get("anchors") or []),
        "last_scan_status": state.get("last_scan_status"),
        "last_scan_at": state.get("last_scan_at"),
        "last_pages_fetched": int(state.get("last_pages_fetched") or 0),
        "last_stop_reason": state.get("last_stop_reason"),
        "last_items_seen": int(state.get("last_items_seen") or 0),
        "last_unique_items": int(state.get("last_unique_items") or 0),
        "last_new_items": int(state.get("last_new_items") or 0),
        "last_anchor_hits": int(state.get("last_anchor_hits") or 0),
        "last_catch_up_complete": state.get("last_catch_up_complete"),
    }


def _feed_items(payload: object) -> list[tuple[str, dict[str, Any] | None]]:
    """Return valid IDs with their first-party feed item when available."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    items = data.get("items") if isinstance(data, dict) else payload.get("items")
    if not isinstance(items, list):
        return []
    result: list[tuple[str, dict[str, Any] | None]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        dynamic_id = str(item.get("id_str") or item.get("dynamic_id") or "").strip()
        if not _VALID_DYNAMIC_ID.fullmatch(dynamic_id) or dynamic_id in seen:
            continue
        seen.add(dynamic_id)
        result.append((dynamic_id, item))
    return result


def _item_ids(payload: object) -> list[str]:
    """保守解析 feed/all：优先 data.items[] 的 id_str；非合法动态 ID 一律忽略。
    """
    return [dynamic_id for dynamic_id, _ in _feed_items(payload)]


def _pipeline_accepts_identity_map() -> bool:
    try:
        signature = inspect.signature(run_new_links_pipeline)
    except (TypeError, ValueError):
        return False
    return "identity_by_raw" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


def _pipeline_accepts_source_observations() -> bool:
    try:
        signature = inspect.signature(run_new_links_pipeline)
    except (TypeError, ValueError):
        return False
    return "source_observations" in signature.parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )


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
    hour_interval: int | None = None,
    hour_offset: int | None = None,
    minute: int | None = None,
) -> int | None:
    """计算下一次关注动态扫描；默认使用 V72 明确固定时刻。"""
    from datetime import datetime, timedelta, timezone
    from web.auto_config import FOLLOWING_FEED_SCAN_HOURS, FOLLOWING_FEED_SCAN_MINUTE

    now = int(time.time()) if now_ts is None else int(now_ts)
    tz = timezone(timedelta(hours=8))
    base = datetime.fromtimestamp(now, tz).replace(second=0, microsecond=0)
    for offset_min in range(1, 24 * 60 * 7 + 1):
        candidate = base + timedelta(minutes=offset_min)
        if hour_interval is None and hour_offset is None and minute is None:
            matches = (
                candidate.minute == FOLLOWING_FEED_SCAN_MINUTE
                and candidate.hour in FOLLOWING_FEED_SCAN_HOURS
            )
        else:
            if hour_interval is None or hour_offset is None or minute is None:
                raise ValueError("hour_interval/hour_offset/minute 必须同时提供")
            matches = candidate.minute == minute and candidate.hour % hour_interval == hour_offset
        if matches:
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
    """执行一轮同轮分页的关注动态增量扫描。

    Feed 的 offset 只存在本次调用的局部变量中；只有收集窗口完整且公共
    pipeline 完成后，才会一次性推进 checkpoint/anchors。
    """
    state, state_valid, state_reason = _read_state_checked()
    if not state_valid:
        # 损坏文件必须保持原字节不变，且绝不发起远程请求。
        return {
            "skipped": False,
            "status": "failed",
            "scanned_items": 0,
            "found": 0,
            "added": 0,
            "message": state_reason,
            "state_corrupt": True,
        }

    checkpoint = str(state.get("last_seen_dynamic_id") or "") or None
    old_anchors = [
        str(value)
        for value in (state.get("anchors") or [])
        if _VALID_DYNAMIC_ID.fullmatch(str(value))
    ]
    if checkpoint and checkpoint not in old_anchors:
        old_anchors.insert(0, checkpoint)
    old_anchors = old_anchors[:ANCHOR_LIMIT]
    # An empty state (or a missing file) establishes a baseline from page 1;
    # a previously incomplete run must continue paging on its next run.
    first_run = not checkpoint and not old_anchors and not state.get("last_scan_status")

    def record_diagnostic(
        *,
        status: str,
        message: str,
        stop_reason: str,
        pages_fetched: int,
        items_seen: int,
        unique_items: int,
        anchor_hits: int,
        catch_up_complete: bool,
    ) -> dict[str, Any]:
        diagnostic = dict(state)
        diagnostic.update(
            {
                "last_scan_status": status,
                "last_status": "failed" if status == "failed" else "incomplete",
                "last_error": message,
                "last_scan_at": int(time.time()),
                "last_pages_fetched": pages_fetched,
                "last_stop_reason": stop_reason,
                "last_items_seen": items_seen,
                "last_unique_items": unique_items,
                "last_new_items": 0,
                "last_anchor_hits": anchor_hits,
                "last_catch_up_complete": catch_up_complete,
            }
        )
        _write_state(diagnostic)
        if on_progress is not None:
            on_progress(1, 1, message)
        return {
            "skipped": False,
            "status": "failed" if status == "failed" else "incomplete",
            "scanned_items": unique_items,
            "found": 0,
            "added": 0,
            "pages_fetched": pages_fetched,
            "catch_up_complete": catch_up_complete,
            "stop_reason": stop_reason,
            "message": message,
        }

    def validate_page(payload: object, page_no: int) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any] | None]], str, bool]:
        # 风控/业务错误只依据结构化 code；正文内容不参与风险判定。
        if not isinstance(payload, dict):
            raise RuntimeError("关注动态 Feed 响应格式异常")
        code = payload.get("code")
        if code is not None and code != 0:
            message = str(payload.get("message") or payload.get("msg") or "")
            if code in (-352, -509):
                raise RuntimeError(f"API error {code}: {message}")
            raise RuntimeError(f"关注动态 Feed 响应异常（code={code}：{message}）")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("items"), list):
            raise ValueError("关注动态 Feed 响应缺少有效 data.items 列表，无法解析")
        page_items = _feed_items(payload)
        if data.get("items") and not page_items:
            raise ValueError("关注动态 Feed 返回非空列表但无法解析出任何有效动态 ID")
        has_more_value = data.get("has_more", False)
        if not isinstance(has_more_value, bool):
            raise ValueError(f"第 {page_no} 页 has_more 不是 bool")
        offset_value = data.get("offset", "")
        if offset_value is None:
            offset_value = ""
        if not isinstance(offset_value, str):
            raise ValueError(f"第 {page_no} 页 offset 不是 string")
        return data, page_items, offset_value.strip(), has_more_value

    if on_progress is not None:
        on_progress(0, 1, "正在读取关注动态 Feed…")

    all_ids: list[str] = []
    item_by_id: dict[str, dict[str, Any] | None] = {}
    page_by_id: dict[str, int] = {}
    seen_dynamic_ids: set[str] = set()
    seen_offsets: set[str] = set()
    pages_fetched = 0
    items_seen = 0
    anchor_hits = 0
    boundary_index: int | None = None
    stop_reason = ""
    catch_up_complete = False
    next_offset: str | None = None

    with client_factory() as client:
        for page_no in range(1, FOLLOW_FEED_MAX_PAGES + 1):
            params: dict[str, object] = {"type": "all", "page": page_no}
            if page_no > 1:
                params["offset"] = next_offset or ""
            payload = client.get_json(
                FOLLOW_FEED_URL,
                params,
                referer=FOLLOW_FEED_REFERER,
                retries=0,
            )
            pages_fetched = page_no
            try:
                data, page_items, offset, has_more = validate_page(payload, page_no)
            except ValueError as exc:
                return record_diagnostic(
                    status="failed" if page_no == 1 else "incomplete",
                    message=str(exc),
                    stop_reason="response_invalid",
                    pages_fetched=pages_fetched,
                    items_seen=items_seen,
                    unique_items=len(all_ids),
                    anchor_hits=anchor_hits,
                    catch_up_complete=False,
                )
            items_seen += len(data.get("items") or [])
            if page_no == 1 and "has_more" not in data and "offset" not in data:
                page_items = page_items[:FOLLOW_FEED_MAX_ITEMS]
            page_new_ids: list[str] = []
            page_anchor_positions: list[int] = []
            for dynamic_id, item in page_items:
                if dynamic_id in seen_dynamic_ids:
                    continue
                seen_dynamic_ids.add(dynamic_id)
                all_ids.append(dynamic_id)
                item_by_id[dynamic_id] = item
                page_by_id[dynamic_id] = page_no
                page_new_ids.append(dynamic_id)
                if dynamic_id in old_anchors:
                    anchor_hits += 1
                    page_anchor_positions.append(len(all_ids) - 1)
            exact_checkpoint_hit = checkpoint in page_new_ids if checkpoint else False
            required_anchor_hits = min(3, len(set(old_anchors))) if old_anchors else 0
            if exact_checkpoint_hit:
                boundary_index = all_ids.index(checkpoint)  # exclude checkpoint itself
                stop_reason = "checkpoint_reached"
                catch_up_complete = True
            elif required_anchor_hits and anchor_hits >= required_anchor_hits:
                boundary_index = min(page_anchor_positions) if page_anchor_positions else len(all_ids)
                stop_reason = "anchor_reached"
                catch_up_complete = True
            elif not has_more:
                stop_reason = "has_more_false"
                catch_up_complete = True
            elif first_run and page_no == 1:
                stop_reason = "first_run_baseline"
                catch_up_complete = True
            elif not offset:
                stop_reason = "missing_next_offset"
                break
            elif offset in seen_offsets:
                stop_reason = "repeated_offset"
                break
            elif page_no >= FOLLOW_FEED_MAX_PAGES:
                stop_reason = "hard_cap_reached"
                break
            else:
                seen_offsets.add(offset)
                next_offset = offset
                continue
            break

    if not catch_up_complete:
        return record_diagnostic(
            status="incomplete",
            message=f"关注动态分页未完整结束：{stop_reason}",
            stop_reason=stop_reason,
            pages_fetched=pages_fetched,
            items_seen=items_seen,
            unique_items=len(all_ids),
            anchor_hits=anchor_hits,
            catch_up_complete=False,
        )

    if boundary_index is None:
        processed = list(all_ids)
    else:
        processed = all_ids[:boundary_index]

    # Following Feed is already scoped by the platform to the current account's
    # followed-feed.  V33 deliberately does not inspect relationship fields or
    # chase repost originals: only non-forward originals are in this source's
    # scope.
    current_uid = get_login_uid()
    identity_by_raw: dict[str, CanonicalIdentity] = {}
    for dynamic_id in processed:
        item = item_by_id.get(dynamic_id)
        if not isinstance(item, dict) or not ("type" in item or "modules" in item):
            continue
        identity = resolve_dynamic_identity(item, dynamic_id)
        identity_by_raw[dynamic_id] = identity
    if identity_by_raw and current_uid is None:
        return record_diagnostic(
            status="failed",
            message="无法确认当前账号 UID，已安全跳过本轮且未推进 checkpoint",
            stop_reason="missing_login_uid",
            pages_fetched=pages_fetched,
            items_seen=items_seen,
            unique_items=len(all_ids),
            anchor_hits=anchor_hits,
            catch_up_complete=catch_up_complete,
        )

    self_skipped = 0
    forward_unresolved_skipped = 0
    forward_skipped = 0
    canonical_duplicate_skipped = 0
    accepted_ids: list[str] = []
    accepted_identities: dict[str, CanonicalIdentity] = {}
    canonical_seen: set[str] = set()
    for dynamic_id in processed:
        identity = identity_by_raw.get(dynamic_id)
        # Older feed fixtures only contain id_str.  Keep their legacy input
        # shape; detailed pipeline resolution still applies to those IDs.
        if identity is None:
            accepted_ids.append(dynamic_id)
            continue
        if current_uid is not None and identity.wrapper_author_uid == str(current_uid):
            self_skipped += 1
            if identity.is_forward:
                forward_skipped += 1
            continue
        if identity.is_forward:
            forward_skipped += 1
            if identity.unresolved_reason:
                forward_unresolved_skipped += 1
            continue
        canonical_id = identity.canonical_dynamic_id
        if canonical_id and canonical_id in canonical_seen:
            # The public pipeline also dedupes after resolution; this local
            # check avoids a needless classification request for the wrapper.
            canonical_duplicate_skipped += 1
            continue
        if canonical_id:
            canonical_seen.add(canonical_id)
        accepted_ids.append(dynamic_id)
        accepted_identities[dynamic_id] = identity

    scan_started_at = int(time.time())
    urls = [opus_link(dynamic_id) for dynamic_id in accepted_ids]
    if on_progress is not None:
        on_progress(0, max(1, len(urls)), f"关注动态共 {len(urls)} 条新候选，正在复用公共流水线…")

    pipeline_kwargs: dict[str, Any] = {
        "workers": 1,
        "scan_started_at": scan_started_at,
        "on_progress": on_progress,
    }
    if _pipeline_accepts_source_observations():
        pipeline_kwargs["source_observations"] = [
            {
                "source_id": "following_feed",
                "dynamic_id": dynamic_id,
                "page_index": page_by_id.get(dynamic_id),
            }
            for dynamic_id in accepted_ids
        ]
    if accepted_identities and _pipeline_accepts_identity_map():
        pipeline_kwargs["identity_by_raw"] = accepted_identities
    pipeline = run_new_links_pipeline(urls, **pipeline_kwargs)

    found = len(processed)
    added = int(getattr(pipeline, "persisted_count", 0) or 0)
    now = int(time.time())

    # checkpoint 语义（V16）：
    # 1) 本轮没有任何“仍需以后重试”的未完成候选 → 推进到本轮最新处理项 processed[0]
    #    （与 V1 完全一致：完整成功推进到最新）。
    # 2) 存在 CHECKPOINT_BLOCKING_SKIP_REASONS 命中的失败（正文不可读取）→ 保留进入本轮前
    #    的旧 checkpoint，下一轮会再次看到该候选并重新进入 pipeline；已成功/终态项
    #    由 DB 去重与 participation 防重链在重扫时安全挡下。
    # 3) pipeline 抛异常 / feed 请求失败 / risk 都发生在写 checkpoint 之前（直接上抛）。
    #    终态安全 skip（404 链接失效、充电/非抽奖、详情提取失败、已结束过滤、去重命中）
    #    不阻塞推进，避免坏动态卡死整轮扫描。
    blocking_count = 0
    skip_reasons = getattr(pipeline, "skip_reasons", None)
    if isinstance(skip_reasons, dict):
        blocking_count = sum(
            int(skip_reasons.get(reason, 0) or 0)
            for reason in CHECKPOINT_BLOCKING_SKIP_REASONS
        )
    # V21：LLM provider partial/breaker 时保留旧 checkpoint，未完成候选下一轮继续。
    if bool(getattr(pipeline, "partial", False)):
        blocking_count += 1

    if blocking_count > 0:
        # 保守语义：只保留进入本轮前 checkpoint 的值；不先写新值再 rollback。
        new_checkpoint = checkpoint or None
    else:
        new_checkpoint = processed[0] if processed else (checkpoint or None)

    # Keep newest stable IDs plus old anchors so a single disappearing anchor
    # cannot make the next run walk indefinitely into an old window.
    new_anchors: list[str] = []
    if blocking_count:
        new_anchors = list(old_anchors)
    else:
        for dynamic_id in processed + old_anchors:
            if _VALID_DYNAMIC_ID.fullmatch(str(dynamic_id)) and dynamic_id not in new_anchors:
                new_anchors.append(dynamic_id)
            if len(new_anchors) >= ANCHOR_LIMIT:
                break
    state["last_seen_dynamic_id"] = new_checkpoint
    state["anchors"] = new_anchors
    state["last_scan_at"] = now
    state["last_found"] = found
    state["last_added"] = added
    state["accepted"] = len(accepted_ids)
    state["inserted"] = added
    state["self_skipped"] = self_skipped
    state["forward_unresolved_skipped"] = forward_unresolved_skipped
    state["forward_skipped"] = forward_skipped
    state["canonical_duplicate_skipped"] = canonical_duplicate_skipped
    for key in (
        "unfollowed_original_skipped",
        "relationship_unavailable_skipped",
        "relationship_snapshot_status",
    ):
        state.pop(key, None)
    state["last_status"] = "success"
    state["last_scan_status"] = "success" if blocking_count == 0 else "incomplete"
    state["last_error"] = ""
    state["last_pages_fetched"] = pages_fetched
    state["last_stop_reason"] = "pipeline_partial" if blocking_count else stop_reason
    state["last_items_seen"] = items_seen
    state["last_unique_items"] = len(all_ids)
    state["last_new_items"] = len(processed)
    state["last_anchor_hits"] = anchor_hits
    state["last_catch_up_complete"] = catch_up_complete
    _write_state(state)

    if on_progress is not None:
        on_progress(1, 1, f"关注动态补漏完成：发现 {found} 条，新增 {added} 条")

    return {
        "skipped": False,
        "status": "success",
        "scanned_items": len(all_ids),
        "found": found,
        "accepted": len(accepted_ids),
        "added": added,
        "inserted": added,
        "new_link_count": int(getattr(pipeline, "new_link_count", 0) or 0),
        "self_skipped": self_skipped,
        "forward_unresolved_skipped": forward_unresolved_skipped,
        "forward_skipped": forward_skipped,
        "canonical_duplicate_skipped": canonical_duplicate_skipped,
        "pages_fetched": pages_fetched,
        "catch_up_complete": catch_up_complete,
        "stop_reason": "pipeline_partial" if blocking_count else stop_reason,
        "message": f"关注动态补漏完成：发现 {found} 条，新增 {added} 条",
    }
