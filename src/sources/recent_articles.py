"""DS-2/4/5/6/7: one-page, bounded article-container incrementality."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Literal, Pattern

import httpx

from src.bilibili_client import BilibiliClient
from src.platform_risk import risk_code_of
from src.sources.common import (
    CheckResult,
    LotteryHint,
    extract_opus_links_with_hints,
    load_previous_output,
    normalize_activity_id,
    opus_link,
    save_result,
)
from src.state_store import get_last_container, get_last_cv_id


# The existing article endpoint accepts ps and already returns a list. Five
# entries fit three intervening publications, the old checkpoint, and one spare
# without a second list request or any pagination.
RECENT_ARTICLE_LIMIT = 5
_CV_URL_RE = re.compile(r"/read/cv(\d+)(?:$|[/?#])")
_KNOWN_DETAIL_ERRORS = (
    "API error ",
    "网络请求失败:",
    "无法获取专栏详情:",
    "响应不是有效 JSON",
    "请求失败",
)


def _checkpoint_cv(source_id: str, previous: str | None, kind: Literal["cv", "opus"]) -> str | None:
    if kind == "opus":
        return get_last_cv_id(source_id) if previous else None
    match = _CV_URL_RE.search(previous or "")
    return match.group(1) if match else None


def _cache_entry(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    links = value.get("activity_links")
    hints = value.get("link_hints")
    if not isinstance(links, list) or not all(isinstance(url, str) for url in links):
        return None
    if not isinstance(hints, dict):
        return None
    if not isinstance(value.get("container_url"), str) or not isinstance(value.get("container_id"), str):
        return None
    return value


def check_recent_articles(
    client: BilibiliClient,
    *,
    source_id: str,
    mid: int,
    output_path: Path,
    kind: Literal["cv", "opus"],
    force: bool = False,
    exclude_link_text: Pattern[str] | None = None,
) -> CheckResult:
    previous = get_last_container(source_id)
    checkpoint_cv = _checkpoint_cv(source_id, previous, kind)
    articles = client.get_recent_articles(mid, limit=RECENT_ARTICLE_LIMIT)[:RECENT_ARTICLE_LIMIT]
    ids = [str(int(article["id"])) for article in articles]
    latest = articles[0]
    latest_cv = ids[0]
    old_snapshot = load_previous_output(output_path)

    if checkpoint_cv == latest_cv and not force:
        return CheckResult(
            source_id=source_id, updated=False, container_url=previous or f"https://www.bilibili.com/read/cv{latest_cv}",
            container_id=str((old_snapshot or {}).get("container_id") or latest_cv),
            title=str(latest.get("title") or ""), published_at=int(latest.get("publish_time") or 0),
            previous_container_url=previous, activity_links=list((old_snapshot or {}).get("activity_links") or []),
            checked_at=int(time.time()), link_hints=dict((old_snapshot or {}).get("link_hints") or {}),
            cv_id=latest_cv if kind == "opus" else None,
        )

    # The list requests sort=publish_time. Refuse to advance when the returned
    # timestamps do not corroborate that newest-to-oldest order.
    if checkpoint_cv and len(articles) > 1:
        times = [int(article.get("publish_time") or 0) for article in articles]
        if any(ts <= 0 for ts in times) or any(a < b for a, b in zip(times, times[1:])):
            raise RuntimeError(f"{source_id} 专栏列表发布时间缺失或顺序异常，未推进检查点")

    if checkpoint_cv is None:
        # First run / old Profile without a usable cv boundary keeps V88's
        # latest-only bootstrap, never retroactively imports a page of history.
        selected = articles[:1]
        boundary_found = True
    elif checkpoint_cv in ids:
        boundary_index = ids.index(checkpoint_cv)
        selected = articles[:boundary_index] or (articles[:1] if force else [])
        boundary_found = True
    else:
        selected = articles
        boundary_found = False

    if not selected:
        return CheckResult(
            source_id=source_id, updated=False, container_url=previous or "", container_id=latest_cv,
            title=str(latest.get("title") or ""), published_at=int(latest.get("publish_time") or 0),
            previous_container_url=previous, activity_links=list((old_snapshot or {}).get("activity_links") or []),
            checked_at=int(time.time()), link_hints=dict((old_snapshot or {}).get("link_hints") or {}),
            cv_id=latest_cv if kind == "opus" else None,
        )

    old_cache = (
        (old_snapshot or {}).get("source_completed_containers") or {}
        if not force and (old_snapshot or {}).get("previous_container_url") == previous
        else {}
    )
    if not isinstance(old_cache, dict):
        old_cache = {}
    selected_ids = [str(int(article["id"])) for article in selected]
    completed: dict[str, dict[str, object]] = {
        cv_id: cached
        for cv_id in selected_ids
        if (cached := _cache_entry(old_cache.get(cv_id))) is not None
    }
    failed: list[str] = []
    risk_error: Exception | None = None
    for index, article in enumerate(selected):
        cv_id = selected_ids[index]
        if cv_id in completed:
            continue
        try:
            detail = client.get_article_detail(int(cv_id))
        except (RuntimeError, httpx.HTTPError) as exc:
            transport = isinstance(exc, httpx.RequestError) or isinstance(exc.__cause__, httpx.RequestError)
            if not transport and risk_code_of(exc) is not None:
                risk_error = exc
                failed.extend(selected_ids[index:])
                break
            if not transport and not isinstance(exc, httpx.HTTPError) and not str(exc).startswith(_KNOWN_DETAIL_ERRORS):
                raise
            failed.append(cv_id)
            continue

        if kind == "opus":
            container_id = str((detail.get("opus") or {}).get("opus_id") or "")
            if not container_id:
                raise RuntimeError(f"无法从 cv{cv_id} 解析 Opus 容器 ID")
            container_url = opus_link(container_id)
            links, hints = extract_opus_links_with_hints(detail, container_opus_id=container_id)
        else:
            container_id = cv_id
            container_url = f"https://www.bilibili.com/read/cv{cv_id}"
            links, hints = extract_opus_links_with_hints(detail, exclude_link_text=exclude_link_text)
        completed[cv_id] = {
            "container_url": container_url,
            "container_id": container_id,
            "title": str(detail.get("title") or article.get("title") or ""),
            "published_at": int(detail.get("publish_time") or article.get("publish_time") or 0),
            "activity_links": links,
            "link_hints": hints,
        }

    unique_links: dict[str, str] = {}
    merged_hints: dict[str, LotteryHint] = {}
    for cv_id in reversed(selected_ids):
        entry = completed.get(cv_id)
        if entry is None:
            continue
        for url in entry["activity_links"]:
            key = normalize_activity_id(url) or url
            unique_links.setdefault(key, url)
        for dynamic_id, hint in entry["link_hints"].items():
            merged_hints.setdefault(dynamic_id, hint)

    newest_entry = completed.get(latest_cv)
    complete = boundary_found and not failed
    result = CheckResult(
        source_id=source_id, updated=True,
        container_url=str(newest_entry["container_url"]) if newest_entry else f"https://www.bilibili.com/read/cv{latest_cv}",
        container_id=str(newest_entry["container_id"]) if newest_entry else latest_cv,
        title=str((newest_entry or {}).get("title") or latest.get("title") or ""),
        published_at=int((newest_entry or {}).get("published_at") or latest.get("publish_time") or 0),
        previous_container_url=previous, activity_links=list(unique_links.values()), checked_at=int(time.time()),
        link_hints=merged_hints, cv_id=latest_cv if kind == "opus" else None,
        source_complete=complete,
        source_incomplete_containers=failed + ([checkpoint_cv] if not boundary_found and checkpoint_cv else []),
        source_completed_containers=completed,
    )
    if risk_error is not None:
        # Source check cannot reach the pipeline on formal risk. Keep successful
        # candidate/cache snapshot for the next safe retry, but never checkpoint.
        save_result(output_path, result)
        raise risk_error
    return result
