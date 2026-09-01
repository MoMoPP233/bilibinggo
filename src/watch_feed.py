from __future__ import annotations

import time
from dataclasses import dataclass

from src.bilibili_client import BilibiliClient, api_code
from src.sources.common import is_valid_dynamic_id, normalize_activity_id, opus_link

SPACE_FEED_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
FEED_PAGE_SIZE = 20
DEFAULT_MAX_PAGES = 30
PAGE_RETRY_ATTEMPTS = 4
PAGE_RETRY_BASE_DELAY = 1.0
PAGE_REQUEST_DELAY = 0.22


@dataclass(frozen=True, slots=True)
class ForwardLink:
    dynamic_id: str
    url: str
    pub_ts: int
    forward_id: str


@dataclass(frozen=True, slots=True)
class OwnedRepost:
    """空间 feed 中能够严格归属给指定账号的一条转发。"""

    repost_dynamic_id: str
    original_dynamic_id: str
    reposted_at: int
    original_author_uid: str | None = None
    original_author_name: str | None = None


def extract_forward_orig_id(item: dict) -> str | None:
    """从空间动态 feed 条目中提取转发原动态的 id_str。"""
    if item.get("type") != "DYNAMIC_TYPE_FORWARD":
        return None
    orig = item.get("orig") or {}
    id_str = str(orig.get("id_str") or "").strip()
    if id_str and is_valid_dynamic_id(id_str):
        return id_str
    return None


def extract_feed_pub_ts(item: dict) -> int | None:
    modules = item.get("modules")
    pub_ts: object = None
    if isinstance(modules, dict):
        pub_ts = (modules.get("module_author") or {}).get("pub_ts")
    elif isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("module_type") == "MODULE_TYPE_AUTHOR":
                pub_ts = (module.get("module_author") or {}).get("pub_ts")
                break
    if pub_ts is None:
        return None
    try:
        value = int(pub_ts)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def extract_forward_id(item: dict) -> str:
    return str(item.get("id_str") or "").strip()


def extract_feed_author(item: dict) -> tuple[str | None, str | None]:
    """从新旧两种 modules 结构中读取顶层动态作者。"""
    modules = item.get("modules")
    author: object = None
    if isinstance(modules, dict):
        author = modules.get("module_author")
    elif isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("module_type") == "MODULE_TYPE_AUTHOR" or module.get("module_author"):
                author = module.get("module_author")
                if author:
                    break
    if not isinstance(author, dict):
        return None, None
    raw_uid = str(author.get("mid") or "").strip()
    uid = raw_uid if raw_uid.isdigit() and int(raw_uid) > 0 else None
    name = str(author.get("name") or author.get("uname") or "").strip() or None
    return uid, name


def extract_owned_repost(item: dict, *, uid: int | str) -> OwnedRepost | None:
    """严格提取当前账号自己的转发；字段不完整或关系有歧义时返回 None。"""
    if not isinstance(item, dict) or item.get("type") != "DYNAMIC_TYPE_FORWARD":
        return None
    owner_uid = str(uid).strip()
    author_uid, _ = extract_feed_author(item)
    if not owner_uid.isdigit() or int(owner_uid) <= 0 or author_uid != owner_uid:
        return None

    repost_dynamic_id = extract_forward_id(item)
    original_dynamic_id = extract_forward_orig_id(item)
    reposted_at = extract_feed_pub_ts(item)
    if (
        not is_valid_dynamic_id(repost_dynamic_id)
        or not original_dynamic_id
        or repost_dynamic_id == original_dynamic_id
        or reposted_at is None
    ):
        return None

    orig = item.get("orig")
    if not isinstance(orig, dict):
        return None
    original_author_uid, original_author_name = extract_feed_author(orig)
    return OwnedRepost(
        repost_dynamic_id=repost_dynamic_id,
        original_dynamic_id=original_dynamic_id,
        reposted_at=reposted_at,
        original_author_uid=original_author_uid,
        original_author_name=original_author_name,
    )


def fetch_space_feed_page(
    client: BilibiliClient,
    *,
    mid: int,
    offset: str,
    retries: int = 0,
) -> dict:
    """读取一页空间动态。

    清理功能使用 ``retries=0``，避免平台异常时快速重复请求；旧监控扫描
    仍由下方兼容包装负责原有重试节奏。
    """
    referer = f"https://space.bilibili.com/{mid}/dynamic"
    payload = client.get_json(
        SPACE_FEED_URL,
        params={
            "host_mid": mid,
            "offset": offset,
            "type": "all",
            "timezone_offset": -480,
            "platform": "web",
            "web_location": "333.1365",
        },
        referer=referer,
        wbi=True,
        retries=retries,
    )
    if api_code(payload) != 0:
        message = str(payload.get("message") or "未知错误")
        raise RuntimeError(f"API error {payload.get('code')}: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("空间动态响应缺少 data")
    return data


def _fetch_space_feed_page(
    client: BilibiliClient,
    *,
    mid: int,
    offset: str,
) -> dict:
    last_error: Exception | None = None
    for attempt in range(PAGE_RETRY_ATTEMPTS):
        try:
            return fetch_space_feed_page(client, mid=mid, offset=offset, retries=1)
        except Exception as exc:
            last_error = exc
            if attempt < PAGE_RETRY_ATTEMPTS - 1:
                time.sleep(PAGE_RETRY_BASE_DELAY * (attempt + 1))
                continue
            raise RuntimeError(f"拉取空间动态失败: {exc}") from exc
    if last_error:
        raise RuntimeError(f"拉取空间动态失败: {last_error}") from last_error
    raise RuntimeError("拉取空间动态失败")


def collect_forward_links_for_user(
    client: BilibiliClient,
    mid: int,
    *,
    since_ts: int,
    until_ts: int | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
) -> list[ForwardLink]:
    """扫描单个用户在时间窗口内的转发原动态（仅 DYNAMIC_TYPE_FORWARD）。"""
    upper = int(until_ts if until_ts is not None else time.time())
    lower = int(since_ts)
    if lower > upper:
        return []

    offset = ""
    collected: list[ForwardLink] = []
    seen_dynamic: set[str] = set()

    for _ in range(max(1, max_pages)):
        data = _fetch_space_feed_page(client, mid=mid, offset=offset)
        items = data.get("items") or []
        if not items:
            break

        reached_older = False
        for item in items:
            if not isinstance(item, dict):
                continue
            pub_ts = extract_feed_pub_ts(item)
            if pub_ts is not None and pub_ts > upper:
                continue
            if pub_ts is not None and pub_ts < lower:
                reached_older = True
                continue

            dynamic_id = extract_forward_orig_id(item)
            if not dynamic_id or dynamic_id in seen_dynamic:
                continue
            seen_dynamic.add(dynamic_id)
            collected.append(
                ForwardLink(
                    dynamic_id=dynamic_id,
                    url=opus_link(dynamic_id),
                    pub_ts=int(pub_ts or upper),
                    forward_id=extract_forward_id(item),
                )
            )

        if reached_older:
            break

        next_offset = str(data.get("offset") or "")
        if not next_offset or len(items) < FEED_PAGE_SIZE:
            break
        offset = next_offset
        time.sleep(PAGE_REQUEST_DELAY)

    return collected
