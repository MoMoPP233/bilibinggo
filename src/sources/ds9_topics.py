"""DS9 话题自动发现源（M1）。

把 Bilibili 话题当作一个“数据源”，只负责发现候选动态：
- 默认内嵌三个话题（topic_id 作为稳定身份，名称仅展示）：
  抽奖！！！ topic_id=1261901；转发动态抽奖！ topic_id=1055656；抽奖号的日常 topic_id=47801。
- 同一 Profile 内：每个 topic_id 独立配置、启用状态、checkpoint/fingerprint、
  incomplete（转发关系不完整）列表、最近成功/错误——状态文件按 Profile 存放，
  绝不跨 Profile 共享。
- V107 转发的 outer 与直接 orig 分别作为候选交给现有公共流水线；只展开一层。
  orig 补读暂时失败时保留 outer 并标记来源 incomplete，不提前推进 checkpoint。
- 先处理后提交：per-topic fingerprint/checkpoint 只在公共流水线成功后的
  commit 钩子（挂到 CheckResult.ds9_topic_commit，由 sources.common.
  commit_source_checkpoint 在流水线成功后调用）里落盘；获取或流水线失败都
  不会推进 checkpoint。
- 结构化平台风控（-352/-509/HTTP 429）以 RuntimeError 信封向上抛（0 retry），
  进入既有 refresh 风险路径与 AutoScheduler 6h cooldown；普通失败只记该话题
  partial（保留旧 checkpoint），继续处理其它话题。

DS9 只是 Source：不创建第二套 classifier / activity DB / participation guard。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.bilibili_client import BilibiliClient
from src.data_paths import get_atomic_path_lock, get_profile_dir
from src.platform_risk import risk_code_of
from src.repost_origin_discovery import OriginDiscovery, discover_origin, is_forward_item
from src.sources.common import CheckResult, is_valid_dynamic_id, opus_link

SOURCE_ID = "DS-9"
TOPIC_HISTORY_MAX_PAGES = 3
TOPIC_PAGE_SIZE = 20
TOPIC_SORT_BY_LATEST = 3
_TOPIC_ID_RE = re.compile(r"^\d{1,12}$")
_URL_TOPIC_ID_RE = re.compile(r"topic_id=(\d{1,12})")

_STATE_FILE = "ds9_topics.json"
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BuiltinTopic:
    name: str
    topic_id: str


BUILTIN_TOPICS: tuple[BuiltinTopic, ...] = (
    BuiltinTopic(name="抽奖！！！", topic_id="1261901"),
    BuiltinTopic(name="转发动态抽奖！", topic_id="1055656"),
    BuiltinTopic(name="抽奖号的日常", topic_id="47801"),
)


# ---------------------------------------------------------------------------
# per-Profile 状态存取（atomic 写，V10 同一 per-path 共享锁）
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return get_profile_dir() / _STATE_FILE


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    return raw if isinstance(raw, dict) else {}


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    with get_atomic_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _default_topic_records() -> list[dict[str, Any]]:
    return [
        {
            "topic_id": topic.topic_id,
            "name": topic.name,
            "kind": "built-in",
            "enabled": True,
            "fp": None,
            "incomplete": [],
            "last_success_at": None,
            "last_scan_at": None,
            "last_found": 0,
            "last_error": "",
            "last_success_unique_ids": None,
            "last_unique_added": None,
        }
        for topic in BUILTIN_TOPICS
    ]


def _topic_records(state: dict[str, Any]) -> list[dict[str, Any]]:
    records = state.get("topics")
    if not isinstance(records, list):
        return _default_topic_records()
    by_id = {str(item.get("topic_id") or ""): item for item in records if isinstance(item, dict)}
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for builtin in _default_topic_records():
        stored = by_id.get(builtin["topic_id"])
        record = {**builtin, **(stored or {})}
        if stored is not None:
            record["kind"] = "built-in"
        merged.append(record)
        seen.add(builtin["topic_id"])
    for item in records:
        if not isinstance(item, dict):
            continue
        topic_id = str(item.get("topic_id") or "")
        if not topic_id or topic_id in seen:
            continue
        seen.add(topic_id)
        merged.append(
            {
                "topic_id": topic_id,
                "name": str(item.get("name") or topic_id),
                "kind": str(item.get("kind") or "custom"),
                "enabled": bool(item.get("enabled", True)),
                "fp": item.get("fp"),
                "incomplete": list(item.get("incomplete") or []),
                "last_success_at": item.get("last_success_at"),
                "last_scan_at": item.get("last_scan_at"),
                "last_found": int(item.get("last_found") or 0),
                "last_error": str(item.get("last_error") or ""),
                "last_success_unique_ids": item.get("last_success_unique_ids"),
                "last_unique_added": item.get("last_unique_added"),
            }
        )
    return merged


def _save_topic_records(records: list[dict[str, Any]]) -> None:
    state = _read_state()
    state["version"] = 1
    state["topics"] = records
    _write_state(state)


# ---------------------------------------------------------------------------
# 配置管理（Profile 隔离；built-in 永驻，custom 可增删）
# ---------------------------------------------------------------------------


def list_topics() -> list[dict[str, Any]]:
    """当前 Profile 的 DS9 话题配置视图（含 built-in 与 custom，0 远程）。

    返回派生的 incomplete_count（纯观察字段，由持久化的 incomplete 列表现算，
    绝不落盘、绝不参与 needs/checkpoint/fingerprint/dedup）。
    """
    topics = []
    for record in _topic_records(_read_state()):
        view = dict(record)
        incomplete = list(view.get("incomplete") or [])
        view["incomplete_count"] = len(incomplete)
        topics.append(view)
    return topics


def parse_topic_id(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("topic_id 或话题 URL 不能为空")
    match = _URL_TOPIC_ID_RE.search(text)
    if match:
        topic_id = match.group(1)
    elif _TOPIC_ID_RE.fullmatch(text):
        topic_id = text
    else:
        raise ValueError("话题格式无效：请输入 topic_id 或包含 topic_id= 的话题链接")
    return topic_id


def add_topic(name: str, topic_id_value: str) -> dict[str, Any]:
    topic_id = parse_topic_id(topic_id_value)
    display_name = str(name or "").strip() or f"话题 {topic_id}"
    records = _topic_records(_read_state())
    if any(str(item.get("topic_id")) == topic_id for item in records):
        raise ValueError(f"话题 {topic_id} 已存在")
    records.append(
        {
            "topic_id": topic_id,
            "name": display_name,
            "kind": "custom",
            "enabled": True,
            "fp": None,
            "incomplete": [],
            "last_success_at": None,
            "last_scan_at": None,
            "last_found": 0,
            "last_error": "",
            "last_success_unique_ids": None,
            "last_unique_added": None,
        }
    )
    _save_topic_records(records)
    return next(item for item in records if item["topic_id"] == topic_id)


def remove_topic(topic_id: str) -> None:
    records = _topic_records(_read_state())
    target = next((item for item in records if item["topic_id"] == topic_id), None)
    if target is None:
        raise ValueError(f"话题不存在：{topic_id}")
    if target["kind"] == "built-in":
        raise ValueError("内置话题不可删除，可通过启用/暂停控制")
    _save_topic_records([item for item in records if item["topic_id"] != topic_id])


def set_topic_enabled(topic_id: str, enabled: bool) -> dict[str, Any]:
    records = _topic_records(_read_state())
    target = next((item for item in records if item["topic_id"] == topic_id), None)
    if target is None:
        raise ValueError(f"话题不存在：{topic_id}")
    target["enabled"] = bool(enabled)
    _save_topic_records(records)
    return target


# ---------------------------------------------------------------------------
# 载荷解析：转发动态的 canonical 必须落到真正目标动态，绝不拿 outer 顶替。
# ---------------------------------------------------------------------------


def _entry_dynamic_id(desc: dict, entry: dict) -> str | None:
    raw = (
        desc.get("dynamic_id")
        or entry.get("id_str")
        or entry.get("dynamic_id")
        or (entry.get("item") or {}).get("id_str")
    )
    if raw is None:
        return None
    text = str(raw).strip()
    return text if is_valid_dynamic_id(text) else None


def _orig_dynamic_id(entry: dict) -> str | None:
    origin = entry.get("desc", {}).get("orig") if isinstance(entry.get("desc"), dict) else None
    if not isinstance(origin, dict):
        origin = entry.get("orig")
    if not isinstance(origin, dict):
        return None
    raw = origin.get("id_str") or origin.get("dynamic_id") or (origin.get("desc") or {}).get("dynamic_id")
    if raw is None:
        return None
    text = str(raw).strip()
    return text if is_valid_dynamic_id(text) else None


def normalize_topic_entry(entry: dict) -> tuple[str | None, str | None]:
    """返回 (canonical_dynamic_id, outer_dynamic_id)。

    - 明确存在结构化 orig.id_str：canonical=orig（活动主键），outer 仅作来源元数据；
    - 非转发原创动态：canonical=外层自身；
    - 结构化转发（desc.type==1）但取不到可靠 orig：canonical=None（incomplete），
      绝不拿 outer 当原动态进入参与链；
    - 结构不明：按原创候选交给公共流水线，由既有详情/分类/去重链兜底。
    """
    desc = entry.get("desc")
    desc = desc if isinstance(desc, dict) else {}
    outer_id = _entry_dynamic_id(desc, entry)
    orig_id = _orig_dynamic_id(entry)
    if orig_id is not None:
        return orig_id, outer_id
    if outer_id is None:
        return None, None
    entry_type = str(entry.get("type") or desc.get("type") or "")
    if entry_type in {"1", "DYNAMIC_TYPE_FORWARD"}:
        return None, outer_id
    return outer_id, outer_id


def normalize_topic_entries(entries: list[dict]) -> tuple[list[str], list[str], list[str]]:
    """返回 (resolved canonical ids, incomplete outer ids, ordered outer ids)。"""
    resolved: list[str] = []
    incomplete: list[str] = []
    outers: list[str] = []
    seen_outer: set[str] = set()
    seen_resolved: set[str] = set()
    for entry in entries:
        canonical, outer = normalize_topic_entry(entry)
        if outer is not None and outer not in seen_outer:
            seen_outer.add(outer)
            outers.append(outer)
        if canonical is not None:
            if canonical not in seen_resolved:
                seen_resolved.add(canonical)
                resolved.append(canonical)
        elif outer is not None and outer not in incomplete:
            incomplete.append(outer)
    return resolved, incomplete, outers


def _expand_topic_entries(
    entries: list[dict],
    client: BilibiliClient,
    detail_cache: dict[str, OriginDiscovery],
) -> tuple[list[str], list[str], list[str], dict[str, dict[str, str]], set[str]]:
    """Keep each outer and add its immediate orig as one extra candidate."""
    candidates: list[str] = []
    incomplete: list[str] = []
    outers: list[str] = []
    provenance: dict[str, dict[str, str]] = {}
    preserve_ids: set[str] = set()
    for entry in entries:
        desc = entry.get("desc") if isinstance(entry.get("desc"), dict) else {}
        outer = _entry_dynamic_id(desc, entry)
        if outer is None:
            continue
        outers.append(outer)
        candidates.append(outer)
        provenance.setdefault(outer, {"discovery_kind": "direct", "via_dynamic_id": ""})
        if not is_forward_item(entry):
            continue
        preserve_ids.add(outer)
        origin = discover_origin(entry, outer, client=client, cache=detail_cache)
        if origin.original_id and origin.original_id != outer:
            preserve_ids.add(origin.original_id)
            candidates.append(origin.original_id)
            provenance.setdefault(
                origin.original_id,
                {"discovery_kind": "repost_origin", "via_dynamic_id": outer},
            )
        elif origin.retryable:
            incomplete.append(outer)
            _logger.warning("DS9 话题原动态待补读 outer=%s reason=%s", outer, origin.reason)
        elif origin.reason:
            _logger.info("DS9 话题原动态不可用 outer=%s reason=%s", outer, origin.reason)
    return list(dict.fromkeys(candidates)), list(dict.fromkeys(incomplete)), list(dict.fromkeys(outers)), provenance, preserve_ids


def fingerprint_of_topic(outer_ids: list[str]) -> str:
    """只基于稳定动态 ID 顺序集合；不含任何持续波动的统计字段。"""
    digest = hashlib.sha256()
    digest.update("\n".join(outer_ids).encode("utf-8"))
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 请求层：结构化风控直接抛（0 retry，进入既有 risk/cooldown），其余按调用方区分。
# ---------------------------------------------------------------------------


def _raise_on_structured_risk(payload: object) -> None:
    code = None
    if isinstance(payload, dict):
        raw_code = payload.get("code")
        try:
            parsed = int(raw_code)
        except (TypeError, ValueError):
            parsed = None
        if parsed in (-352, -509, 429):
            code = parsed
    if code is None:
        code = risk_code_of(payload)
    if code is None:
        return
    message = ""
    if isinstance(payload, dict):
        message = str(payload.get("message") or payload.get("msg") or "")
    raise RuntimeError(f"API error {code}: {message or '话题接口触发平台风控'}")


def _api_error(payload: dict) -> RuntimeError | None:
    code = payload.get("code")
    if code is None or code == 0:
        return None
    message = str(payload.get("message") or payload.get("msg") or "")
    return RuntimeError(f"话题接口返回异常（code={code}：{message}）")


def _fetch_topic_page(
    client: BilibiliClient,
    topic_id: str,
    *,
    offset: str = "",
) -> tuple[list[dict], str, bool]:
    payload = client.get_topic_feed(
        topic_id,
        offset=offset,
        page_size=TOPIC_PAGE_SIZE,
        sort_by=TOPIC_SORT_BY_LATEST,
        source="Web",
    )
    _raise_on_structured_risk(payload)
    error = _api_error(payload)
    if error is not None:
        raise error
    return _payload_topic_page(payload)


def _payload_topic_page(payload: dict) -> tuple[list[dict], str, bool]:
    data = payload.get("data")
    data = data if isinstance(data, dict) else {}
    entries: list[dict] = []
    seen: set[str] = set()
    topic_cards = data.get("topic_card_list")
    if isinstance(topic_cards, dict):
        for wrapper in topic_cards.get("items") or []:
            if not isinstance(wrapper, dict):
                continue
            item = wrapper.get("dynamic_card_item")
            if not isinstance(item, dict):
                continue
            outer = _entry_dynamic_id({}, item)
            if outer is not None:
                if outer in seen:
                    continue
                seen.add(outer)
            entries.append(item)
        return (
            entries,
            str(topic_cards.get("offset") or ""),
            bool(topic_cards.get("has_more")),
        )

    # 兼容历史 fixture/响应，仅用于容错；DS9 主请求已固定走现代 feed/topic。
    for key in ("cards", "items", "hot_list"):
        for item in data.get(key) or []:
            if not isinstance(item, dict):
                continue
            desc = item.get("desc")
            outer = _entry_dynamic_id(desc if isinstance(desc, dict) else {}, item)
            if outer is not None:
                if outer in seen:
                    continue
                seen.add(outer)
            entries.append(item)
    return entries, str(data.get("offset") or ""), bool(data.get("has_more"))


def _fetch_topic_history_pages(
    client: BilibiliClient,
    topic_id: str,
    *,
    first_offset: str,
    first_has_more: bool,
    max_pages: int = TOPIC_HISTORY_MAX_PAGES - 1,
) -> list[dict]:
    entries: list[dict] = []
    offset = str(first_offset or "")
    has_more = bool(first_has_more)
    seen_offsets: set[str] = set()
    for _ in range(max_pages):
        if not has_more or not offset or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        page_entries, next_offset, has_more = _fetch_topic_page(
            client,
            topic_id,
            offset=offset,
        )
        if not page_entries:
            break
        entries.extend(page_entries)
        if not next_offset:
            break
        offset = next_offset
    return entries


def _fetch_topic_history_pages_with_index(
    client: BilibiliClient,
    topic_id: str,
    *,
    first_offset: str,
    first_has_more: bool,
    max_pages: int = TOPIC_HISTORY_MAX_PAGES - 1,
) -> list[tuple[int, list[dict]]]:
    """Fetch the same bounded history window while retaining real page numbers."""
    pages: list[tuple[int, list[dict]]] = []
    offset = str(first_offset or "")
    has_more = bool(first_has_more)
    seen_offsets: set[str] = set()
    for page_index in range(2, max_pages + 2):
        if not has_more or not offset or offset in seen_offsets:
            break
        seen_offsets.add(offset)
        page_entries, next_offset, has_more = _fetch_topic_page(
            client,
            topic_id,
            offset=offset,
        )
        if not page_entries:
            break
        pages.append((page_index, page_entries))
        if not next_offset:
            break
        offset = next_offset
    return pages


# ---------------------------------------------------------------------------
# 扫描 + 流水线成功后再提交的 per-topic checkpoint
# ---------------------------------------------------------------------------


def _commit_topic_meta_now(records: list[dict[str, Any]], *, now: int) -> None:
    """仅更新失败/扫描时间等运行元数据；绝不触碰 fp/incomplete/成功态。"""
    stored = _topic_records(_read_state())
    by_id = {str(item.get("topic_id")): item for item in stored}
    touched = False
    for record in records:
        target = by_id.get(str(record.get("topic_id")))
        if target is None:
            continue
        if "last_error" in record:
            target["last_error"] = record.get("last_error", target.get("last_error", ""))
        if "last_scan_at" in record:
            target["last_scan_at"] = record.get("last_scan_at", target.get("last_scan_at"))
        touched = True
    if touched:
        _save_topic_records(stored)


def _apply_topic_commits(
    pending: list[dict[str, Any]], nochange_topic_ids: list[str] | None = None,
) -> None:
    """流水线成功后的唯一 checkpoint 提交点：per-topic fp/incomplete/成功元数据。"""
    if not pending and not nochange_topic_ids:
        return
    # Contribution observations are written by the completed pipeline before
    # this checkpoint hook. Keep the same seven-day/exclusive definition as
    # /api/ds9/contribution/unique; never classify or fetch again here.
    unique_by_topic: dict[str, set[str]] | None = None
    try:
        from src.ds9_contribution_audit import list_unique_valid

        snapshot: dict[str, set[str]] = {}
        for row in list_unique_valid():
            topic_id = str(row.get("topic_id") or "")
            dynamic_id = str(row.get("dynamic_id") or "")
            if topic_id and dynamic_id:
                snapshot.setdefault(topic_id, set()).add(dynamic_id)
        unique_by_topic = snapshot
    except Exception:  # noqa: BLE001 - optional audit must not block checkpoint
        _logger.exception("DS9 独有抽奖基线读取失败，保留既有基线")
    stored = _topic_records(_read_state())
    by_id = {str(item.get("topic_id")): item for item in stored}

    def update_unique_baseline(target: dict[str, Any], topic_id: str) -> None:
        if unique_by_topic is None:
            return
        current_ids = unique_by_topic.get(topic_id, set())
        previous_ids = target.get("last_success_unique_ids")
        previous = (
            {item for item in previous_ids if isinstance(item, str)}
            if isinstance(previous_ids, list) else None
        )
        target["last_unique_added"] = (
            len(current_ids - previous) if previous is not None else None
        )
        # Replaces the previous rolling seven-day snapshot; never accumulates
        # all historical IDs in this Profile-local JSON.
        target["last_success_unique_ids"] = sorted(current_ids)

    for update in pending:
        target = by_id.get(str(update.get("topic_id")))
        if target is None:
            continue
        target["fp"] = update.get("fp")
        target["incomplete"] = list(update.get("incomplete") or [])
        target["last_success_at"] = update.get("last_success_at")
        target["last_found"] = int(update.get("last_found") or 0)
        target["last_error"] = ""
        target["last_scan_at"] = update.get("last_scan_at")
        # A topic with unresolved outer/orig relationships has not completed
        # its candidate window. Preserve the previous comparison baseline.
        if not update.get("incomplete"):
            update_unique_baseline(target, str(update.get("topic_id")))
    for topic_id in nochange_topic_ids or []:
        target = by_id.get(topic_id)
        if target is not None:
            update_unique_baseline(target, topic_id)
    _save_topic_records(stored)


def check_update(*, force: bool = False) -> CheckResult:
    """扫描全部启用话题：成功话题产出候选；失败话题保留旧 checkpoint 并记 partial。"""
    records = _topic_records(_read_state())
    now = int(time.time())
    links: list[str] = []
    activity_source_context: dict[str, dict[str, str]] = {}
    seen_links: set[str] = set()
    pending: list[dict[str, Any]] = []
    activity_source_observations: list[dict[str, str]] = []
    source_page_stats: list[dict[str, object]] = []
    failed_topic_ids: list[str] = []
    incomplete_origin_topics: list[str] = []
    origin_retryable_outers: list[str] = []
    nochange_topic_ids: list[str] = []
    preserve_forward_ids: set[str] = set()
    detail_cache: dict[str, OriginDiscovery] = {}

    with BilibiliClient() as client:
        for record in records:
            topic_id = str(record.get("topic_id") or "")
            if not record.get("enabled"):
                continue
            prev_fp = record.get("fp")
            prev_incomplete = list(record.get("incomplete") or [])
            try:
                new_entries, first_offset, first_has_more = _fetch_topic_page(client, topic_id)
                outers = list(dict.fromkeys(
                    outer for entry in new_entries
                    if (outer := _entry_dynamic_id(
                        entry.get("desc") if isinstance(entry.get("desc"), dict) else {}, entry,
                    )) is not None
                ))
                fp = fingerprint_of_topic(outers)
                needs = force or fp != prev_fp or bool(prev_incomplete)
                page_entries: list[tuple[int, list[dict]]] = [(1, new_entries)]
                if needs:
                    # 现代话题 Feed：最新页 + 最多 2 个 offset 历史页，总计 ≤3 页/话题。
                    history_pages = _fetch_topic_history_pages_with_index(
                        client,
                        topic_id,
                        first_offset=first_offset,
                        first_has_more=first_has_more,
                    )
                    page_entries.extend(history_pages)
                if not needs:
                    # 无变化且无未完成项：本轮无产出。last_scan_at 是纯观察字段
                    # （不参与 fp/checkpoint/去重/是否扫描），仍然持久化以便重启后可读。
                    record["last_scan_at"] = now
                    nochange_topic_ids.append(topic_id)
                    continue

                resolved: list[str] = []
                incomplete: list[str] = []
                topic_retryable = False
                provenance: dict[str, dict[str, str]] = {}
                topic_preserve_ids: set[str] = set()
                visible_outers: set[str] = set()
                seen_page_ids: set[str] = set()
                for page_index, entries in page_entries:
                    page_resolved, page_incomplete, page_outers, page_provenance, page_preserve_ids = _expand_topic_entries(
                        entries, client, detail_cache,
                    )
                    topic_preserve_ids.update(page_preserve_ids)
                    resolved.extend(page_resolved)
                    incomplete.extend(page_incomplete)
                    if page_incomplete:
                        topic_retryable = True
                        origin_retryable_outers.extend(page_incomplete)
                    visible_outers.update(page_outers)
                    for candidate, metadata in page_provenance.items():
                        provenance.setdefault(candidate, metadata)
                    duplicate_count = 0
                    for canonical in page_resolved:
                        if canonical in seen_page_ids:
                            duplicate_count += 1
                        seen_page_ids.add(canonical)
                        activity_source_observations.append(
                            {
                                "source_id": SOURCE_ID,
                                "dynamic_id": canonical,
                                "topic_id": topic_id,
                                "topic_name": str(record.get("name") or topic_id),
                                "page_index": str(page_index),
                            }
                        )
                    source_page_stats.append(
                        {
                            "source_id": SOURCE_ID,
                            "topic_id": topic_id,
                            "topic_name": str(record.get("name") or topic_id),
                            "page_index": page_index,
                            "discovered_ids": page_resolved,
                            "discovered_count": len(page_resolved),
                            "batch_duplicate_count": duplicate_count,
                        }
                    )
                resolved = list(dict.fromkeys(resolved))
                incomplete = list(dict.fromkeys(incomplete))
                # Older unresolved wrappers outside the bounded visible window
                # remain retryable, as before. A visible resolved wrapper clears.
                for outer in prev_incomplete:
                    if outer not in visible_outers and outer not in incomplete:
                        incomplete.append(outer)
                if topic_retryable:
                    incomplete_origin_topics.append(topic_id)
            except Exception as exc:  # noqa: BLE001 - 普通失败按话题隔离；风险继续上抛
                if risk_code_of(exc) is not None:
                    raise
                failed_topic_ids.append(topic_id)
                record["last_error"] = str(exc).strip() or "话题扫描失败"
                record["last_scan_at"] = now
                continue

            # 本轮该话题产生产出（或空批次变化）：提交计划挂在流水线成功之后。
            preserve_forward_ids.update(topic_preserve_ids)
            for canonical in resolved:
                if canonical not in seen_links:
                    seen_links.add(canonical)
                    links.append(opus_link(canonical))
                    activity_source_context[canonical] = {
                        "topic_id": topic_id,
                        "topic_name": str(record.get("name") or topic_id),
                        "source_id": SOURCE_ID,
                        **provenance.get(canonical, {}),
                    }
            pending.append(
                {
                    "topic_id": topic_id,
                    "fp": fp,
                    "incomplete": incomplete,
                    "last_success_at": now,
                    "last_found": len(resolved),
                    "last_scan_at": now,
                }
            )

    # 失败话题与“无变化话题”的 last_scan_at 观察元数据一次性原子落盘
    # （绝不触碰 fp/incomplete/成功态/checkpoint）。
    meta_updates: list[dict[str, Any]] = []
    for record in records:
        tid = str(record.get("topic_id") or "")
        if tid in failed_topic_ids:
            meta_updates.append(
                {"topic_id": tid, "last_error": record.get("last_error", ""), "last_scan_at": now}
            )
        elif tid in nochange_topic_ids:
            meta_updates.append({"topic_id": tid, "last_scan_at": now})
    if meta_updates:
        _commit_topic_meta_now(meta_updates, now=now)

    updated = bool(pending)
    summary = {
        "partial": bool(failed_topic_ids or incomplete_origin_topics),
        "failed_topics": failed_topic_ids,
        "incomplete_origin_topics": incomplete_origin_topics,
        "pending_commit": len(pending),
    }
    result = CheckResult(
        source_id=SOURCE_ID,
        updated=updated,
        container_url="topic://discovery",
        container_id="topics",
        title=f"话题发现（{len(records)} 个话题，{len(links)} 条动态）",
        published_at=now if updated else 0,
        previous_container_url="topic://discovery",
        activity_links=links,
        checked_at=now,
        activity_source_context=activity_source_context,
        activity_preserve_forward_ids=sorted(preserve_forward_ids),
        activity_source_observations=activity_source_observations,
        source_page_stats=source_page_stats,
        source_complete=not bool(incomplete_origin_topics),
        source_incomplete_containers=[opus_link(outer) for outer in dict.fromkeys(origin_retryable_outers)],
    )
    # 提交钩子：公共流水线成功（common.commit_source_checkpoint）后执行。
    result.ds9_summary = summary  # type: ignore[attr-defined]
    result.ds9_topic_commit = lambda: _apply_topic_commits(pending, nochange_topic_ids)  # type: ignore[attr-defined]
    return result


def save_result(result: CheckResult) -> Path | None:
    from src.sources.common import save_result as generic_save

    # DS9 快照走公共 DB 快照（source_id 维度）；返回值为聚合器预留的占位。
    generic_save(Path("unused"), result)
    return None
