from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from src.app_logging import get_logger
from src.bilibili_client import BilibiliClient
from src.lottery_api import OpusReadError, OpusRiskControlError, fetch_opus_detail_item_strict
from src.sources.common import (
    EXCLUDE_CONTEXT_RE,
    CheckResult,
    LotteryHint,
    extract_opus_links_with_hints,
    load_previous_output,
    normalize_activity_id,
    opus_link,
    parse_opus_id,
    save_result as write_result,
)
from src.state_store import DATA_DIR, get_last_container

SOURCE_ID = "DS-3"
MID = 100680137
OUTPUT_PATH = DATA_DIR / "output" / "ds3_latest.json"
logger = get_logger("sources.ds3")
SourceProgress = Callable[[int, int, str], None]


def _report(on_progress: SourceProgress | None, done: int, total: int, message: str) -> None:
    if on_progress is not None:
        on_progress(done, total, message)


def article_url(cv_id: int | str) -> str:
    return f"https://www.bilibili.com/read/cv{cv_id}"


def _is_collection_opus(item: dict | None) -> bool:
    if item is None:
        return False
    item_type = item.get("type")
    if type(item_type) is not int:
        raise ValueError(f"Opus 类型必须是整数: {item_type!r}")
    if item_type == 0:
        return False
    if item_type != 1:
        raise ValueError(f"未知 Opus 类型: {item_type!r}")
    basic = item.get("basic")
    if not isinstance(basic, dict):
        raise TypeError("Opus basic 必须是对象")
    if not basic.get("uid") or basic.get("comment_type") != 12:
        raise ValueError("专栏型 Opus 缺少有效作者或评论类型")
    # 实际 DS-3 合集是该作者的专栏型 Opus；普通 Opus 也有正文和标题，
    # 不能凭 module_content、标题关键词或抽奖内容把它们当作合集。
    return str(basic["uid"]) == str(MID)


def _container_links(
    item: dict, container_id: str
) -> tuple[list[str], dict[str, LotteryHint]]:
    modules = item.get("modules")
    if modules is None:
        raise OpusReadError("合集正文缺失")
    if isinstance(modules, dict):
        modules = [modules]
    if not isinstance(modules, list):
        raise TypeError("Opus modules 必须是列表或对象")

    paragraphs: list[dict] = []
    found_content = False
    for module in modules:
        if not isinstance(module, dict):
            raise TypeError("Opus module 必须是对象")
        content = module.get("module_content")
        if content is None:
            continue
        if not isinstance(content, dict):
            raise TypeError("Opus module_content 必须是对象")
        found_content = True
        raw_paragraphs = content.get("paragraphs")
        if raw_paragraphs is None:
            raise OpusReadError("合集正文缺少段落")
        if not isinstance(raw_paragraphs, list):
            raise TypeError("Opus paragraphs 必须是列表")
        for paragraph in raw_paragraphs:
            if not isinstance(paragraph, dict):
                raise TypeError("Opus paragraph 必须是对象")
            text = paragraph.get("text")
            if text is None:
                text = {}
            if not isinstance(text, dict):
                raise TypeError("Opus paragraph.text 必须是对象")
            nodes = text.get("nodes")
            if nodes is None:
                nodes = []
            if not isinstance(nodes, list):
                raise TypeError("Opus text.nodes 必须是列表")
            converted: list[dict] = []
            for node in nodes:
                if not isinstance(node, dict):
                    raise TypeError("Opus text node 必须是对象")
                node_type = node.get("type")
                if node_type == "TEXT_NODE_TYPE_WORD":
                    converted.append({"node_type": 1, "word": node.get("word") or {}})
                elif node_type == "TEXT_NODE_TYPE_RICH":
                    rich = node.get("rich")
                    if rich is None:
                        rich = {}
                    if not isinstance(rich, dict):
                        raise TypeError("Opus rich node 必须是对象")
                    rich_type = rich.get("type")
                    if rich_type not in ("RICH_TEXT_NODE_TYPE_OPUS", "RICH_TEXT_NODE_TYPE_WEB"):
                        continue
                    converted.append(
                        {
                            "node_type": 4,
                            "link": {
                                # WEB 的 rid 可能是其他资源的 ID，不能当作动态 ID。
                                "biz_id": (
                                    rich.get("rid")
                                    if rich_type == "RICH_TEXT_NODE_TYPE_OPUS"
                                    else ""
                                ),
                                "link": rich.get("jump_url") or "",
                                "show_text": rich.get("text") or "",
                                "link_type": 39 if rich_type == "RICH_TEXT_NODE_TYPE_OPUS" else 16,
                            },
                        }
                    )
            paragraphs.append({"text": {"nodes": converted}})
    if not found_content:
        raise OpusReadError("合集正文缺失")

    # 仅适配 Opus 的节点格式，URL/ID 解析、章节提示和去重继续复用原提取器。
    return extract_opus_links_with_hints(
        {"opus": {"content": {"paragraphs": paragraphs}}},
        container_opus_id=container_id,
    )


def _direct_dynamic_ids(paragraphs: list[dict]) -> set[str]:
    """保留结构化正文中的直接动态 URL，避免给普通动态额外查询 Opus。"""
    direct_ids: set[str] = set()
    opus_ids: set[str] = set()
    for paragraph in paragraphs:
        nodes = (paragraph.get("text") or {}).get("nodes") or []
        paragraph_text = "".join(
            (node.get("word") or {}).get("words", "")
            for node in nodes
            if node.get("node_type") == 1
        )
        # 与原提取器保持一致；被排除的历史段落不能把直接动态升级为探测项。
        if EXCLUDE_CONTEXT_RE.search(paragraph_text):
            continue
        for node in nodes:
            if node.get("node_type") != 4:
                continue
            link = node.get("link") or {}
            dynamic_id = parse_opus_id(link)
            if dynamic_id is None:
                continue
            url = link.get("link") or ""
            if (
                urlsplit(url).hostname == "t.bilibili.com"
                and normalize_activity_id(url) == dynamic_id
                and link.get("link_type") != 39
            ):
                direct_ids.add(dynamic_id)
            else:
                opus_ids.add(dynamic_id)
    # 同 ID 同时出现 Opus 与 t 链接时，以需核对的 Opus 元数据为准。
    return direct_ids - opus_ids


def expand_container_links(
    client: BilibiliClient,
    activity_links: list[str],
    link_hints: dict[str, LotteryHint] | None = None,
    *,
    direct_dynamic_ids: set[str] | None = None,
    on_progress: SourceProgress | None = None,
) -> tuple[list[str], dict[str, LotteryHint]]:
    """只读取外层 cv 的 Opus 候选；合集子链接直接作为叶子交原流水线。"""
    root_ids = list(
        dict.fromkeys(
            dynamic_id
            for url in activity_links
            if (dynamic_id := normalize_activity_id(url)) is not None
        )
    )
    direct_ids = direct_dynamic_ids or set()
    probe_ids = [dynamic_id for dynamic_id in root_ids if dynamic_id not in direct_ids]
    original_hints = link_hints or {}
    containers: dict[str, dict] = {}
    emitted: dict[str, None] = {}
    excluded: set[str] = set()
    hints: dict[str, LotteryHint] = {}
    detail_blocked = False
    failed_count = 0

    def skip(dynamic_id: str, reason: str, done: int, total: int) -> None:
        logger.warning("DS-3 跳过 %s：%s", opus_link(dynamic_id), reason)
        _report(on_progress, done, total, f"跳过 {opus_link(dynamic_id)}：{reason}")

    _report(on_progress, 0, len(probe_ids), "正在分析专栏中的合集...")
    # 两阶段处理：先仅核对外层候选并缓存正文，再本地展开。这样根合集互链
    # 可统一排除，且无论内文有多少 OPUS 节点，都不会再产生子项 detail 请求。
    for index, dynamic_id in enumerate(probe_ids, start=1):
        if detail_blocked:
            excluded.add(dynamic_id)
            failed_count += 1
            skip(dynamic_id, "本次已触发 -352，停止后续合集 detail 请求", index, len(probe_ids))
            continue
        _report(
            on_progress, index - 1, len(probe_ids),
            f"正在分析专栏中的合集：检查候选 {index}/{len(probe_ids)}...",
        )
        try:
            item = fetch_opus_detail_item_strict(client, dynamic_id)
        except OpusReadError as exc:
            excluded.add(dynamic_id)
            failed_count += 1
            if isinstance(exc, OpusRiskControlError):
                detail_blocked = True
            skip(dynamic_id, str(exc), index, len(probe_ids))
            continue
        if _is_collection_opus(item):
            containers[dynamic_id] = item
            excluded.add(dynamic_id)

    expanded: dict[str, tuple[list[str], dict[str, LotteryHint]]] = {}
    for index, (dynamic_id, item) in enumerate(containers.items(), start=1):
        _report(
            on_progress, index - 1, len(containers),
            f"发现 {len(containers)} 个合集，正在展开 {index}/{len(containers)}...",
        )
        try:
            child_links, child_hints = _container_links(item, dynamic_id)
        except OpusReadError as exc:
            failed_count += 1
            skip(dynamic_id, str(exc), index, len(containers))
            continue
        if not child_links:
            skip(dynamic_id, "合集正文中没有动态链接", index, len(containers))
            continue
        expanded[dynamic_id] = child_links, child_hints
        logger.info("DS-3 合集展开 %s：%d 条候选链接", opus_link(dynamic_id), len(child_links))
        _report(
            on_progress, index, len(containers),
            f"合集 {index}/{len(containers)}：发现 {len(child_links)} 条候选动态",
        )

    _report(on_progress, 0, 0, "正在整理候选链接...")
    for dynamic_id in root_ids:
        candidate_links, candidate_hints = expanded.get(dynamic_id, ([opus_link(dynamic_id)], {}))
        for url in candidate_links:
            leaf_id = normalize_activity_id(url)
            if leaf_id is None or leaf_id in excluded:
                continue
            # OPUS 链接类型仅表示链接格式，不证明它又是合集。子链接到此为止，
            # 不递归、不探测；已确认的根合集及失败根候选统一从输出中排除。
            emitted.setdefault(leaf_id, None)
            hint = (
                original_hints.get(leaf_id)
                or candidate_hints.get(leaf_id)
                or original_hints.get(dynamic_id)
            )
            if hint:
                hints.setdefault(leaf_id, hint)
    links = [opus_link(dynamic_id) for dynamic_id in emitted]
    if failed_count:
        warning = f"部分合集读取失败：{failed_count} 项未读取，已保留 {len(links)} 条候选动态"
        if detail_blocked:
            warning += "；本次触发 -352，已停止后续合集 detail 请求"
        logger.warning("DS-3 %s", warning)
        _report(on_progress, 0, 0, warning)
    return links, hints


def check_update(*, force: bool = False, on_progress: SourceProgress | None = None) -> CheckResult:
    _report(on_progress, 0, 0, "正在读取「你的抽奖工具人」最新专栏...")
    with BilibiliClient() as client:
        latest = client.get_latest_article(MID)
        cv_id = int(latest["id"])
        container_url = article_url(cv_id)
        previous = get_last_container(SOURCE_ID)

        if not force and previous == container_url:
            prev_output = load_previous_output(OUTPUT_PATH)
            _report(on_progress, 0, 0, "最新专栏没有变化，使用已有候选链接")
            return CheckResult(
                source_id=SOURCE_ID,
                updated=False,
                container_url=container_url,
                container_id=str(cv_id),
                title=latest.get("title", ""),
                published_at=int(latest.get("publish_time") or 0),
                previous_container_url=previous,
                activity_links=(prev_output or {}).get("activity_links") or [],
                checked_at=int(time.time()),
                link_hints=(prev_output or {}).get("link_hints") or {},
            )

        _report(on_progress, 0, 0, "正在读取「你的抽奖工具人」专栏正文...")
        detail = client.get_article_detail(cv_id)
        links, hints = extract_opus_links_with_hints(detail)
        direct_ids = _direct_dynamic_ids(detail["opus"]["content"].get("paragraphs") or [])
        links, hints = expand_container_links(
            client, links, hints, direct_dynamic_ids=direct_ids, on_progress=on_progress,
        )

        return CheckResult(
            source_id=SOURCE_ID,
            updated=True,
            container_url=container_url,
            container_id=str(cv_id),
            title=detail.get("title") or latest.get("title", ""),
            published_at=int(detail.get("publish_time") or latest.get("publish_time") or 0),
            previous_container_url=previous,
            activity_links=links,
            checked_at=int(time.time()),
            link_hints=hints,
        )


def save_result(result: CheckResult) -> Path | None:
    return write_result(OUTPUT_PATH, result)
