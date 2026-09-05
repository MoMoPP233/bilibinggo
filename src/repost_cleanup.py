"""当前 Profile 的个人转发索引与三级清理评估（V2）。

safe        : 程序能够证明安全可删（官方已开奖、中奖名单完整且当前账号未中奖、超 3 天）。
manual_review: 已确认属于历史抽奖，但中奖/领奖状态无法自动确认，由用户人工决定。
blocked     : 身份关系或关键安全信息冲突/歧义，禁止删除。
excluded    : 明确非抽奖/充电抽奖，不计入清理候选。
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
from src.data_paths import get_runtime_profile_id, get_selected_profile_id
from src.db.engine import db_path
from src.lottery_api import DYNAMIC_DETAIL_URL, LOTTERY_NOTICE_URL
from src.lottery_classifier import is_charging_lottery_activity
from src.participation_guard import (
    get_guard,
    list_confirmed_guards_needing_history_sync,
    reconcile_uncertain_guards_from_history,
)
from src.participation_store import get_participation
from src.pipeline.classify_step import ClassifyOutcome, classify_for_cleanup
from src.repost_history import (
    claim_delete_pending,
    clear_maintenance_risk,
    clear_sync_needed_if_unchanged,
    count_repost_delete_statuses,
    defer_repost,
    get_checkpoint,
    get_repost,
    list_evaluable_reposts,
    list_deleted_reposts,
    list_reposts_needing_identity,
    list_reposts_by_originals,
    list_repost_assessments,
    list_repost_issues,
    mark_delete_result,
    pause_maintenance_risk,
    remove_repost_assessment,
    restore_repost,
    save_checkpoint,
    set_repost_identity,
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
ASSESSMENT_BUDGET_PER_ROUND = 40
MAX_IDENTITY_CHECKS_PER_ROUND = 20
MAX_ASSESSMENTS_PER_JOB = 120
AUTO_ASSESSMENT_BUDGET = 20
MANUAL_REVIEW_DEFER_SECONDS = 30 * 24 * 60 * 60
CONSECUTIVE_FAILURE_LIMIT = 3
MAX_DELETE_BATCH = 20
HEALTH_ISSUE_LIMIT = 20
TRUSTED_IDENTITY_SOURCES = frozenset({"space_feed", "legacy_space_feed"})
RETRYABLE_ASSESSMENT_REASON_CODES = frozenset({"remote_unknown"})
PERMANENT_IDENTITY_ERROR_MARKERS = (
    "不属于当前账号",
    "不是明确的转发动态",
    "对应关系不一致",
    "缺少原动态关系",
    "转发动态 ID 无效",
)


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


class AssessmentRateLimited(RuntimeError):
    """本轮评估/删除命中平台限流或风控，必须立即停止后续同类请求。"""


@dataclass(frozen=True, slots=True)
class CandidateAssessment:
    level: str
    reason: str
    reason_code: str = ""
    lottery_type: str = ""
    lottery_time: int | None = None
    eligible_after: int | None = None
    lottery_time_reliable: bool = False
    classification_source: str = "activities"
    summary: str = ""
    remote_checked_at: int | None = None

    @property
    def eligible(self) -> bool:
        return self.level == "safe"


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
    if not isinstance(payload, dict):
        raise RuntimeError("无法校验当前登录账号：NAV 响应格式异常")
    code = api_code(payload)
    if code != 0:
        message = str(payload.get("message") or payload.get("msg") or "未知错误")
        raise RuntimeError(f"无法校验当前登录账号：NAV API error {code}: {message}")
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
    sync_started_at = int(time.time())
    _check_cancel(cancel_check)
    _, local_uid = require_login()  # 只读当前 runtime Profile Cookie，先捕获 dirty 水位。
    initial_checkpoint = get_checkpoint(str(local_uid))
    initial_sync_needed = bool(getattr(initial_checkpoint, "sync_needed", False))
    consumed_sync_needed_at = (
        getattr(initial_checkpoint, "sync_needed_at", None)
        if initial_sync_needed
        else None
    )
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
                # 秒级时间无法区分“同步开始”和同秒 confirmed 的先后；把覆盖水位
                # 保守地停在开始前 1 秒，最多多补同步一次，不能永久漏同步。
                last_synced_at=max(1, sync_started_at - 1),
            )

    guard_reconciliation = {"checked": 0, "resolved": 0, "remaining": 0}
    if completed:
        # history 已完整落库后才允许做纯本地、单向的 uncertain → confirmed。
        # 不调用 confirm_repost，避免把已经在 history 中的旧转发重新标成待同步。
        guard_reconciliation = reconcile_uncertain_guards_from_history(str(uid))

    dirty_cleared = False
    if completed and initial_sync_needed:
        dirty_cleared = clear_sync_needed_if_unchanged(
            str(uid),
            expected_sync_needed_at=consumed_sync_needed_at,
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
        "sync_started_at": sync_started_at,
        "consumed_sync_needed_at": consumed_sync_needed_at,
        "sync_needed_cleared": dirty_cleared,
        "guard_reconciliation": guard_reconciliation,
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
        # 明确平台风控原样保留（不得转成 notice_unreadable/manual_review）。
        risk = _risk_code_from_message(exc)
        if risk is not None:
            raise AssessmentRateLimited(
                f"原动态读取触发平台限制（{risk}）：{exc}"
            ) from exc
        raise RemoteStateUnknown(f"动态详情读取失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RemoteStateUnknown("动态详情响应异常")
    code = payload.get("code")
    if code in (-352, -509, 429):
        raise AssessmentRateLimited(
            f"原动态读取触发平台限制（{code}）：{payload.get('message') or ''}"
        )
    if api_code(payload) != 0:
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
        # 明确平台风控必须原样保留（不得转成普通 notice_unreadable/manual_review）。
        risk = _risk_code_from_message(exc)
        if risk is not None:
            raise AssessmentRateLimited(
                f"抽奖结果读取触发平台限制（{risk}）：{exc}"
            ) from exc
        raise RemoteStateUnknown(f"抽奖结果读取失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RemoteStateUnknown("lottery_notice 响应异常")
    code = payload.get("code")
    if code in (-352, -509, 429):
        raise AssessmentRateLimited(
            f"抽奖结果读取触发平台限制（{code}）：{payload.get('message') or ''}"
        )
    if api_code(payload) != 0:
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


def _risk_code_from_message(message: object) -> int | None:
    """结构化风控码判定（统一 src.platform_risk 规则），不扫描正文文本。"""
    from src.platform_risk import risk_code_of

    if isinstance(message, int):
        return message if message in (-352, -509) else None
    return risk_code_of(message)


class _AssessmentBreaker:
    """串行评估/删除的风控熔断器：-352/429 立即停止，连续异常达阈值也停止。"""

    def __init__(self, *, consecutive_limit: int = CONSECUTIVE_FAILURE_LIMIT) -> None:
        self.consecutive_limit = max(1, int(consecutive_limit))
        self.consecutive_failures = 0
        self.rate_limited = False
        self.stopped = False
        self.stop_message = ""

    def note_success(self) -> None:
        self.consecutive_failures = 0

    def note_failure(self, exc_or_code: object) -> None:
        risk = _risk_code_from_message(exc_or_code)
        if risk is None and isinstance(exc_or_code, int):
            risk = exc_or_code if isinstance(exc_or_code, int) and exc_or_code in (-352, -509, -799, 429) else None
        if risk is not None:
            self.rate_limited = True
            self.stopped = True
            self.stop_message = (
                f"本轮操作因平台限流/风控提前停止（{risk}），已经完成的结果已保存。"
            )
            raise AssessmentRateLimited(self.stop_message)
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.consecutive_limit:
            self.stopped = True
            self.stop_message = (
                "本轮操作因连续远程异常提前停止，已经完成的结果已保存。"
            )
            raise AssessmentRateLimited(self.stop_message)


def _summary_from_content(content: object) -> str:
    text = " ".join(str(content or "").split())
    return text[:120] if text else ""


def _activity_summary(activity: Mapping[str, Any]) -> str:
    url = str(activity.get("source_url") or "").strip()
    return url[:200] if url else ""


def _assess_confirmed_lottery(
    *,
    lottery_type: str,
    notice: Mapping[str, Any],
    uid: str,
    now_ts: int,
    classification_source: str,
    summary: str,
) -> CandidateAssessment:
    """官方互动/预约抽奖的严格安全判定；结果不完整一律 manual_review。"""
    remote_at = int(time.time())
    status = notice.get("status")
    if isinstance(status, bool):
        return CandidateAssessment(
            "manual_review", "官方开奖状态不明确，无法自动确认是否中奖",
            "notice_status_unclear", lottery_type,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
        )
    try:
        status_int = int(status)
    except (TypeError, ValueError):
        return CandidateAssessment(
            "manual_review", "官方开奖状态不明确，无法自动确认是否中奖",
            "notice_status_unclear", lottery_type,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
        )
    if status_int == 0:
        return CandidateAssessment(
            "manual_review", "官方抽奖尚未明确结束，无法自动确认是否中奖",
            "notice_not_ended", lottery_type,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
        )
    lottery_time = _strict_positive_int(notice.get("lottery_time"))
    if lottery_time is None:
        return CandidateAssessment(
            "manual_review", "官方开奖时间缺失，无法自动确认是否中奖",
            "notice_time_missing", lottery_type,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
        )
    winners = _complete_winner_uids(notice)
    if winners is None:
        return CandidateAssessment(
            "manual_review", "官方中奖结果不完整，无法确认当前账号是否中奖",
            "notice_winners_incomplete", lottery_type, lottery_time,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
            lottery_time_reliable=True,
        )
    if uid in winners:
        return CandidateAssessment(
            "blocked", "当前账号在中奖名单中，请先领奖，禁止删除",
            "current_account_won", lottery_type, lottery_time,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
            lottery_time_reliable=True,
        )
    eligible_after = lottery_time + EXPIRY_BUFFER_SECONDS
    if now_ts < eligible_after:
        return CandidateAssessment(
            "manual_review", "开奖后安全缓冲期未满 3 天",
            "buffer_not_elapsed", lottery_type, lottery_time, eligible_after,
            classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
            lottery_time_reliable=True,
        )
    return CandidateAssessment(
        "safe", "官方已开奖、中奖名单完整且当前账号未中奖，开奖后已超过 3 天",
        "safe_official_notice", lottery_type, lottery_time, eligible_after,
        classification_source=classification_source, summary=summary, remote_checked_at=remote_at,
        lottery_time_reliable=True,
    )


def _assess_from_activity(
    client: BilibiliClient,
    *,
    activity: Mapping[str, Any],
    uid: str,
    now_ts: int,
) -> CandidateAssessment | None:
    """activities 可靠记录直接复用；不可靠时返回 None 交给公共分类兜底。"""
    dynamic_id = str(activity.get("dynamic_id") or "").strip()
    lottery_type = str(activity.get("lottery_type") or "").strip()
    summary = _activity_summary(activity)
    if lottery_type == "充电抽奖" or is_charging_lottery_activity(dict(activity)):
        return CandidateAssessment(
            "excluded", "充电抽奖不可清理", "charging_lottery",
            classification_source="activities", summary=summary,
        )
    if lottery_type == "转发抽奖":
        if activity.get("skipped") or not activity.get("status_classified"):
            return None
        return CandidateAssessment(
            "manual_review",
            "已确认属于历史抽奖，但程序无法确认是否中奖/是否仍需领奖，请人工查看原动态后决定是否删除",
            "forward_lottery_manual", lottery_type,
            classification_source="activities", summary=summary,
        )
    if lottery_type not in ELIGIBLE_LOTTERY_TYPES:
        if activity.get("skipped") or not activity.get("status_classified"):
            return None
        return CandidateAssessment(
            "excluded", "非可清理抽奖类型", "non_cleanup_type",
            classification_source="activities", summary=summary,
        )
    if activity.get("skipped") or not activity.get("status_classified"):
        return None
    conditions = activity.get("conditions")
    if isinstance(conditions, dict) and conditions.get("lottery_time_inferred") is True:
        return CandidateAssessment(
            "manual_review", "开奖时间为推断值，无法自动确认安全",
            "lottery_time_inferred", lottery_type,
            classification_source="activities", summary=summary,
        )
    participation = get_participation(dynamic_id, uid=uid)
    if participation is not None and participation.user_status == "未参加":
        return CandidateAssessment(
            "blocked", "本地参与状态与转发历史冲突",
            "participation_conflict", lottery_type,
            classification_source="activities", summary=summary,
        )
    guard = get_guard(uid, dynamic_id)
    if guard is not None and guard.repost_status in {"pending", "unknown", "suspected"}:
        return CandidateAssessment(
            "blocked", f"转发保护状态为 {guard.repost_status}",
            "guard_conflict", lottery_type,
            classification_source="activities", summary=summary,
        )
    try:
        _fetch_dynamic_item_strict(client, dynamic_id)
    except RemoteStateUnknown as exc:
        return CandidateAssessment(
            "manual_review", f"原动态当前无法读取，无法自动确认是否中奖：{exc}",
            "notice_unreadable", lottery_type,
            classification_source="activities", summary=summary, remote_checked_at=int(time.time()),
        )
    expected_business_type = ELIGIBLE_BUSINESS_TYPES[lottery_type]
    business_id = str(activity.get("business_id") or "").strip()
    try:
        business_type = int(activity.get("business_type"))
    except (TypeError, ValueError):
        return CandidateAssessment(
            "manual_review", "缺少可靠 lottery_notice 业务标识，无法自动确认是否中奖",
            "notice_business_missing", lottery_type,
            classification_source="activities", summary=summary,
        )
    if not business_id or business_type != expected_business_type:
        return CandidateAssessment(
            "manual_review", "lottery_notice 业务标识不一致，无法自动确认是否中奖",
            "notice_business_mismatch", lottery_type,
            classification_source="activities", summary=summary,
        )
    if lottery_type == "互动抽奖" and business_id != dynamic_id:
        return CandidateAssessment(
            "manual_review", "互动抽奖业务 ID 与原动态不一致",
            "notice_business_mismatch", lottery_type,
            classification_source="activities", summary=summary,
        )
    try:
        notice = _fetch_notice_strict(
            client,
            original_dynamic_id=dynamic_id,
            business_id=business_id,
            business_type=business_type,
        )
    except RemoteStateUnknown as exc:
        return CandidateAssessment(
            "manual_review", f"官方抽奖结果无法读取，无法自动确认是否中奖：{exc}",
            "notice_unreadable", lottery_type,
            classification_source="activities", summary=summary, remote_checked_at=int(time.time()),
        )
    return _assess_confirmed_lottery(
        lottery_type=lottery_type,
        notice=notice,
        uid=uid,
        now_ts=now_ts,
        classification_source="activities",
        summary=summary,
    )


def _assess_via_public_classifier(
    client: BilibiliClient,
    *,
    original_id: str,
    uid: str,
    now_ts: int,
) -> CandidateAssessment:
    """activities 无可靠记录时复用公共分类能力；命中风控立即熔断。"""
    try:
        outcome, risk_code = classify_for_cleanup(client, original_id)
    except Exception as exc:
        risk = _risk_code_from_message(exc)
        if risk is not None:
            raise AssessmentRateLimited(
                f"本轮评估因平台限流/风控提前停止（{risk}），已经完成的结果已保存。"
            ) from exc
        return CandidateAssessment(
            "blocked", f"公共分类无法可靠判断原动态是否为抽奖：{exc}",
            "classify_failed", classification_source="public_classifier",
        )
    if risk_code is not None:
        raise AssessmentRateLimited(
            f"本轮评估因平台限流/风控提前停止（{risk_code}），已经完成的结果已保存。"
        )
    summary = _summary_from_content(outcome.classify_content)
    if outcome.skipped:
        if outcome.lottery_type == "充电抽奖":
            return CandidateAssessment(
                "excluded", "充电抽奖不可清理", "charging_lottery",
                classification_source="public_classifier", summary=summary,
            )
        if outcome.skip_reason == "链接失效":
            return CandidateAssessment(
                "blocked", "原动态已不可读取，无法判断是否为抽奖",
                "original_unreadable", classification_source="public_classifier", summary=summary,
            )
        return CandidateAssessment(
            "excluded", "明确非抽奖活动", "non_lottery",
            classification_source="public_classifier", summary=summary,
        )
    if outcome.lottery_type == "转发抽奖":
        return CandidateAssessment(
            "manual_review",
            "已确认属于历史抽奖，但程序无法确认是否中奖/是否仍需领奖，请人工查看原动态后决定是否删除",
            "forward_lottery_manual", outcome.lottery_type,
            classification_source="public_classifier", summary=summary,
        )
    if outcome.lottery_type not in ELIGIBLE_LOTTERY_TYPES:
        return CandidateAssessment(
            "blocked", "原动态抽奖类型无法可靠判断",
            "lottery_type_unclear", classification_source="public_classifier", summary=summary,
        )
    notice = outcome.lottery_notice
    if not notice or not outcome.notice_business_id or outcome.notice_business_type is None:
        return CandidateAssessment(
            "manual_review", "已确认属于官方抽奖，但中奖结果无法读取，无法自动确认是否中奖",
            "notice_unreadable", outcome.lottery_type,
            classification_source="public_classifier", summary=summary,
        )
    return _assess_confirmed_lottery(
        lottery_type=outcome.lottery_type,
        notice=notice,
        uid=uid,
        now_ts=now_ts,
        classification_source="public_classifier",
        summary=summary,
    )


def _assess_original(
    client: BilibiliClient,
    *,
    original_id: str,
    uid: str,
    now_ts: int,
    activities_map: Mapping[str, Any],
) -> CandidateAssessment:
    activity = activities_map.get(original_id)
    if activity is not None:
        from_activity = _assess_from_activity(
            client, activity=activity, uid=uid, now_ts=now_ts
        )
        if from_activity is not None:
            return from_activity
    return _assess_via_public_classifier(
        client, original_id=original_id, uid=uid, now_ts=now_ts
    )


def _persist_assessment(
    uid: str,
    original_id: str,
    assessment: CandidateAssessment,
) -> None:
    upsert_repost_assessment(
        uid,
        original_id,
        assessment_level=assessment.level,
        assessment_status=_assessment_status_for(assessment),
        lottery_time_reliable=assessment.lottery_time_reliable,
        reason_code=assessment.reason_code or None,
        lottery_type=assessment.lottery_type or None,
        lottery_time=assessment.lottery_time,
        eligible_after=assessment.eligible_after,
        reason=assessment.reason,
        classification_source=assessment.classification_source,
        summary=assessment.summary or None,
        evaluated_at=int(time.time()),
        remote_checked_at=assessment.remote_checked_at,
    )


def _assessment_status_for(assessment: CandidateAssessment) -> str:
    """safe/manual_review/excluded 及永久 blocked 为 final；网络类未完成可重试。"""
    if assessment.level == "blocked" and assessment.reason_code in RETRYABLE_ASSESSMENT_REASON_CODES:
        return "retryable_unknown"
    return "final"


def _identity_trusted(record: object) -> bool:
    row = _record_dict(record)
    return row.get("identity_ok") is True


def _identity_retryable(record: object) -> bool:
    row = _record_dict(record)
    if row.get("identity_ok") is True:
        return row.get("identity_source") is None
    error = str(row.get("identity_error") or "")
    return not any(marker in error for marker in PERMANENT_IDENTITY_ERROR_MARKERS)


def _normalize_force_originals(value: object) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, (list, tuple, set)):
        raise ValueError("重新评估参数无效")
    result: set[str] = set()
    for raw in value:
        original_id = str(raw or "").strip()
        if not is_valid_dynamic_id(original_id):
            raise ValueError("重新评估原动态 ID 无效")
        result.add(original_id)
    return result


def _build_pending_queue(
    *,
    candidate_originals: set[str],
    existing: Mapping[str, Any],
    activities_map: Mapping[str, Any],
) -> list[str]:
    """按优先级排队待评估 original：activities 本地便宜项在前，公共分类/LLM 项在后。"""
    pending = [
        original_id
        for original_id in candidate_originals
        if original_id not in existing
        or getattr(existing[original_id], "assessment_status", "final") == "retryable_unknown"
    ]
    return sorted(
        pending,
        key=lambda original_id: (
            0 if original_id in activities_map else 1,
            original_id,
        ),
    )


def scan_expired_reposts(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
    force_original_ids: object = None,
) -> dict[str, Any]:
    """一键智能评估：串行多批执行，单批 ≤40、单 Job ≤120；风控/取消立即整 Job 停止。"""
    current = int(now_ts if now_ts is not None else time.time())
    _check_cancel(cancel_check)
    forced = _normalize_force_originals(force_original_ids)
    breaker = _AssessmentBreaker()
    with client_factory() as client:
        _, uid_int = _require_verified_login(client)
        uid = str(uid_int)
        reposts = list_evaluable_reposts(uid)
        activities_map = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        existing = list_repost_assessments(uid)
        if forced:
            for original_id in forced:
                remove_repost_assessment(uid, original_id)
                existing.pop(original_id, None)

        identity_needed = [
            record for record in reposts
            if not _identity_trusted(record) and _identity_retryable(record)
        ]
        identity_done = 0
        for record in identity_needed:
            if identity_done >= MAX_IDENTITY_CHECKS_PER_ROUND or breaker.stopped:
                break
            _check_cancel(cancel_check)
            identity_done += 1
            repost_id = record.repost_dynamic_id
            original_id = record.original_dynamic_id
            try:
                _verify_owned_repost_detail(
                    client,
                    uid=uid,
                    repost_dynamic_id=repost_id,
                    original_dynamic_id=original_id,
                )
                set_repost_identity(uid, repost_id, ok=True, source="remote_detail", checked_at=current)
                breaker.note_success()
            except (RemoteStateUnknown, RuntimeError, ValueError) as exc:
                set_repost_identity(
                    uid, repost_id, ok=False, source="remote_detail",
                    error=str(exc), checked_at=current,
                )
                try:
                    breaker.note_failure(exc)
                except AssessmentRateLimited:
                    break

        candidate_originals = {record.original_dynamic_id for record in reposts}
        pending = _build_pending_queue(
            candidate_originals=candidate_originals,
            existing=existing,
            activities_map=activities_map,
        )
        evaluated = 0
        total_pending = len(pending)
        _progress(
            on_progress,
            0,
            min(MAX_ASSESSMENTS_PER_JOB, max(1, total_pending)),
            f"待评估原动态 {total_pending} 条，本任务预算 {MAX_ASSESSMENTS_PER_JOB} 条…",
        )
        while pending and evaluated < MAX_ASSESSMENTS_PER_JOB and not breaker.stopped:
            _check_cancel(cancel_check)
            batch = pending[:ASSESSMENT_BUDGET_PER_ROUND]
            pending = pending[ASSESSMENT_BUDGET_PER_ROUND:]
            batch_size = len(batch)
            for index, original_id in enumerate(batch, 1):
                if evaluated >= MAX_ASSESSMENTS_PER_JOB or breaker.stopped:
                    break
                _check_cancel(cancel_check)
                try:
                    assessment = _assess_original(
                        client,
                        original_id=original_id,
                        uid=uid,
                        now_ts=current,
                        activities_map=activities_map,
                    )
                except AssessmentRateLimited as exc:
                    breaker.rate_limited = True
                    breaker.stopped = True
                    breaker.stop_message = str(exc)
                    break
                except RemoteStateUnknown as exc:
                    assessment = CandidateAssessment(
                        "blocked", f"原动态状态无法可靠判断：{exc}",
                        "remote_unknown", classification_source="public_classifier",
                    )
                _persist_assessment(uid, original_id, assessment)
                evaluated += 1
                breaker.note_success()
                phase = "本地评估" if original_id in activities_map else "远程分类"
                _progress(
                    on_progress,
                    evaluated,
                    min(MAX_ASSESSMENTS_PER_JOB, max(1, total_pending)),
                    f"智能评估：本次完成 {evaluated}/{MAX_ASSESSMENTS_PER_JOB}（{phase}）",
                    f"{original_id}：{assessment.level}",
                )
            if breaker.stopped:
                break

    refreshed = list_repost_assessments(uid)
    remaining = len(
        {
            record.original_dynamic_id
            for record in reposts
            if record.original_dynamic_id not in refreshed
            or getattr(refreshed[record.original_dynamic_id], "assessment_status", "final")
            == "retryable_unknown"
        }
    )
    if breaker.stop_message:
        message = breaker.stop_message
    elif remaining:
        message = f"本次完成 {evaluated} 条，仍有 {remaining} 条待评估，可继续点击一键智能评估。"
    else:
        message = f"智能评估完成，本次共评估 {evaluated} 条原动态。"
    return {
        "uid": uid,
        "history_total": len(reposts),
        "evaluated_originals": evaluated,
        "pending_originals": remaining,
        "budget": ASSESSMENT_BUDGET_PER_ROUND,
        "job_budget": MAX_ASSESSMENTS_PER_JOB,
        "rate_limited": breaker.rate_limited,
        "stopped_early": breaker.stopped,
        "message": message,
        "candidates": load_persisted_candidates(uid),
    }


def maintenance_risk_paused(uid: str) -> bool:
    checkpoint = get_checkpoint(uid)
    return bool(getattr(checkpoint, "maintenance_risk_paused", False))


def maintenance_risk_state(uid: str) -> dict[str, Any]:
    checkpoint = get_checkpoint(uid)
    return {
        "risk_paused": bool(getattr(checkpoint, "maintenance_risk_paused", False)),
        "risk_paused_at": getattr(checkpoint, "maintenance_risk_paused_at", None),
        "risk_reason": getattr(checkpoint, "maintenance_risk_reason", None),
    }


def recover_auto_maintenance(uid: str) -> None:
    """人工恢复自动维护：只清除本地风控暂停，0 远程，恢复后不立即执行维护。"""
    clear_maintenance_risk(uid)


def auto_maintenance_paused_state() -> tuple[bool, str]:
    """只读本地 Cookie + checkpoint 判断当前 runtime Profile 是否风控暂停（0 远程）。"""
    try:
        _, uid_int = require_login()
    except RuntimeError:
        return False, ""
    checkpoint = get_checkpoint(str(uid_int))
    if bool(getattr(checkpoint, "maintenance_risk_paused", False)):
        return True, str(
            getattr(checkpoint, "maintenance_risk_reason", "") or "平台风控暂停"
        )
    return False, ""


def auto_clear_expired_maintenance_risk(
    *,
    cooldown_seconds: int,
    now_ts: int | None = None,
) -> bool:
    """cleanup 风控冷却到期后本地自动恢复调度资格（0 远程，不立即执行维护）。

    只针对自动 cleanup maintenance 的风控暂停字段（maintenance_risk_paused /
    _at / _reason），以 maintenance_risk_paused_at + cooldown_seconds 判定。
    不清除任何其它人工/删除安全状态。
    """
    try:
        _, uid_int = require_login()
    except RuntimeError:
        return False
    uid = str(uid_int)
    checkpoint = get_checkpoint(uid)
    if not bool(getattr(checkpoint, "maintenance_risk_paused", False)):
        return False
    paused_at = getattr(checkpoint, "maintenance_risk_paused_at", None)
    try:
        paused_at_int = int(paused_at)
    except (TypeError, ValueError):
        return False
    if paused_at_int <= 0:
        return False
    now = int(time.time()) if now_ts is None else int(now_ts)
    if now < paused_at_int + int(cooldown_seconds):
        return False
    clear_maintenance_risk(uid)
    return True


def reconciliation_needs_sync(uid: str) -> bool:
    """纯本地最终一致性检查：是否存在“已 confirmed 但可能未被增量同步覆盖”的转发。

    判定依据（只读本地库，0 远程，不修改 guard / 不参与 / 不 repost）：
    - participation_guard.confirmed 的 updated_at 即 confirmed 写入时刻（confirmed 为终态）。
    - repost_sync_checkpoint.last_synced_at 为最近一次成功增量同步完成时间。
    - 若存在 confirmed 且 confirmed_at >= last_synced_at 的 guard，则秒级先后关系不确定，
      保守地补同步一次；同步使用“开始前 1 秒”作为覆盖水位，后续成功轮次会自然闭包。
    - 一旦后续成功同步令 last_synced_at 严格越过 confirmed_at，之后不再判定为漏同步，
      避免“已同步的 confirmed 导致每个维护周期都重扫个人空间”。
    - sync_needed=true 时由主路径直接同步，本检查仅在 sync_needed=false 时兜底。
    """
    scoped_uid = str(uid).strip()
    checkpoint = get_checkpoint(scoped_uid)
    if bool(getattr(checkpoint, "sync_needed", False)):
        return False
    last_synced_at = int(getattr(checkpoint, "last_synced_at", None) or 0)
    confirmed = list_confirmed_guards_needing_history_sync(scoped_uid)
    if not confirmed:
        return False
    return any(int(record.updated_at) >= last_synced_at for record in confirmed)


def auto_maintain(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    """低频自动维护：只做增量同步 + 保守评估，绝不删除。

    0 远程快速退出：风控暂停中 或 sync_needed=false 且无待评估 original。
    明确平台风控（-352/429/限流）→ 持久化 maintenance_risk_paused，需人工恢复。
    普通临时错误 → 仅结束本轮，下一正常维护周期允许再试。
    """
    current = int(now_ts if now_ts is not None else time.time())
    _check_cancel(cancel_check)
    _, uid_int = require_login()  # 只读本地 Cookie，不发网络请求。
    uid = str(uid_int)
    checkpoint = get_checkpoint(uid)
    sync_needed = bool(getattr(checkpoint, "sync_needed", False))
    if bool(getattr(checkpoint, "maintenance_risk_paused", False)):
        # 风控暂停优先级最高：即使 reconciliation 发现漏标 confirmed，也必须 0 远程退出。
        return {
            "uid": uid,
            "synced": False,
            "evaluated_originals": 0,
            "sync_needed": sync_needed,
            "rate_limited": False,
            "message": "自动维护因平台限制已暂停，请稍后手动恢复。",
            "candidates": load_persisted_candidates(uid),
        }
    reposts = list_evaluable_reposts(uid)
    assessments = list_repost_assessments(uid)
    pending = {
        record.original_dynamic_id
        for record in reposts
        if record.original_dynamic_id not in assessments
        or getattr(assessments[record.original_dynamic_id], "assessment_status", "final")
        == "retryable_unknown"
    }
    reconciliation_missing = reconciliation_needs_sync(uid)
    if not sync_needed and not pending and not reconciliation_missing:
        return {
            "uid": uid,
            "synced": False,
            "evaluated_originals": 0,
            "sync_needed": False,
            "rate_limited": False,
            "message": "无新数据，本轮维护 0 远程请求",
            "candidates": load_persisted_candidates(uid),
        }

    breaker = _AssessmentBreaker()
    profile_id = get_runtime_profile_id()
    database_path = str(db_path().resolve())
    synced = False
    guard_reconciliation = {"checked": 0, "resolved": 0, "remaining": 0}
    should_sync = sync_needed or reconciliation_missing
    if should_sync:
        try:
            # 同步阶段完整拥有并关闭自己的 client；评估阶段绝不复用该实例。
            sync_result = sync_repost_history(
                on_progress=on_progress,
                cancel_check=cancel_check,
                client_factory=client_factory,
            )
            raw_reconciliation = sync_result.get("guard_reconciliation")
            if isinstance(raw_reconciliation, Mapping):
                guard_reconciliation = {
                    "checked": int(raw_reconciliation.get("checked") or 0),
                    "resolved": int(raw_reconciliation.get("resolved") or 0),
                    "remaining": int(raw_reconciliation.get("remaining") or 0),
                }
        except (AssessmentRateLimited, RemoteStateUnknown, RuntimeError, ValueError) as exc:
            risk = _risk_code_from_message(exc)
            if risk is not None:
                breaker.rate_limited = True
                breaker.stopped = True
                pause_maintenance_risk(
                    uid,
                    f"自动维护命中平台风控（{risk}）已暂停，需人工恢复",
                )
                breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"
            else:
                breaker.stop_message = (
                    f"自动增量同步失败，保持待同步，等待下一维护周期：{exc}"
                )
                breaker.stopped = True
            return {
                "uid": uid,
                "synced": False,
                "evaluated_originals": 0,
                "sync_needed": bool(getattr(get_checkpoint(uid), "sync_needed", False)),
                "rate_limited": breaker.rate_limited,
                "message": breaker.stop_message,
                "candidates": load_persisted_candidates(uid),
            }
        synced = True
        assessments = list_repost_assessments(uid)
        reposts = list_evaluable_reposts(uid)
        pending = {
            record.original_dynamic_id
            for record in reposts
            if record.original_dynamic_id not in assessments
            or getattr(assessments[record.original_dynamic_id], "assessment_status", "final")
            == "retryable_unknown"
        }

    activities_map = {
        str(item.get("dynamic_id") or ""): item
        for item in load_activities()
        if isinstance(item, dict) and item.get("dynamic_id")
    }
    evaluated = 0
    ordered = sorted(pending)
    if ordered:
        # 评估阶段使用全新的 client。首次 NAV 也纳入明确风控持久化暂停。
        with client_factory() as client:
            try:
                _require_verified_login(client)
            except RuntimeError as exc:
                risk = _risk_code_from_message(exc)
                if risk is None:
                    raise
                breaker.rate_limited = True
                breaker.stopped = True
                pause_maintenance_risk(
                    uid,
                    f"自动维护首次账号校验命中平台风控（{risk}）已暂停，需人工恢复",
                )
                breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"

            for original_id in ordered:
                if evaluated >= AUTO_ASSESSMENT_BUDGET or breaker.stopped:
                    break
                _check_cancel(cancel_check)
                if (
                    get_runtime_profile_id() != profile_id
                    or str(db_path().resolve()) != database_path
                ):
                    breaker.stop_message = "自动维护期间运行 Profile 已变化，已停止本轮维护"
                    breaker.stopped = True
                    break
                try:
                    assessment = _assess_original(
                        client,
                        original_id=original_id,
                        uid=uid,
                        now_ts=current,
                        activities_map=activities_map,
                    )
                except AssessmentRateLimited as exc:
                    breaker.rate_limited = True
                    breaker.stopped = True
                    pause_maintenance_risk(
                        uid,
                        f"自动维护命中平台风控已暂停，需人工恢复：{exc}",
                    )
                    breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"
                    break
                except RemoteStateUnknown as exc:
                    assessment = CandidateAssessment(
                        "blocked", f"原动态状态无法可靠判断：{exc}",
                        "remote_unknown", classification_source="public_classifier",
                    )
                except RuntimeError as exc:
                    risk = _risk_code_from_message(exc)
                    if risk is not None:
                        breaker.rate_limited = True
                        breaker.stopped = True
                        pause_maintenance_risk(
                            uid,
                            f"自动维护命中平台风控（{risk}）已暂停，需人工恢复",
                        )
                        breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"
                        break
                    raise
                _persist_assessment(uid, original_id, assessment)
                evaluated += 1
                breaker.note_success()
                if on_progress is not None:
                    on_progress(
                        evaluated,
                        min(AUTO_ASSESSMENT_BUDGET, max(1, len(ordered))),
                        f"自动维护：评估 {evaluated}/{AUTO_ASSESSMENT_BUDGET}",
                        f"{original_id}：{assessment.level}",
                    )

    refreshed = list_repost_assessments(uid)
    remaining = len(
        {
            record.original_dynamic_id
            for record in list_evaluable_reposts(uid)
            if record.original_dynamic_id not in refreshed
            or getattr(refreshed[record.original_dynamic_id], "assessment_status", "final")
            == "retryable_unknown"
        }
    )
    if breaker.stop_message:
        message = breaker.stop_message
    elif remaining:
        message = f"自动维护完成：增量同步 {'成功' if synced else '无需'}，本轮评估 {evaluated} 条，仍有 {remaining} 条待评估。"
    else:
        message = f"自动维护完成：增量同步 {'成功' if synced else '无需'}，本轮评估 {evaluated} 条。"
    if guard_reconciliation["resolved"]:
        message += (
            f" 已根据本地转发历史自动确认 "
            f"{guard_reconciliation['resolved']} 条历史参与记录。"
        )
    return {
        "uid": uid,
        "synced": synced,
        "evaluated_originals": evaluated,
        "sync_needed": bool(getattr(get_checkpoint(uid), "sync_needed", False)),
        "rate_limited": breaker.rate_limited,
        "message": message,
        "guard_reconciliation": guard_reconciliation,
        "candidates": load_persisted_candidates(uid),
    }


def _effective_candidate_state(
    repost: object,
    assessment: Any,
) -> tuple[str, str, str]:
    if getattr(repost, "identity_ok", None) is not True:
        error = getattr(repost, "identity_error", None) or "身份关系未验证或验证失败"
        return "blocked", error, "identity_unverified"
    level = str(getattr(assessment, "assessment_level", "blocked"))
    if level == "excluded":
        return "excluded", str(getattr(assessment, "reason", "") or "非抽奖"), "non_lottery"
    if level == "blocked":
        return (
            "blocked",
            str(getattr(assessment, "reason", "") or "关键安全信息冲突"),
            str(getattr(assessment, "reason_code", "") or "blocked"),
        )
    return (
        level,
        str(getattr(assessment, "reason", "") or ""),
        str(getattr(assessment, "reason_code", "") or ""),
    )


def _defer_state(repost: object, assessment: Any) -> dict[str, Any] | None:
    """计算 per-repost 暂缓状态：用户暂缓优先，其次系统 30 天暂缓（本地动态计算）。"""
    if getattr(repost, "cleanup_defer_reason", None) == "user":
        return {
            "defer_reason": "user",
            "reason_code": "user_deferred",
            "reason": "用户暂不删除",
            "deferred_at": getattr(repost, "cleanup_deferred_at", None),
            "deferred_until": getattr(repost, "cleanup_deferred_until", None),
        }
    if (
        getattr(assessment, "assessment_level", "") == "manual_review"
        and getattr(assessment, "lottery_time_reliable", False) is True
        and _strict_positive_int(getattr(assessment, "lottery_time", None)) is not None
    ):
        lottery_time = int(getattr(assessment, "lottery_time"))
        deferred_until = lottery_time + MANUAL_REVIEW_DEFER_SECONDS
        if int(time.time()) < deferred_until:
            return {
                "defer_reason": "recent_reliable_lottery",
                "reason_code": "recent_reliable_lottery",
                "reason": "官方可靠开奖时间距今未满 30 天，暂缓人工清理。",
                "deferred_until": deferred_until,
            }
    return None


def load_persisted_candidates(uid: str) -> list[dict[str, Any]]:
    """从本地评估结果重建三级候选；不访问任何远程接口，也不重新扫描。"""
    scoped_uid = str(uid).strip()
    assessments = list_repost_assessments(scoped_uid)
    candidate_originals = {
        original_id
        for original_id, assessment in assessments.items()
        if assessment.assessment_level != "excluded"
    }
    if not candidate_originals:
        return []
    reposts = list_reposts_by_originals(scoped_uid, candidate_originals)
    candidates: list[dict[str, Any]] = []
    for repost in reposts:
        assessment = assessments[repost.original_dynamic_id]
        level, reason, reason_code = _effective_candidate_state(repost, assessment)
        if level == "excluded":
            continue
        defer = _defer_state(repost, assessment)
        if defer is not None:
            level = "deferred"
            reason = defer["reason"]
            reason_code = defer["reason_code"]
        candidates.append(
            {
                "uid": scoped_uid,
                "repost_dynamic_id": repost.repost_dynamic_id,
                "original_dynamic_id": repost.original_dynamic_id,
                "reposted_at": repost.reposted_at,
                "original_author_uid": repost.original_author_uid,
                "original_author_name": repost.original_author_name,
                "level": level,
                "reason_code": reason_code,
                "lottery_type": assessment.lottery_type,
                "lottery_time": assessment.lottery_time,
                "eligible_after": assessment.eligible_after,
                "reason": reason,
                "summary": assessment.summary,
                "classification_source": assessment.classification_source,
                "evaluated_at": assessment.evaluated_at,
                "delete_status": repost.delete_status,
                "delete_requested_at": repost.delete_requested_at,
                "deleted_at": repost.deleted_at,
                "defer_reason": defer.get("defer_reason") if defer else None,
                "deferred_at": defer.get("deferred_at") if defer else None,
                "deferred_until": defer.get("deferred_until") if defer else None,
                "lottery_time_reliable": assessment.lottery_time_reliable,
                "assessment_status": assessment.assessment_status,
                "identity_source": repost.identity_source,
                "identity_checked_at": repost.identity_checked_at,
            }
        )
    return candidates


def load_deleted_candidates(uid: str) -> list[dict[str, Any]]:
    """已删除 tombstone 明细：仅供“已删除”筛选查看，不提供删除操作。"""
    scoped_uid = str(uid).strip()
    return [
        {
            "uid": scoped_uid,
            "repost_dynamic_id": repost.repost_dynamic_id,
            "original_dynamic_id": repost.original_dynamic_id,
            "reposted_at": repost.reposted_at,
            "original_author_uid": repost.original_author_uid,
            "original_author_name": repost.original_author_name,
            "level": "deleted",
            "delete_status": "deleted",
            "deleted_at": repost.deleted_at,
            "delete_requested_at": repost.delete_requested_at,
            "last_error": repost.last_error,
        }
        for repost in list_deleted_reposts(scoped_uid)
    ]


def repost_cleanup_summary(uid: str, *, show_deleted: bool = False) -> dict[str, Any]:
    """供页面零远程恢复的汇总：三级计数 + 待评估 + 候选明细。

    统计口径（只读本地，0 远程）：
    - all_total    : 全部历史，含已删除/未完成删除等所有 delete_status 行。
    - history_total: 现存可处理转发 = active + delete_failed + unknown（候选评估范围）。
    - active / delete_pending / delete_failed / unknown / deleted : 按 delete_status 互斥计数。
    - 其余 safe/manual_review/deferred/blocked/excluded : 按评估等级计数（仅现存可处理范围）。
    """
    scoped_uid = str(uid).strip()
    reposts = list_evaluable_reposts(scoped_uid)
    assessments = list_repost_assessments(scoped_uid)
    candidates = load_persisted_candidates(scoped_uid)
    deleted_candidates = load_deleted_candidates(scoped_uid) if show_deleted else []
    checkpoint = get_checkpoint(scoped_uid)
    status_counts = count_repost_delete_statuses(scoped_uid)
    counts = {"safe": 0, "manual_review": 0, "blocked": 0, "deferred": 0}
    for candidate in candidates:
        level = candidate.get("level")
        if level in counts:
            counts[level] += 1
    pending_originals = {
        record.original_dynamic_id
        for record in reposts
        if record.original_dynamic_id not in assessments
        or getattr(
            assessments[record.original_dynamic_id], "assessment_status", "final"
        )
        == "retryable_unknown"
    }
    return {
        "uid": scoped_uid,
        "all_total": status_counts["total"],
        "active": status_counts["active"],
        "delete_pending": status_counts["delete_pending"],
        "delete_failed": status_counts["delete_failed"],
        "unknown": status_counts["unknown"],
        "history_total": len(reposts),
        "assessed_total": len(assessments),
        "safe": counts["safe"],
        "manual_review": counts["manual_review"],
        "deferred": counts["deferred"],
        "blocked": counts["blocked"],
        "excluded": sum(
            1 for assessment in assessments.values()
            if assessment.assessment_level == "excluded"
        ),
        "pending_evaluation": len(pending_originals),
        "deleted": status_counts["deleted"],
        "last_synced_at": getattr(checkpoint, "last_synced_at", None),
        "full_scan_completed": bool(getattr(checkpoint, "full_scan_completed", False)),
        "last_evaluated_at": max(
            (
                int(assessment.evaluated_at or 0)
                for assessment in assessments.values()
            ),
            default=None,
        ),
        "deleted_candidates": deleted_candidates,
        "candidates": candidates,
    }


def _health_dt(value: object) -> str:
    """本地显示时间（固定 UTC+8，与业务时间一致）；空值返回「—」。"""
    try:
        ts = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    if ts <= 0:
        return "—"
    try:
        from datetime import datetime, timedelta, timezone

        dt = datetime.fromtimestamp(ts, tz=timezone(timedelta(hours=8)))
        return dt.strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ts)


def cleanup_health_check(
    uid: str,
    *,
    delete_job_in_progress: bool = False,
) -> dict[str, Any]:
    """当前 runtime Profile 清理维护的纯本地健康检查。

    约束：0 Bilibili 请求 / 0 LLM / 0 DELETE；只读本地库与本地文件。
    只负责「让用户看见问题」：绝不自动恢复 delete_pending、绝不自动重试
    unknown/delete_failed、绝不自动重新删除、绝不自动解除风控暂停。
    """
    scoped_uid = str(uid).strip()
    summary = repost_cleanup_summary(scoped_uid)
    status_counts = count_repost_delete_statuses(scoped_uid)
    checkpoint = get_checkpoint(scoped_uid)
    sync_needed = bool(getattr(checkpoint, "sync_needed", False))
    sync_needed_at = getattr(checkpoint, "sync_needed_at", None)
    full_scan_completed = bool(getattr(checkpoint, "full_scan_completed", False))
    last_synced_at = getattr(checkpoint, "last_synced_at", None)
    reconciliation_missing = reconciliation_needs_sync(scoped_uid)
    pending_evaluation = int(summary.get("pending_evaluation") or 0)
    risk = maintenance_risk_state(scoped_uid)
    risk_paused = bool(risk.get("risk_paused"))

    issue_counts = {
        "delete_pending": int(status_counts["delete_pending"]),
        "delete_failed": int(status_counts["delete_failed"]),
        "unknown": int(status_counts["unknown"]),
    }
    issue_total = sum(issue_counts.values())

    issue_rows = [
        {
            "repost_dynamic_id": record.repost_dynamic_id,
            "original_dynamic_id": record.original_dynamic_id,
            "delete_status": record.delete_status,
            "last_error": record.last_error,
            "delete_requested_at": record.delete_requested_at,
            "updated_at": record.updated_at,
        }
        for record in list_repost_issues(scoped_uid, limit=HEALTH_ISSUE_LIMIT)
    ]

    checks: list[dict[str, str]] = []
    runtime_error = False

    profile_id = get_runtime_profile_id()
    selected_id = get_selected_profile_id()
    if selected_id and selected_id != profile_id:
        checks.append(
            {
                "key": "profile_switch_pending",
                "tone": "info",
                "text": (
                    f"已选择切换 Profile「{selected_id}」，将在重启后生效；"
                    f"当前进程仍运行「{profile_id}」，不会访问新账号。"
                ),
            }
        )
    try:
        from src.profile_manager import get_profile_metadata

        metadata_mid = str(get_profile_metadata(profile_id).get("mid") or "").strip()
    except Exception:
        metadata_mid = ""
    if metadata_mid and metadata_mid != scoped_uid:
        runtime_error = True
        checks.append(
            {
                "key": "runtime_identity_mismatch",
                "tone": "error",
                "text": (
                    f"运行状态异常：Profile「{profile_id}」元数据账号 {metadata_mid} "
                    f"与当前登录 Cookie 账号 {scoped_uid} 不一致。程序不会猜测账号，"
                    f"请重启 Binggo 并切换到正确的 Profile 后使用。"
                ),
            }
        )

    if checkpoint is None:
        checks.append(
            {
                "key": "sync_not_started",
                "tone": "info",
                "text": "尚未开始转发历史同步；可在上方点击「同步我的转发历史」。",
            }
        )
    else:
        if sync_needed:
            checks.append(
                {
                    "key": "sync_needed",
                    "tone": "info",
                    "text": f"有新的转发等待增量同步（标记于 {_health_dt(sync_needed_at)}）。",
                }
            )
        if reconciliation_missing:
            checks.append(
                {
                    "key": "sync_reconciliation",
                    "tone": "info",
                    "text": (
                        "检测到最近参与的转发可能尚未被同步覆盖，"
                        "下一维护周期会保守地补同步一次。"
                    ),
                }
            )
        if not full_scan_completed:
            checks.append(
                {
                    "key": "full_scan_pending",
                    "tone": "info",
                    "text": "历史索引尚未完成首次完整扫描。",
                }
            )
        checks.append(
            {
                "key": "last_synced_at",
                "tone": "ok",
                "text": f"最近成功同步：{_health_dt(last_synced_at)}。"
                if last_synced_at
                else "尚无成功的增量同步。",
            }
        )

    if pending_evaluation:
        checks.append(
            {
                "key": "pending_evaluation",
                "tone": "info",
                "text": (
                    f"还有 {pending_evaluation} 条转发待评估；"
                    f"自动评估每轮最多 {AUTO_ASSESSMENT_BUDGET} 条，人工评估最多 "
                    f"{MAX_ASSESSMENTS_PER_JOB} 条。"
                ),
            }
        )
    else:
        checks.append(
            {
                "key": "pending_evaluation",
                "tone": "ok",
                "text": "所有可评估转发均已完成评估。",
            }
        )

    if risk_paused:
        reason = str(risk.get("risk_reason") or "平台风控暂停")
        paused_at = risk.get("risk_paused_at")
        checks.append(
            {
                "key": "maintenance_risk_paused",
                "tone": "warn",
                "text": (
                    f"自动维护因平台限制已暂停：{reason}"
                    f"（暂停于 {_health_dt(paused_at)}）。"
                    "需要人工在「自动维护」面板点击恢复；恢复后不会立即联网。"
                ),
            }
        )
    if issue_counts["delete_pending"]:
        if delete_job_in_progress:
            tone = "info"
            text = (
                f"有 {issue_counts['delete_pending']} 条未完成删除操作，"
                "删除任务正在运行，请等待结果。"
            )
        else:
            tone = "warn"
            text = (
                f"有 {issue_counts['delete_pending']} 条未完成删除操作"
                "（可能因删除任务中断遗留），需要人工检查；不会自动恢复，"
                "也不会自动重新发送删除请求。"
            )
        checks.append({"key": "delete_pending", "tone": tone, "text": text})
    if issue_counts["unknown"]:
        checks.append(
            {
                "key": "delete_unknown",
                "tone": "warn",
                "text": (
                    f"有 {issue_counts['unknown']} 条删除结果无法确认，"
                    "程序不会自动再次删除；可在下方查看原动态后重新同步核实。"
                ),
            }
        )
    if issue_counts["delete_failed"]:
        checks.append(
            {
                "key": "delete_failed",
                "tone": "warn",
                "text": (
                    f"有 {issue_counts['delete_failed']} 条删除已明确失败，"
                    "不会在后台自动重试；可点击下方记录查看失败原因。"
                ),
            }
        )
    if not risk_paused and not issue_total:
        checks.append(
            {
                "key": "no_issue",
                "tone": "ok",
                "text": "没有需要人工检查的删除记录。",
            }
        )

    if runtime_error:
        status = "runtime_error"
        status_text = "运行状态异常"
    elif risk_paused:
        status = "risk_paused"
        status_text = "维护已暂停（风控）"
    elif issue_total:
        status = "review_needed"
        status_text = "需要人工检查"
    elif sync_needed or reconciliation_missing:
        status = "sync_pending"
        status_text = "有待同步数据"
    else:
        status = "normal"
        status_text = "维护状态正常"

    return {
        "uid": scoped_uid,
        "runtime": {
            "profile_id": profile_id,
            "uid": scoped_uid,
            "database_path": str(db_path().resolve()),
            "selected_profile_id": selected_id or None,
            "consistent": not runtime_error,
        },
        "checkpoint": {
            "exists": checkpoint is not None,
            "full_scan_completed": full_scan_completed,
            "sync_needed": sync_needed,
            "sync_needed_at": sync_needed_at,
            "last_synced_at": last_synced_at,
            "head_dynamic_id": getattr(checkpoint, "head_dynamic_id", None),
        },
        "counts": {
            "all_total": int(status_counts["total"]),
            "active": int(status_counts["active"]),
            "delete_pending": int(status_counts["delete_pending"]),
            "delete_failed": int(status_counts["delete_failed"]),
            "unknown": int(status_counts["unknown"]),
            "deleted": int(status_counts["deleted"]),
            "history_total": int(summary.get("history_total") or 0),
            "pending_evaluation": pending_evaluation,
        },
        "maintenance": {
            "risk_paused": risk_paused,
            "risk_paused_at": risk.get("risk_paused_at"),
            "risk_reason": risk.get("risk_reason"),
        },
        "issues": issue_rows,
        "issue_total": issue_total,
        "issue_counts": issue_counts,
        "checks": checks,
        "status": status,
        "status_text": status_text,
    }


def defer_candidate(uid: str, repost_id: str) -> None:
    """用户“暂不删除”：只写 per-repost 暂缓标记，0 远程请求。"""
    defer_repost(uid, repost_id, reason="user")


def restore_candidate(uid: str, repost_id: str) -> None:
    """恢复用户暂缓：清除 per-repost 标记，0 远程请求。"""
    restore_repost(uid, repost_id)


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
    manual_review_confirmed: bool = False,
) -> dict[str, Any]:
    """逐条删除人工确认的个人转发；任何不确定结果均不自动重试。

    manual_review 条目必须额外确认；safe 条目保持 V1 严格开奖状态重验；
    blocked 拒绝删除；命中限流/风控立即停止整个剩余批次。
    """
    requested: list[str] = []
    seen: set[str] = set()
    for raw in repost_dynamic_ids:
        repost_id = str(raw or "").strip()
        if repost_id not in seen:
            seen.add(repost_id)
            requested.append(repost_id)
    if len(requested) > MAX_DELETE_BATCH:
        raise ValueError(f"单次最多删除 {MAX_DELETE_BATCH} 条转发动态")

    _check_cancel(cancel_check)
    profile_id = get_runtime_profile_id()
    database_path = str(db_path().resolve())
    items: list[dict[str, str]] = []
    stopped = False
    stop_reason = ""
    with client_factory() as client:
        _, uid_int = _require_verified_login(client)
        uid = str(uid_int)
        activities_map = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        assessments = list_repost_assessments(uid)
        total = len(requested)
        _progress(on_progress, 0, total, f"准备逐条验证并删除 {total} 条转发…")

        def _record_global_risk_if_unified(risk_code: int | None, exc: object) -> None:
            """统一风险出口：明确平台风控写入 global auto remote 6h cooldown。

            只对 src.platform_risk 统一认定的码（-352/-509/429）记录；
            -799 等既有删除安全语义照旧停止批次但不进 global cooldown。
            记录失败只降级为日志，绝不阻断删除流程本身。
            """
            if risk_code not in (-352, -509, 429):
                return
            try:
                from web.auto_remote_state import record_auto_remote_risk

                record_auto_remote_risk(
                    trigger_stage="delete_reposts",
                    reason=str(exc or "") or "",
                    code=str(risk_code),
                )
            except Exception:
                import logging

                logging.getLogger("repost_cleanup").exception(
                    "记录删除任务平台风控冷却失败 code=%s", risk_code
                )

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
                stored = assessments.get(original_id)
                if stored is None:
                    raise ValueError("该原动态尚未完成历史抽奖评估")
                if _defer_state(record, stored) is not None:
                    raise ValueError("该候选已暂缓，请先恢复后再删除")
                level, reason, _ = _effective_candidate_state(record, stored)
                if level == "blocked":
                    raise ValueError(reason)
                if level == "excluded":
                    raise ValueError("非抽奖原动态，禁止删除")
                if level == "manual_review":
                    if not manual_review_confirmed:
                        raise ValueError(
                            "该候选为人工确认项，必须先确认已人工检查原动态且不再需要保留"
                        )
                elif level == "safe":
                    recheck = _assess_original(
                        client,
                        original_id=original_id,
                        uid=uid,
                        now_ts=int(time.time()),
                        activities_map=activities_map,
                    )
                    if recheck.level != "safe":
                        raise ValueError(recheck.reason)
                else:
                    raise ValueError("候选评估等级无效，禁止删除")

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
                    risk = _risk_code_from_message(exc)
                    if risk is not None:
                        stopped = True
                        stop_reason = (
                            f"批量删除因平台限流/风控停止（{risk}），剩余条目保持原状态。"
                        )
                        _record_global_risk_if_unified(risk, exc)
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
                        if code in (-352, -509, -799):
                            stopped = True
                            stop_reason = (
                                f"批量删除因平台限流/风控停止（{code}），剩余条目保持原状态。"
                            )
                            _record_global_risk_if_unified(code, error)
            except AssessmentRateLimited as exc:
                # 明确平台风控（删除前 recheck / 详情校验命中统一风险）：
                # 当前候选绝不进入 DELETE，剩余候选 0 远程，整批立即停止。
                risk = _risk_code_from_message(exc)
                status, message = "skipped", str(exc)
                stopped = True
                stop_reason = (
                    f"批量删除因平台限流/风控停止（{risk if risk is not None else 'risk'}），"
                    f"剩余条目保持原状态。{exc}"
                )
                _record_global_risk_if_unified(risk, exc)
            except (RemoteStateUnknown, RuntimeError, ValueError) as exc:
                # 普通失败保持原单条跳过语义（不可读/404/业务不满足等），继续下一候选。
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
            if stopped:
                break

    return {
        "uid": uid,
        "requested_count": len(requested),
        "deleted_count": sum(item["status"] == "deleted" for item in items),
        "failed_count": sum(item["status"] == "delete_failed" for item in items),
        "unknown_count": sum(item["status"] == "unknown" for item in items),
        "skipped_count": sum(item["status"] == "skipped" for item in items),
        "items": items,
        "rate_limited": stopped,
        "stopped_early": stopped,
        "message": stop_reason or "删除任务处理完成",
    }


__all__ = [
    "AUTO_ASSESSMENT_BUDGET",
    "CandidateAssessment",
    "DELETE_REPOST_URL",
    "EXPIRY_BUFFER_SECONDS",
    "MANUAL_REVIEW_DEFER_SECONDS",
    "MAX_ASSESSMENTS_PER_JOB",
    "MAX_DELETE_BATCH",
    "RemoteStateUnknown",
    "auto_maintain",
    "cleanup_health_check",
    "defer_candidate",
    "delete_reposts",
    "load_persisted_candidates",
    "repost_cleanup_summary",
    "restore_candidate",
    "scan_expired_reposts",
    "sync_repost_history",
]
