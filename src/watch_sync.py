from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Callable

from src import watch_resume
from src.bilibili_client import BilibiliClient
from src.sources.common import is_valid_dynamic_id, normalize_activity_id, opus_link
from src.state_store import DATA_DIR, get_watch_last_synced_at
from src.watch_feed import (
    DEFAULT_MAX_PAGES, PAGE_REQUEST_DELAY, ForwardScanResult, collect_forward_links_for_user,
)
from src.watch_users import WatchUser, list_watch_users

WATCH_SOURCE_ID = "WATCH"
OUTPUT_PATH = DATA_DIR / "output" / "watch_latest.json"
MAX_WINDOW_SECONDS = 10 * 24 * 3600
FIRST_SYNC_WINDOW_SECONDS = 10 * 24 * 3600
USER_REQUEST_DELAY = 0.45

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class WatchUserScanResult:
    mid: int
    name: str
    ok: bool
    link_count: int
    forward_count: int
    message: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class WatchSyncResult:
    source_id: str
    synced_at: int
    window_start: int
    window_end: int
    activity_links: list[str]
    users_total: int
    users_ok: int
    users_failed: list[dict]
    user_results: list[dict]
    link_count: int
    source_complete: bool = True
    resume_token: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def compute_sync_window(*, now: int | None = None, last_synced_at: int | None = None) -> tuple[int, int]:
    current = int(now if now is not None else time.time())
    if last_synced_at is None or last_synced_at <= 0:
        return current - FIRST_SYNC_WINDOW_SECONDS, current
    earliest = current - MAX_WINDOW_SECONDS
    since = max(int(last_synced_at), earliest)
    return since, current


def _dedupe_activity_links(raw_links: list[str]) -> list[str]:
    """合并多用户扫描结果时按动态 ID 去重，仅整理采集列表。"""
    ordered: list[str] = []
    seen: set[str] = set()
    for raw_url in raw_links:
        dynamic_id = normalize_activity_id(str(raw_url))
        if not dynamic_id or dynamic_id in seen:
            continue
        seen.add(dynamic_id)
        ordered.append(opus_link(dynamic_id))
    return ordered


def sync_watch_forwards(
    *,
    users: list[WatchUser] | None = None,
    on_progress: ProgressCallback | None = None,
) -> WatchSyncResult:
    """拉取监控用户在时间窗口内的转发原动态链接（仅采集，不做活动库去重）。"""
    sync_started_at = int(time.time())
    last_synced_at = get_watch_last_synced_at()
    window_start, window_end = compute_sync_window(now=sync_started_at, last_synced_at=last_synced_at)

    watch_users = users if users is not None else list_watch_users()
    total = len(watch_users)
    if total == 0:
        raise RuntimeError("监控用户列表为空，请先在设置中添加用户")

    state = watch_resume.load_state()
    resuming = bool(state and state["active"])
    if not resuming:
        if state and state["next_window_start"] is not None:
            # Time spent catching up is still owed, even if it took > ten days.
            window_start = state["next_window_start"]
            window_end = min(sync_started_at, window_start + MAX_WINDOW_SECONDS)
        state = watch_resume.new_state(window_start, window_end, sync_started_at)
    window_start, window_end = state["window_start"], state["window_end"]
    state["observed_at"] = max(state["observed_at"], sync_started_at)
    current_uids = {str(user.mid) for user in watch_users}
    state["users"] = {uid: value for uid, value in state["users"].items() if uid in current_uids}
    for user in watch_users:
        uid = str(user.mid)
        if uid not in state["users"]:
            # A newly configured UID gets the existing first/incremental window,
            # never another UID's cursor or a stale catch-up window.
            start, end = compute_sync_window(now=sync_started_at, last_synced_at=last_synced_at)
            if not resuming:
                start, end = window_start, window_end
            state["users"][uid] = watch_resume.new_user(start, end)
    watch_resume.save_state(state)

    user_results: list[WatchUserScanResult] = []

    with BilibiliClient(timeout=30.0) as client:
        for index, user in enumerate(watch_users, start=1):
            scan = state["users"][str(user.mid)]

            def before_page() -> None:
                if on_progress:
                    # Also serves the existing action cancellation check, per page.
                    on_progress(index, total, f"正在扫描 {user.name} 的转发动态…")

            def save_page(page: ForwardScanResult, *, discovery: bool = False) -> None:
                state["pending_links"] = _dedupe_activity_links(
                    state["pending_links"] + [item.url for item in page.links]
                )
                if not discovery:
                    scan.update(offset=page.next_offset, complete=page.complete, seen_offsets=page.seen_offsets)
                watch_resume.save_state(state)

            before_page()
            budget = DEFAULT_MAX_PAGES
            forwards = []
            if resuming and (scan["complete"] or scan["offset"]):
                # Bounded discovery only, NOT completion evidence. Unsampled new
                # pages remain owed by the successor window after old catch-up.
                head = collect_forward_links_for_user(
                    client, user.mid,
                    since_ts=max(window_end, sync_started_at - MAX_WINDOW_SECONDS),
                    until_ts=sync_started_at, max_pages=1,
                    on_page=lambda page: save_page(page, discovery=True),
                    before_page=before_page,
                )
                forwards.extend(head.links)
                budget -= head.pages
                if head.complete and not scan["complete"]:
                    # The collector already delays after non-terminal pages.
                    # A terminal discovery page must not create a faster burst.
                    time.sleep(PAGE_REQUEST_DELAY)
            if not scan["complete"]:
                result = collect_forward_links_for_user(
                    client,
                    user.mid,
                    since_ts=scan["since_ts"],
                    until_ts=scan["until_ts"],
                    offset=scan["offset"],
                    seen_offsets=scan["seen_offsets"],
                    max_pages=budget,
                    on_page=save_page,
                    before_page=before_page,
                )
                forwards.extend(result.links)
            links = [item.url for item in forwards if is_valid_dynamic_id(item.dynamic_id)]
            message = (
                "历史续读完成" if scan["complete"] and resuming else
                "扫描完成" if scan["complete"] else
                "达到本轮扫描上限，已保存续读位置，下次继续"
            )
            if on_progress:
                on_progress(index, total, f"{user.name}：{message}")
            user_results.append(
                WatchUserScanResult(
                    mid=user.mid, name=user.name, ok=scan["complete"],
                    link_count=len(links), forward_count=len(forwards), message=message,
                )
            )
            if index < total:
                time.sleep(USER_REQUEST_DELAY)

    activity_links = state["pending_links"]
    source_complete = all(user["complete"] for user in state["users"].values())

    return WatchSyncResult(
        source_id=WATCH_SOURCE_ID,
        synced_at=sync_started_at,
        window_start=window_start,
        window_end=window_end,
        activity_links=activity_links,
        users_total=total,
        users_ok=sum(item.ok for item in user_results),
        users_failed=[],
        user_results=[item.to_dict() for item in user_results],
        link_count=len(activity_links),
        source_complete=source_complete,
        resume_token=state["token"],
    )


def save_watch_result(result: WatchSyncResult) -> WatchSyncResult:
    from src.db.snapshots import save_watch_sync_dict

    payload = {
        "source_id": result.source_id,
        "synced_at": result.synced_at,
        "window_start": result.window_start,
        "window_end": result.window_end,
        "checked_at": result.synced_at,
        "activity_links": result.activity_links,
        "link_count": result.link_count,
        "users_total": result.users_total,
        "users_ok": result.users_ok,
        "users_failed": result.users_failed,
        "user_results": result.user_results,
        "source_complete": result.source_complete,
    }
    save_watch_sync_dict(payload)
    return result
