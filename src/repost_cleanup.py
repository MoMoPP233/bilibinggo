"""当前 Profile 的个人转发索引与严格过期清理。

本模块只把能够通过官方结构化抽奖信息确认安全的互动/预约抽奖列为候选。
普通转发抽奖仍会进入历史索引，但不会仅凭 LLM 结果进入删除候选。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from src.activity_store import load_activities
from src.bilibili_auth import require_login
from src.bilibili_client import BilibiliClient, api_code
from src.data_paths import get_runtime_profile_id
from src.db.engine import db_path
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL
from src.lottery_classifier import is_charging_lottery_activity
from src.participation_guard import get_guard
from src.participation_store import get_participation
from src.repost_history import (
    claim_delete_pending,
    get_checkpoint,
    get_repost,
    list_active_reposts,
    list_reposts_by_originals,
    list_repost_assessments,
    mark_delete_result,
    remove_repost_assessment,
    save_checkpoint,
    upsert_repost_assessment,
    upsert_repost_records,
)
from src.sources.common import is_valid_dynamic_id, opus_link
from src.watch_feed import (
    PAGE_REQUEST_DELAY,
    extract_feed_author,
    extract_feed_pub_ts,
    extract_owned_repost,
    fetch_space_feed_page,
)

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
DELETE_REPOST_URL = "https://api.bilibili.com/x/dynamic/feed/operate/remove"
EXPIRY_BUFFER_SECONDS = 3 * 24 * 60 * 60
ELIGIBLE_LOTTERY_TYPES = frozenset({"互动抽奖", "预约抽奖"})
ELIGIBLE_BUSINESS_TYPES = {"互动抽奖": 1, "预约抽奖": 10}


class ProgressCallback(Protocol):
    def __call__(
        self,
        done: int,
        total: int,
        message: str,
        log_line: str | None = None,
    ) -> None: ...


CancelCheck = Callable[[], object]


class RemoteStateUnknown(RuntimeError):
    """远程状态无法可靠读取；该条记录必须跳过。"""


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    eligible: bool
    reason: str
    lottery_type: str = ""
    lottery_time: int | None = None
    eligible_after: int | None = None


def _progress(
    callback: ProgressCallback | None,
    done: int,
    total: int,
    message: str,
    log_line: str | None = None,
) -> None:
    if callback is not None:
        callback(done, total, message, log_line)


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check is not None and cancel_check() is True:
        raise RuntimeError("任务已取消")


def _strict_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _record_dict(record: object) -> dict[str, Any]:
    if isinstance(record, Mapping):
        return dict(record)
    try:
        return asdict(record)  # type: ignore[arg-type]
    except TypeError:
        fields = (
            "uid",
            "repost_dynamic_id",
            "original_dynamic_id",
            "reposted_at",
            "original_author_uid",
            "original_author_name",
            "source",
            "delete_status",
            "delete_requested_at",
            "deleted_at",
            "last_seen_at",
            "last_error",
            "updated_at",
        )
        return {name: getattr(record, name, None) for name in fields}


def _require_verified_login(client: BilibiliClient) -> tuple[str, int]:
    csrf, cookie_uid = require_login()
    try:
        payload = client.request_json(NAV_URL, retries=0)
    except (httpx.HTTPError, RuntimeError) as exc:
        raise RuntimeError(f"无法校验当前登录账号：{exc}") from exc
    if not isinstance(payload, dict) or api_code(payload) != 0:
        raise RuntimeError("无法校验当前登录账号：NAV 响应异常")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("isLogin") is not True:
        raise RuntimeError("当前 Cookie 未登录，不能扫描或清理转发")
    nav_uid = _strict_positive_int(data.get("mid"))
    if nav_uid != cookie_uid:
        raise RuntimeError("Cookie UID 与当前登录账号不一致，已停止操作")
    return csrf, cookie_uid


def _feed_item_id(item: object) -> str | None:
    if not isinstance(item, dict):
        return None
    dynamic_id = str(item.get("id_str") or "").strip()
    return dynamic_id if is_valid_dynamic_id(dynamic_id) else None


def sync_repost_history(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    """人工增量同步当前账号的个人转发历史。"""
    _check_cancel(cancel_check)
    with client_factory() as client:
        _, uid = _require_verified_login(client)
        checkpoint = get_checkpoint(str(uid))
        old_head = str(getattr(checkpoint, "head_dynamic_id", "") or "")
        old_head_ts = _strict_positive_int(
            getattr(checkpoint, "head_published_at", None)
        )

        offset = ""
        seen_offsets: set[str] = set()
        scanned_pages = 0
        scanned_items = 0
        found_reposts = 0
        imported_count = 0
        completed = False
        newest_id: str | None = None
        newest_ts: int | None = None

        _progress(on_progress, 0, 0, "正在读取当前账号的转发历史…")
        while True:
            _check_cancel(cancel_check)
            if offset in seen_offsets:
                raise RuntimeError("空间动态分页游标重复，未推进同步检查点")
            seen_offsets.add(offset)
            data = fetch_space_feed_page(client, mid=uid, offset=offset, retries=0)
            scanned_pages += 1
            raw_items = data.get("items")
            if raw_items is None:
                raise RuntimeError("空间动态响应缺少 items，未推进同步检查点")
            if not isinstance(raw_items, list):
                raise RuntimeError("空间动态 items 格式异常，未推进同步检查点")
            scanned_items += len(raw_items)

            page_records: list[dict[str, Any]] = []
            reached_checkpoint = False
            page_timestamps: list[int | None] = []
            seen_at = int(time.time())
            for item in raw_items:
                item_id = _feed_item_id(item)
                item_ts = extract_feed_pub_ts(item) if isinstance(item, dict) else None
                page_timestamps.append(item_ts)
                if item_id and item_ts and (newest_ts is None or item_ts > newest_ts):
                    newest_id, newest_ts = item_id, item_ts
                if old_head and item_id == old_head:
                    reached_checkpoint = True

                owned = extract_owned_repost(item, uid=uid) if isinstance(item, dict) else None
                if owned is None:
                    continue
                found_reposts += 1
                page_records.append(
                    {
                        "uid": str(uid),
                        "repost_dynamic_id": owned.repost_dynamic_id,
                        "original_dynamic_id": owned.original_dynamic_id,
                        "reposted_at": owned.reposted_at,
                        "original_author_uid": owned.original_author_uid,
                        "original_author_name": owned.original_author_name,
                        "source": "history_import",
                        "delete_status": "active",
                        "last_seen_at": seen_at,
                        "updated_at": seen_at,
                    }
                )

            # Feed 中旧 head 可能已被用户手工删除。只有整页每一条都有可靠
            # 时间，且全部严格早于旧锚点时，才以时间作为保守回退停止条件。
            # 本页仍完整导入，避免分页边界附近的新转发被漏掉。
            if (
                old_head
                and old_head_ts is not None
                and raw_items
                and all(timestamp is not None for timestamp in page_timestamps)
                and all(timestamp < old_head_ts for timestamp in page_timestamps if timestamp)
            ):
                reached_checkpoint = True
            if page_records:
                imported_count += int(
                    upsert_repost_records(str(uid), page_records, seen_at=seen_at)
                )

            _progress(
                on_progress,
                scanned_pages,
                0,
                f"已扫描 {scanned_pages} 页，发现 {found_reposts} 条自己的转发",
            )
            if reached_checkpoint:
                completed = True
                break
            next_offset = str(data.get("offset") or "").strip()
            if not raw_items or not next_offset:
                completed = True
                break
            offset = next_offset
            if PAGE_REQUEST_DELAY > 0:
                time.sleep(PAGE_REQUEST_DELAY)

        _check_cancel(cancel_check)
        saved_checkpoint = checkpoint
        if completed:
            # 空账号保留旧锚点；首次空扫描仍以 full_scan_completed 标记完成。
            saved_checkpoint = save_checkpoint(
                str(uid),
                head_dynamic_id=newest_id or old_head or None,
                head_published_at=newest_ts
                if newest_ts is not None
                else getattr(checkpoint, "head_published_at", None),
                full_scan_completed=bool(
                    getattr(checkpoint, "full_scan_completed", False) or not old_head
                ),
                last_synced_at=int(time.time()),
            )

    checkpoint_payload = None
    if saved_checkpoint is not None:
        checkpoint_payload = {
            "head_dynamic_id": getattr(saved_checkpoint, "head_dynamic_id", None),
            "head_published_at": getattr(saved_checkpoint, "head_published_at", None),
        }
    return {
        "uid": str(uid),
        "scanned_pages": scanned_pages,
        "scanned_items": scanned_items,
        "found_reposts": found_reposts,
        "imported_count": imported_count,
        "full_scan_completed": bool(
            getattr(saved_checkpoint, "full_scan_completed", completed)
        ),
        "checkpoint": checkpoint_payload,
    }


def _fetch_dynamic_item_strict(
    client: BilibiliClient,
    dynamic_id: str,
) -> dict[str, Any]:
    try:
        payload = client.get_json(
            DYNAMIC_DETAIL_URL,
            {"id": dynamic_id},
            referer=opus_link(dynamic_id),
            retries=0,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise RemoteStateUnknown(f"动态详情读取失败：{exc}") from exc
    if not isinstance(payload, dict) or api_code(payload) != 0:
        raise RemoteStateUnknown("动态详情响应异常")
    data = payload.get("data")
    item = data.get("item") if isinstance(data, dict) else None
    if not isinstance(item, dict):
        raise RemoteStateUnknown("动态详情缺少 item")
    returned_id = str(item.get("id_str") or "").strip()
    if returned_id != dynamic_id:
        raise RemoteStateUnknown("动态详情 ID 与请求目标不一致")
    return item


def _fetch_notice_strict(
    client: BilibiliClient,
    *,
    original_dynamic_id: str,
    business_id: str,
    business_type: int,
) -> dict[str, Any]:
    try:
        payload = client.request_json(
            LOTTERY_NOTICE_URL,
            {"business_id": business_id, "business_type": business_type},
            referer=opus_link(original_dynamic_id),
            retries=0,
        )
    except (httpx.HTTPError, RuntimeError) as exc:
        raise RemoteStateUnknown(f"抽奖结果读取失败：{exc}") from exc
    if not isinstance(payload, dict) or api_code(payload) != 0:
        raise RemoteStateUnknown("lottery_notice 响应异常")
    notice = payload.get("data")
    if not isinstance(notice, dict) or not _strict_positive_int(notice.get("lottery_id")):
        raise RemoteStateUnknown("lottery_notice 缺少有效抽奖信息")
    return notice


def _complete_winner_uids(notice: Mapping[str, Any]) -> set[str] | None:
    """只有奖项人数与完整结果逐项一致时才返回中奖 UID 集。"""
    result = notice.get("lottery_result")
    if not isinstance(result, dict) or not result:
        return None
    mappings = (
        ("first_prize", "first_prize_result"),
        ("second_prize", "second_prize_result"),
        ("third_prize", "third_prize_result"),
    )
    expected_total = 0
    winners: set[str] = set()
    for count_key, result_key in mappings:
        raw_count = notice.get(count_key, 0)
        if isinstance(raw_count, bool):
            return None
        try:
            count = int(raw_count or 0)
        except (TypeError, ValueError):
            return None
        if count < 0:
            return None
        entries = result.get(result_key, [])
        if not isinstance(entries, list) or len(entries) != count:
            return None
        expected_total += count
        for entry in entries:
            if not isinstance(entry, dict):
                return None
            winner_uid = _strict_positive_int(entry.get("uid"))
            if winner_uid is None:
                return None
            winners.add(str(winner_uid))
    return winners if expected_total > 0 else None


def _assess_activity(
    client: BilibiliClient,
    *,
    activity: Mapping[str, Any],
    uid: str,
    now_ts: int,
) -> CandidateAssessment:
    dynamic_id = str(activity.get("dynamic_id") or "").strip()
    lottery_type = str(activity.get("lottery_type") or "").strip()
    if lottery_type not in ELIGIBLE_LOTTERY_TYPES:
        return CandidateAssessment(False, "仅官方互动/预约抽奖可自动判断")
    if activity.get("skipped") or not activity.get("status_classified"):
        return CandidateAssessment(False, "活动未完成可靠分类")
    if is_charging_lottery_activity(dict(activity)):
        return CandidateAssessment(False, "充电抽奖不可清理")
    conditions = activity.get("conditions")
    if isinstance(conditions, dict) and conditions.get("lottery_time_inferred") is True:
        return CandidateAssessment(False, "开奖时间为推断值")

    participation = get_participation(dynamic_id, uid=uid)
    if participation is not None and participation.user_status == "未参加":
        return CandidateAssessment(False, "本地参与状态与转发历史冲突")
    guard = get_guard(uid, dynamic_id)
    if guard is not None and guard.repost_status in {"pending", "unknown", "suspected"}:
        return CandidateAssessment(False, f"转发保护状态为 {guard.repost_status}")

    _fetch_dynamic_item_strict(client, dynamic_id)

    expected_business_type = ELIGIBLE_BUSINESS_TYPES[lottery_type]
    business_id = str(activity.get("business_id") or "").strip()
    try:
        business_type = int(activity.get("business_type"))
    except (TypeError, ValueError):
        return CandidateAssessment(False, "缺少可靠 lottery_notice 业务标识")
    if not business_id or business_type != expected_business_type:
        return CandidateAssessment(False, "lottery_notice 业务标识不一致")
    if lottery_type == "互动抽奖" and business_id != dynamic_id:
        return CandidateAssessment(False, "互动抽奖业务 ID 与原动态不一致")

    notice = _fetch_notice_strict(
        client,
        original_dynamic_id=dynamic_id,
        business_id=business_id,
        business_type=business_type,
    )
    status = notice.get("status")
    if isinstance(status, bool):
        return CandidateAssessment(False, "官方开奖状态不明确")
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return CandidateAssessment(False, "官方开奖状态不明确")
    if status_int == 0:
        return CandidateAssessment(False, "官方抽奖尚未明确结束")

    lottery_time = _strict_positive_int(notice.get("lottery_time"))
    if lottery_time is None:
        return CandidateAssessment(False, "官方开奖时间缺失")
    winners = _complete_winner_uids(notice)
    if winners is None:
        return CandidateAssessment(False, "官方中奖结果不完整")
    if uid in winners:
        return CandidateAssessment(False, "当前账号在中奖名单中")
    eligible_after = lottery_time + EXPIRY_BUFFER_SECONDS
    if now_ts < eligible_after:
        return CandidateAssessment(
            False,
            "开奖后安全缓冲期未满 3 天",
            lottery_type,
            lottery_time,
            eligible_after,
        )
    return CandidateAssessment(
        True,
        "官方已开奖、中奖名单完整且当前账号未中奖，开奖后已超过 3 天",
        lottery_type,
        lottery_time,
        eligible_after,
    )


def _candidate_dict(record: object, assessment: CandidateAssessment) -> dict[str, Any]:
    row = _record_dict(record)
    return {
        "uid": str(row.get("uid") or ""),
        "repost_dynamic_id": str(row.get("repost_dynamic_id") or ""),
        "original_dynamic_id": str(row.get("original_dynamic_id") or ""),
        "reposted_at": row.get("reposted_at"),
        "original_author_uid": row.get("original_author_uid"),
        "original_author_name": row.get("original_author_name"),
        "lottery_type": assessment.lottery_type,
        "lottery_time": assessment.lottery_time,
        "eligible_after": assessment.eligible_after,
        "reason": assessment.reason,
    }


def _persist_assessment(
    uid: str,
    original_id: str,
    assessment: CandidateAssessment,
) -> None:
    if assessment.eligible:
        upsert_repost_assessment(
            uid,
            original_id,
            lottery_type=assessment.lottery_type or None,
            lottery_time=assessment.lottery_time,
            eligible_after=assessment.eligible_after,
            reason=assessment.reason,
        )
    else:
        remove_repost_assessment(uid, original_id)


def scan_expired_reposts(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    """扫描本地转发索引，只返回严格、可重新验证的安全候选。"""
    current = int(now_ts if now_ts is not None else time.time())
    _check_cancel(cancel_check)
    with client_factory() as client:
        _, uid_int = _require_verified_login(client)
        uid = str(uid_int)
        records = list_active_reposts(uid)
        activities = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        candidates: list[dict[str, Any]] = []
        assessment_cache: dict[str, CandidateAssessment] = {}
        total = len(records)
        _progress(on_progress, 0, total, f"正在检查 {total} 条个人转发…")
        for index, record in enumerate(records, 1):
            _check_cancel(cancel_check)
            row = _record_dict(record)
            original_id = str(row.get("original_dynamic_id") or "").strip()
            assessment = assessment_cache.get(original_id)
            if assessment is None:
                activity = activities.get(original_id)
                if activity is None:
                    assessment = CandidateAssessment(False, "原动态不在本地抽奖活动库")
                    remove_repost_assessment(uid, original_id)
                else:
                    try:
                        assessment = _assess_activity(
                            client,
                            activity=activity,
                            uid=uid,
                            now_ts=current,
                        )
                    except RemoteStateUnknown as exc:
                        assessment = CandidateAssessment(False, str(exc))
                        # 远程状态无法确认时保留既有评估，避免误清除有效候选。
                    else:
                        _persist_assessment(uid, original_id, assessment)
                assessment_cache[original_id] = assessment
            if assessment.eligible:
                candidates.append(_candidate_dict(record, assessment))
            _progress(
                on_progress,
                index,
                total,
                f"正在检查过期转发 ({index}/{total})",
            )

    return {
        "uid": uid,
        "scanned": len(records),
        "eligible_count": len(candidates),
        "skipped_count": len(records) - len(candidates),
        "candidates": candidates,
    }


def load_persisted_candidates(uid: str) -> list[dict[str, Any]]:
    """从本地评估结果重建候选；不访问任何远程接口，也不重新扫描。"""
    scoped_uid = str(uid).strip()
    assessments = list_repost_assessments(scoped_uid)
    if not assessments:
        return []
    reposts = list_reposts_by_originals(scoped_uid, assessments.keys())
    candidates: list[dict[str, Any]] = []
    for repost in reposts:
        assessment = assessments[repost.original_dynamic_id]
        candidates.append(
            {
                "uid": scoped_uid,
                "repost_dynamic_id": repost.repost_dynamic_id,
                "original_dynamic_id": repost.original_dynamic_id,
                "reposted_at": repost.reposted_at,
                "original_author_uid": repost.original_author_uid,
                "original_author_name": repost.original_author_name,
                "lottery_type": assessment.lottery_type,
                "lottery_time": assessment.lottery_time,
                "eligible_after": assessment.eligible_after,
                "reason": assessment.reason,
                "delete_status": repost.delete_status,
            }
        )
    return candidates


def _assert_frozen_runtime(*, profile_id: str, database_path: str, uid: str) -> str:
    if get_runtime_profile_id() != profile_id or str(db_path().resolve()) != database_path:
        raise RuntimeError("运行中的 Profile 已变化，已停止删除")
    csrf, current_uid = require_login()
    if str(current_uid) != uid:
        raise RuntimeError("当前登录 UID 已变化，已停止删除")
    return csrf


def _verify_owned_repost_detail(
    client: BilibiliClient,
    *,
    uid: str,
    repost_dynamic_id: str,
    original_dynamic_id: str,
) -> None:
    item = _fetch_dynamic_item_strict(client, repost_dynamic_id)
    if item.get("type") != "DYNAMIC_TYPE_FORWARD":
        raise RemoteStateUnknown("目标已不是明确的转发动态")
    author_uid, _ = extract_feed_author(item)
    if author_uid != uid:
        raise RemoteStateUnknown("目标动态不属于当前账号")
    orig = item.get("orig")
    if not isinstance(orig, dict):
        raise RemoteStateUnknown("目标动态缺少原动态关系")
    returned_original_id = str(orig.get("id_str") or "").strip()
    if returned_original_id != original_dynamic_id:
        raise RemoteStateUnknown("转发与原动态对应关系不一致")


def _delete_failure_item(
    *,
    repost_id: str,
    original_id: str,
    status: str,
    message: str,
) -> dict[str, str]:
    return {
        "repost_dynamic_id": repost_id,
        "original_dynamic_id": original_id,
        "status": status,
        "message": message,
    }


def delete_reposts(
    repost_dynamic_ids: Sequence[str],
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    """逐条删除人工确认的个人转发；任何不确定结果均不自动重试。"""
    requested: list[str] = []
    seen: set[str] = set()
    for raw in repost_dynamic_ids:
        repost_id = str(raw or "").strip()
        if repost_id not in seen:
            seen.add(repost_id)
            requested.append(repost_id)

    _check_cancel(cancel_check)
    profile_id = get_runtime_profile_id()
    database_path = str(db_path().resolve())
    items: list[dict[str, str]] = []
    with client_factory() as client:
        _, uid_int = _require_verified_login(client)
        uid = str(uid_int)
        activity_map = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        total = len(requested)
        _progress(on_progress, 0, total, f"准备逐条验证并删除 {total} 条转发…")

        for index, repost_id in enumerate(requested, 1):
            _check_cancel(cancel_check)
            original_id = ""
            status = "skipped"
            message = "安全检查未通过"
            try:
                csrf = _assert_frozen_runtime(
                    profile_id=profile_id,
                    database_path=database_path,
                    uid=uid,
                )
                if not is_valid_dynamic_id(repost_id):
                    raise ValueError("转发动态 ID 无效")
                record = get_repost(uid, repost_id)
                if record is None:
                    raise ValueError("转发历史中不存在该目标")
                row = _record_dict(record)
                original_id = str(row.get("original_dynamic_id") or "").strip()
                if not is_valid_dynamic_id(original_id) or original_id == repost_id:
                    raise ValueError("转发与原动态 ID 无法安全区分")
                if str(row.get("delete_status") or "") not in {"active", "delete_failed"}:
                    raise ValueError("当前删除状态不允许再次提交")

                _verify_owned_repost_detail(
                    client,
                    uid=uid,
                    repost_dynamic_id=repost_id,
                    original_dynamic_id=original_id,
                )
                activity = activity_map.get(original_id)
                if activity is None:
                    raise ValueError("原动态不在本地抽奖活动库")
                assessment = _assess_activity(
                    client,
                    activity=activity,
                    uid=uid,
                    now_ts=int(time.time()),
                )
                if not assessment.eligible:
                    raise ValueError(assessment.reason)

                # 先持久化 pending；占用失败时绝不能发送删除请求。
                if not claim_delete_pending(uid, repost_id, requested_at=int(time.time())):
                    raise ValueError("未能持久化 delete_pending，已禁止发送删除请求")

                try:
                    payload = client.post_json(
                        DELETE_REPOST_URL,
                        {"dyn_id_str": repost_id},
                        params={"platform": "web", "csrf": csrf},
                        referer=f"https://space.bilibili.com/{uid}/dynamic",
                        retries=0,
                        raise_on_code=False,
                    )
                except Exception as exc:
                    error = f"删除结果不确定：{type(exc).__name__}: {exc}"
                    try:
                        mark_delete_result(uid, repost_id, status="unknown", error=error)
                    except Exception:
                        # 原 pending 已足以阻止自动重发。
                        pass
                    status, message = "unknown", error
                else:
                    code = payload.get("code") if isinstance(payload, dict) else None
                    if type(code) is not int:
                        error = "删除响应缺少有效整数 code，结果不确定"
                        try:
                            mark_delete_result(uid, repost_id, status="unknown", error=error)
                        except Exception:
                            pass
                        status, message = "unknown", error
                    elif code == 0:
                        save_error = ""
                        try:
                            saved = mark_delete_result(
                                uid,
                                repost_id,
                                status="deleted",
                                deleted_at=int(time.time()),
                            )
                        except Exception as exc:
                            saved = False
                            save_error = f"平台已返回成功，但本地状态保存失败：{exc}"
                        if saved:
                            status, message = "deleted", "删除成功"
                        else:
                            status = "unknown"
                            message = save_error or "平台已返回成功，但本地状态未确认"
                    else:
                        api_message = str(payload.get("message") or payload.get("msg") or "").strip()
                        error = f"Bilibili API error {code}: {api_message}".rstrip()
                        try:
                            saved = mark_delete_result(
                                uid,
                                repost_id,
                                status="delete_failed",
                                error=error,
                            )
                        except Exception as exc:
                            saved = False
                            error = f"{error}；本地状态保存失败：{exc}"
                        status, message = ("delete_failed" if saved else "unknown"), error
            except (RemoteStateUnknown, RuntimeError, ValueError) as exc:
                status, message = "skipped", str(exc)

            items.append(
                _delete_failure_item(
                    repost_id=repost_id,
                    original_id=original_id,
                    status=status,
                    message=message,
                )
            )
            _progress(
                on_progress,
                index,
                total,
                f"删除进度 ({index}/{total})",
                f"{repost_id}：{message}",
            )

    return {
        "uid": uid,
        "requested_count": len(requested),
        "deleted_count": sum(item["status"] == "deleted" for item in items),
        "failed_count": sum(item["status"] == "delete_failed" for item in items),
        "unknown_count": sum(item["status"] == "unknown" for item in items),
        "skipped_count": sum(item["status"] == "skipped" for item in items),
        "items": items,
    }


__all__ = [
    "CandidateAssessment",
    "DELETE_REPOST_URL",
    "EXPIRY_BUFFER_SECONDS",
    "RemoteStateUnknown",
    "delete_reposts",
    "load_persisted_candidates",
    "scan_expired_reposts",
    "sync_repost_history",
]
