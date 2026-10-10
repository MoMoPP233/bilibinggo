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
from src.bilibili_request_budget import (
    RequestBudgetExhausted,
    bilibili_request_budget_scope,
    get_bilibili_request_budget,
)
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
    save_sync_progress,
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
ASSESSMENT_BATCH_SIZE = 120
ASSESSMENT_MICRO_CHUNK_SIZE = 40
MAX_IDENTITY_CHECKS_PER_ROUND = 20
# 兼容别名：120 只表示“每个业务批次大小”，不再是整个 Job 的总上限；
# 40 只用于内部请求节流的参考粒度，不再决定 Job 何时结束。
ASSESSMENT_BUDGET_PER_ROUND = ASSESSMENT_MICRO_CHUNK_SIZE
MAX_ASSESSMENTS_PER_JOB = ASSESSMENT_BATCH_SIZE
AUTO_ASSESSMENT_BUDGET = 20
MANUAL_REVIEW_DEFER_SECONDS = 30 * 24 * 60 * 60
CONSECUTIVE_FAILURE_LIMIT = 3
MAX_DELETE_BATCH = 20
AUTO_DELETE_BATCH_SIZE = 20
HEALTH_ISSUE_LIMIT = 20
TRUSTED_IDENTITY_SOURCES = frozenset({"space_feed", "legacy_space_feed"})
RETRYABLE_ASSESSMENT_REASON_CODES = frozenset({"remote_unknown", "llm_temporary"})
# 基础设施临时失败的最小退避窗口：同一 retryable 记录在该窗口内不重复远程评估
RETRYABLE_ASSESSMENT_BACKOFF_SECONDS = 15 * 60
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


class _SharedClientLease:
    """Borrow a cycle-scoped client without closing the owner at stage boundaries."""

    def __init__(self, client: BilibiliClient) -> None:
        self._client = client

    def __enter__(self) -> BilibiliClient:
        return self._client

    def __exit__(self, *args: object) -> None:
        return None


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
    profile_id = get_runtime_profile_id()
    cached = getattr(client, "_cleanup_verified_login", None)
    if (
        isinstance(cached, tuple)
        and len(cached) == 3
        and cached[0] == csrf
        and str(cached[1]) == str(cookie_uid)
        and cached[2] == profile_id
    ):
        return cached[0], int(cached[1])
    try:
        payload = client.request_json(NAV_URL, retries=0)
    except RequestBudgetExhausted:
        # V22 本地预算保护：不伪装成登录/网络失败。
        raise
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
    setattr(client, "_cleanup_verified_login", (csrf, cookie_uid, profile_id))
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
    enforce_request_budget: bool = True,
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
    uid = str(local_uid)
    checkpoint = initial_checkpoint
    old_head = str(getattr(checkpoint, "head_dynamic_id", "") or "")
    old_head_ts = _strict_positive_int(getattr(checkpoint, "head_published_at", None))
    offset = str(getattr(checkpoint, "resume_offset", "") or "")
    resumed_scan = bool(offset)
    scan_started_at = _strict_positive_int(getattr(checkpoint, "resume_started_at", None)) or sync_started_at
    seen_offsets: set[str] = set()
    scanned_pages = 0
    scanned_items = 0
    found_reposts = 0
    imported_count = 0
    completed = False
    budget_exhausted = False
    newest_id: str | None = getattr(checkpoint, "resume_head_dynamic_id", None)
    newest_ts: int | None = _strict_positive_int(getattr(checkpoint, "resume_head_published_at", None))
    saved_checkpoint = checkpoint

    with bilibili_request_budget_scope(enforce_limit=enforce_request_budget) as budget_scope:
        with client_factory() as client:
            try:
                _, uid = _require_verified_login(client)
                checkpoint = get_checkpoint(str(uid))
                offset = str(getattr(checkpoint, "resume_offset", "") or "")
                resumed_scan = bool(offset)
                scan_started_at = _strict_positive_int(getattr(checkpoint, "resume_started_at", None)) or sync_started_at
                newest_id = getattr(checkpoint, "resume_head_dynamic_id", None)
                newest_ts = _strict_positive_int(getattr(checkpoint, "resume_head_published_at", None))
                old_head = str(getattr(checkpoint, "head_dynamic_id", "") or "")
                old_head_ts = _strict_positive_int(
                    getattr(checkpoint, "head_published_at", None)
                )
                saved_checkpoint = checkpoint

                _progress(on_progress, 0, 0, "正在读取当前账号的转发历史…")
                while True:
                    _check_cancel(cancel_check)
                    if offset in seen_offsets:
                        raise RuntimeError("空间动态分页游标重复，未推进同步检查点")
                    seen_offsets.add(offset)
                    try:
                        data = fetch_space_feed_page(
                            client, mid=uid, offset=offset, retries=0
                        )
                    except RequestBudgetExhausted:
                        # V22：预算耗尽 → 安全停止分页；不保存检查点、不清 dirty。
                        budget_exhausted = True
                        break
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
                        item_ts = (
                            extract_feed_pub_ts(item) if isinstance(item, dict) else None
                        )
                        page_timestamps.append(item_ts)
                        if item_id and item_ts and (newest_ts is None or item_ts > newest_ts):
                            newest_id, newest_ts = item_id, item_ts
                        if old_head and item_id == old_head:
                            reached_checkpoint = True

                        owned = (
                            extract_owned_repost(item, uid=uid)
                            if isinstance(item, dict)
                            else None
                        )
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
                        and all(
                            timestamp < old_head_ts
                            for timestamp in page_timestamps
                            if timestamp
                        )
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
                    # 本页 history 已提交后才前移游标。崩溃夹在两次提交之间最多重读
                    # 一页，不会跳过尚未落库的转发记录。
                    save_sync_progress(
                        str(uid),
                        next_offset=next_offset,
                        newest_id=newest_id,
                        newest_ts=newest_ts,
                        started_at=scan_started_at,
                    )
                    offset = next_offset
                    if PAGE_REQUEST_DELAY > 0:
                        time.sleep(PAGE_REQUEST_DELAY)

                _check_cancel(cancel_check)
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
                        last_synced_at=max(1, scan_started_at - 1),
                    )
            except RequestBudgetExhausted:
                # 登录校验/分页等任何真实请求耗尽预算：安全暂停，不推进检查点。
                budget_exhausted = True

        budget_fields = budget_scope.result_fields(
            exhausted=budget_exhausted, partial=budget_exhausted or not completed
        )
        if budget_exhausted:
            _log_budget_paused(budget_scope, f"sync uid={uid}")

    guard_reconciliation = {"checked": 0, "resolved": 0, "remaining": 0}
    if completed:
        # history 已完整落库后才允许做纯本地、单向的 uncertain → confirmed。
        # 不调用 confirm_repost，避免把已经在 history 中的旧转发重新标成待同步。
        guard_reconciliation = reconcile_uncertain_guards_from_history(str(uid))

    dirty_cleared = False
    if (
        completed
        and initial_sync_needed
        and (
            not resumed_scan
            or consumed_sync_needed_at is None
            or int(consumed_sync_needed_at) < scan_started_at
        )
    ):
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
        "sync_complete": completed,
        "full_scan_completed": bool(
            getattr(saved_checkpoint, "full_scan_completed", completed)
        ),
        "sync_started_at": sync_started_at,
        "consumed_sync_needed_at": consumed_sync_needed_at,
        "sync_needed_cleared": dirty_cleared,
        "guard_reconciliation": guard_reconciliation,
        "checkpoint": checkpoint_payload,
        **budget_fields,
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
    except RequestBudgetExhausted:
        raise
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
    except RequestBudgetExhausted:
        raise
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


def _record_global_platform_risk(stage: str, exc: object) -> None:
    """正式平台风控（-352/-509/429）→ 写入 per-Profile 全局自动远程冷却（单一来源）。

    -799/普通失败不写；写失败不影响当前业务结果（只记日志）。
    """
    code = _risk_code_from_message(exc)
    if code not in (-352, -509, 429):
        return
    try:
        from web.auto_remote_state import record_auto_remote_risk

        record_auto_remote_risk(trigger_stage=stage, reason=str(exc or ""), code=str(code))
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger("repost_cleanup").exception("记录全局平台风控冷却失败 code=%s", code)


def _format_budget_resume(expires_at: int) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(int(expires_at)).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return "--:--"


def _log_budget_paused(scope: object, stage: str) -> None:
    """同一个任务预算耗尽只记录一次主 warning（不逐 candidate 刷屏）。"""
    import logging

    snapshot = scope.snapshot()  # type: ignore[attr-defined]
    logging.getLogger("repost_cleanup").warning(
        "本轮 Bilibili 安全请求预算已用完：已使用 %s / %s 次（%s）；"
        "已安全暂停连续评估；额度恢复后请手动再次执行，预计恢复时间：%s",
        snapshot.used_requests,
        snapshot.budget_limit,
        stage,
        _format_budget_resume(snapshot.window_expires_at),
    )


def _log_budget_cooling(scope: object, stage: str) -> None:
    import logging

    snapshot = scope.snapshot()  # type: ignore[attr-defined]
    minutes = max(1, (snapshot.remaining_seconds + 59) // 60)
    logging.getLogger("repost_cleanup").info(
        "Bilibili 请求安全窗口仍在冷却，本轮不发送新的评估请求（%s）；剩余约 %s 分钟",
        stage,
        minutes,
    )


def _log_budget_recovered(scope: object, stage: str) -> None:
    import logging

    if bool(getattr(scope, "rolled", False)):
        logging.getLogger("repost_cleanup").info(
            "Bilibili 请求安全预算已恢复，可手动继续处理未完成评估（%s）", stage
        )


def _budget_paused_message(snapshot: object) -> str:
    return (
        f"本轮 Bilibili 安全请求预算已用完：已使用 "
        f"{getattr(snapshot, 'used_requests', 0)} / {getattr(snapshot, 'budget_limit', 0)} 次；"
        "已安全暂停连续评估；额度恢复后请手动再次执行，"
        f"预计恢复时间：{_format_budget_resume(getattr(snapshot, 'window_expires_at', 0))}"
    )


def _budget_cooling_message(snapshot: object) -> str:
    remaining = int(getattr(snapshot, "remaining_seconds", 0) or 0)
    minutes = max(1, (remaining + 59) // 60)
    return (
        f"Bilibili 请求安全窗口仍在冷却，本轮不发送新的评估请求；剩余约 {minutes} 分钟"
    )


_BUDGET_RESULT_KEYS = (
    "budget_exhausted",
    "partial",
    "budget_limit",
    "used_requests",
    "remaining_requests",
    "window_started_at",
    "window_expires_at",
    "resume_after",
    "remaining_seconds",
    "request_attempts",
    "request_categories",
    "cache_hits",
)


def _copy_budget_fields(source: Mapping[str, Any]) -> dict[str, Any]:
    return {key: source[key] for key in _BUDGET_RESULT_KEYS if key in source}


def _budget_result_fields(scope: object | None, *, exhausted: bool) -> dict[str, Any]:
    if scope is not None:
        return scope.result_fields(exhausted=exhausted, partial=exhausted)  # type: ignore[attr-defined]
    snap = get_bilibili_request_budget()
    return {
        "budget_exhausted": False,
        "partial": False,
        **snap.to_dict(),
        "request_attempts": 0,
        "request_categories": {},
        "cache_hits": 0,
    }


class _AssessmentBreaker:
    """串行评估/删除的风控熔断器：-352/429 立即停止，连续异常达阈值也停止。"""

    def __init__(self, *, consecutive_limit: int = CONSECUTIVE_FAILURE_LIMIT) -> None:
        self.consecutive_limit = max(1, int(consecutive_limit))
        self.consecutive_failures = 0
        self.rate_limited = False
        self.stopped = False
        self.stop_message = ""
        self.risk_code: int | None = None

    def note_success(self) -> None:
        self.consecutive_failures = 0

    def note_failure(self, exc_or_code: object) -> None:
        risk = _risk_code_from_message(exc_or_code)
        if risk is None and isinstance(exc_or_code, int):
            risk = exc_or_code if isinstance(exc_or_code, int) and exc_or_code in (-352, -509, 429) else None
        if risk is not None:
            self.rate_limited = True
            self.risk_code = risk
            self.stopped = True
            # V24：保留结构化信封，保证任何调用方/上层 helper 都能重新识别正式风控，
            # 不会因为只有整数码而把 -352/-509/429 降级成普通文本错误。
            self.stop_message = (
                f"API error {risk}: 本轮操作因平台限流/风控提前停止（{risk}），"
                "已经完成的结果已保存。"
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
    except RequestBudgetExhausted:
        # V22 本地预算保护：不得保存成 classify_failed / 业务 final。
        raise
    except Exception as exc:
        risk = _risk_code_from_message(exc)
        if risk is not None:
            raise AssessmentRateLimited(
                f"本轮评估因平台限流/风控提前停止（{risk}），已经完成的结果已保存。"
            ) from exc
        # LLM 基础设施临时/协议失败：属于 retryable，绝不保存成业务 final。
        from src.llm_client import LlmConfigError, LlmProtocolError, LlmTemporaryError

        if isinstance(exc, (LlmTemporaryError, LlmProtocolError, LlmConfigError)):
            return CandidateAssessment(
                "blocked",
                f"LLM 服务临时不可用（{type(exc).__name__}），稍后自动重试",
                "llm_temporary",
                classification_source="public_classifier",
            )
        return CandidateAssessment(
            "blocked", f"公共分类无法可靠判断原动态是否为抽奖：{exc}",
            "classify_failed", classification_source="public_classifier",
        )
    if risk_code is not None:
        if risk_code in (-352, -509, 429):
            # 正式结构化平台风控：停止当前评估并上抛（durable pause 由调用方处理）
            raise AssessmentRateLimited(
                f"API error {risk_code}: 分类接口命中平台风控，已停止（已完成结果已保留）"
            ) from None
        # -799 等非正式码：只按本地受限处理（不参与平台风控/全局冷却/风险暂停），
        # 标记 blocked 候选，绝不进入 DELETE。
        return CandidateAssessment(
            "blocked", f"原动态状态获取受限（{risk_code}），本轮按不可参与处理；非正式平台风控，不写全局冷却",
            "temporary_unavailable", classification_source="public_classifier",
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
    *,
    now_ts: int | None = None,
) -> None:
    current = int(now_ts if now_ts is not None else time.time())
    eligible_after = assessment.eligible_after
    if assessment.level == "manual_review":
        # 人工确认只代表本次判断，不把候选永久锁死；沿用现有 eligible_after
        # 字段作为下一次人工评估资格时间，不引入第二套调度状态。
        if _strict_positive_int(eligible_after) is None:
            lottery_time = _strict_positive_int(assessment.lottery_time)
            if assessment.lottery_time_reliable and lottery_time is not None:
                eligible_after = lottery_time + MANUAL_REVIEW_DEFER_SECONDS
            else:
                eligible_after = current + MANUAL_REVIEW_DEFER_SECONDS
    upsert_repost_assessment(
        uid,
        original_id,
        assessment_level=assessment.level,
        assessment_status=_assessment_status_for(assessment),
        lottery_time_reliable=assessment.lottery_time_reliable,
        reason_code=assessment.reason_code or None,
        lottery_type=assessment.lottery_type or None,
        lottery_time=assessment.lottery_time,
        eligible_after=eligible_after,
        reason=assessment.reason,
        classification_source=assessment.classification_source,
        summary=assessment.summary or None,
        evaluated_at=current,
        remote_checked_at=assessment.remote_checked_at,
    )


def _assessment_status_for(assessment: CandidateAssessment) -> str:
    """人工确认项按既有 eligible_after 重新具备评估资格，不永久 final。"""
    if assessment.level == "manual_review":
        return "refreshable"
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


def _manual_review_recheck_at(assessment: object, *, fallback_now: int) -> int:
    """返回人工确认项下一次可重新评估的时间，兼容 V77 旧 final 记录。"""
    row = _record_dict(assessment)
    eligible_after = _strict_positive_int(row.get("eligible_after"))
    if eligible_after is not None:
        return eligible_after
    lottery_time = _strict_positive_int(row.get("lottery_time"))
    if row.get("lottery_time_reliable") is True and lottery_time is not None:
        return lottery_time + MANUAL_REVIEW_DEFER_SECONDS
    evaluated_at = _strict_positive_int(row.get("evaluated_at"))
    if evaluated_at is not None:
        return evaluated_at + MANUAL_REVIEW_DEFER_SECONDS
    return fallback_now + MANUAL_REVIEW_DEFER_SECONDS


def _assessment_ready_for_manual_review(assessment: object, *, now_ts: int) -> bool:
    row = _record_dict(assessment)
    status = str(row.get("assessment_status") or "final")
    if status != "refreshable":
        return False
    return _manual_review_recheck_at(row, fallback_now=now_ts) <= now_ts


def _normalize_manual_review_lifecycle(
    uid: str,
    assessments: Mapping[str, Any],
    *,
    now_ts: int,
) -> dict[str, Any]:
    """将 V77 旧 manual_review/final 记录接入 refreshable 生命周期。"""
    changed = False
    for original_id, assessment in assessments.items():
        row = _record_dict(assessment)
        if row.get("assessment_level") != "manual_review":
            continue
        if str(row.get("assessment_status") or "final") != "final":
            continue
        eligible_after = _manual_review_recheck_at(row, fallback_now=now_ts)
        upsert_repost_assessment(
            uid,
            original_id,
            assessment_level="manual_review",
            assessment_status="refreshable",
            lottery_time_reliable=row.get("lottery_time_reliable") is True,
            reason_code=row.get("reason_code"),
            lottery_type=row.get("lottery_type"),
            lottery_time=row.get("lottery_time"),
            eligible_after=eligible_after,
            reason=row.get("reason"),
            classification_source=row.get("classification_source") or "activities",
            summary=row.get("summary"),
            evaluated_at=row.get("evaluated_at"),
            remote_checked_at=row.get("remote_checked_at"),
            assessed_at=row.get("assessed_at") or now_ts,
        )
        changed = True
    return list_repost_assessments(uid) if changed else dict(assessments)


def scan_expired_reposts(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
    force_original_ids: object = None,
    stale_check: Callable[[], object] | None = None,
    enforce_request_budget: bool = True,
) -> dict[str, Any]:
    """一键智能评估（M4：连续批次）。

    - Job 启动时冻结 scope_ids（当时全部可评估 original_dynamic_id）。
    - 业务批次大小 ASSESSMENT_BATCH_SIZE=120：一批最多 120 条；一批结束后重新查库，
      继续下一批，直到本次 frozen backlog 全部完成；全程同一个 Job / 同一次调用。
    - attempted_this_job 在进入网络/LLM 评估前登记：同一 original_id 同一 Job 最多评估一次；
      retryable_unknown 只允许未来 Job（满足 backoff 语义后）重新进入。
    - 每批重新查询 assessment 状态；失败来源不伪装空结果。
    - 停止条件：cancel / 平台风控 / Profile·DB·UID 不一致 / processed=0 /
      made_progress=0（避免死循环与无限烧 LLM）。
    - progress_total 固定为 len(scope_ids)；progress_step = 本 Job 已尝试唯一数，单调递增。
    """
    current = int(now_ts if now_ts is not None else time.time())
    _check_cancel(cancel_check)
    forced = _normalize_force_originals(force_original_ids)
    breaker = _AssessmentBreaker()
    budget_exhausted = False
    local_cache_hits = 0

    # 完全没有待处理 original/身份时，不为确认“无事可做”而访问 NAV。
    if not enforce_request_budget and not forced:
        _, local_uid_int = require_login()
        local_uid = str(local_uid_int)
        local_reposts = list_evaluable_reposts(local_uid)
        local_existing = list_repost_assessments(local_uid)
        local_existing = _normalize_manual_review_lifecycle(
            local_uid, local_existing, now_ts=current
        )
    else:
        local_reposts = []
        local_existing = {}
    if not enforce_request_budget and not forced and all(
        (_identity_trusted(record) or not _identity_retryable(record))
        and record.original_dynamic_id in local_existing
        and not (
            getattr(local_existing[record.original_dynamic_id], "assessment_status", "final")
            == "retryable_unknown"
            or _assessment_ready_for_manual_review(
                local_existing[record.original_dynamic_id], now_ts=current
            )
        )
        for record in local_reposts
    ):
        return {
            "uid": local_uid,
            "history_total": len(local_reposts),
            "evaluated_originals": 0,
            "pending_originals": 0,
            "budget": ASSESSMENT_BUDGET_PER_ROUND,
            "job_budget": MAX_ASSESSMENTS_PER_JOB,
            "batch_size": ASSESSMENT_BATCH_SIZE,
            "frozen_scope": 0,
            "batches_done": 0,
            "attempted_this_job": 0,
            "made_progress": 0,
            "rate_limited": False,
            "risk_code": None,
            "stopped_early": False,
            "stop_reason": "",
            "message": "智能评估完成，共处理 0 条原动态（0 批）。",
            "candidates": load_persisted_candidates(local_uid),
            "budget_exhausted": False,
            "partial": False,
            "request_attempts": 0,
            "request_categories": {},
            "cache_hits": 0,
        }

    def _semantic(record: object) -> tuple | None:
        if record is None:
            return None
        row = _record_dict(record)
        return (
            row.get("assessment_status") or None,
            row.get("assessment_level") or None,
            row.get("reason_code") or None,
            row.get("lottery_type") or None,
            row.get("lottery_time"),
            row.get("eligible_after"),
            row.get("classification_source") or None,
        )

    def _after_semantic(assessment: CandidateAssessment) -> tuple:
        return (
            _assessment_status_for(assessment),
            assessment.level,
            assessment.reason_code or None,
            assessment.lottery_type or None,
            assessment.lottery_time,
            assessment.eligible_after,
            assessment.classification_source or None,
        )

    def _check_runtime(frozen_uid: str, frozen_profile: str, frozen_db: str) -> None:
        if get_runtime_profile_id() != frozen_profile:
            raise RuntimeError("运行期间 Profile 发生变化，已停止")
        if str(db_path()) != frozen_db:
            raise RuntimeError("运行期间数据库路径发生变化，已停止")
        # UID 已在本 Job 开始由 _require_verified_login 冻结；Profile/DB 不变即身份不变。

    with bilibili_request_budget_scope(enforce_limit=enforce_request_budget) as budget_scope, client_factory() as client:
        try:
            _, uid_int = _require_verified_login(client)
        except RequestBudgetExhausted as exc:
            # 窗口未恢复：本轮 0 远程请求，返回结构化 paused 结果（不是风控/崩溃）。
            _log_budget_cooling(budget_scope, "scan verify")
            _, local_uid_int = require_login()
            early_uid = str(local_uid_int)
            early_reposts = list_evaluable_reposts(early_uid)
            early_existing = list_repost_assessments(early_uid)
            early_pending = {
                record.original_dynamic_id
                for record in early_reposts
                if record.original_dynamic_id not in early_existing
                or getattr(
                    early_existing[record.original_dynamic_id],
                    "assessment_status",
                    "final",
                )
                == "retryable_unknown"
            }
            return {
                "uid": early_uid,
                "history_total": len(early_reposts),
                "evaluated_originals": 0,
                "pending_originals": len(early_pending),
                "budget": ASSESSMENT_BUDGET_PER_ROUND,
                "job_budget": MAX_ASSESSMENTS_PER_JOB,
                "batch_size": ASSESSMENT_BATCH_SIZE,
                "frozen_scope": len(early_pending),
                "batches_done": 0,
                "attempted_this_job": 0,
                "made_progress": 0,
                "rate_limited": False,
                "risk_code": None,
                "stopped_early": True,
                "stop_reason": "REQUEST_BUDGET_EXHAUSTED",
                "message": _budget_cooling_message(exc.snapshot),
                "candidates": load_persisted_candidates(early_uid),
                **budget_scope.result_fields(exhausted=True, partial=True),
            }
        uid = str(uid_int)
        frozen_profile = get_runtime_profile_id()
        frozen_db = str(db_path())
        reposts = list_evaluable_reposts(uid)
        activities_map = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        existing = list_repost_assessments(uid)
        existing = _normalize_manual_review_lifecycle(uid, existing, now_ts=current)
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
            except RequestBudgetExhausted:
                # V22：预算耗尽不得把身份写成失败；安全停止，剩余下窗口继续。
                budget_exhausted = True
                break
            except (RemoteStateUnknown, RuntimeError, ValueError) as exc:
                if _risk_code_from_message(exc) is not None:
                    # 正式风控不是身份反证；不污染身份账本，也不请求后续条目。
                    _record_global_platform_risk("scan_expired_reposts", exc)
                    try:
                        breaker.note_failure(exc)
                    except AssessmentRateLimited:
                        break
                set_repost_identity(
                    uid, repost_id, ok=False, source="remote_detail",
                    error=str(exc), checked_at=current,
                )
                try:
                    breaker.note_failure(exc)
                except AssessmentRateLimited:
                    break

        # 冻结本 Job scope：运行中新产生的记录不进入当前 Job。
        scope_ids = {record.original_dynamic_id for record in reposts}
        scope_ids.update(forced)
        scope_ids = sorted(
            scope_ids,
            key=lambda original_id: (
                0 if original_id in activities_map else 1,
                original_id,
            ),
        )
        scope_total = len(scope_ids)
        max_batches = max(1, (scope_total + ASSESSMENT_BATCH_SIZE - 1) // ASSESSMENT_BATCH_SIZE) if scope_total else 0
        attempted_this_job: set[str] = set()
        evaluated = 0
        made_progress_total = 0
        batches_done = 0
        processed_total = 0
        stop_reason = ""
        _progress(
            on_progress,
            0,
            max(1, scope_total),
            f"待评估原动态 {scope_total} 条（已冻结，自动连续分批处理）",
        )

        for batch_no in range(max_batches):
            _check_cancel(cancel_check)
            if breaker.stopped or budget_exhausted:
                break
            if stale_check is not None and stale_check() is True:
                raise RuntimeError("任务已过期（调度换代），已停止")
            _check_runtime(uid, frozen_profile, frozen_db)

            # 每批开始：重新查库（assessment 状态以最新为准）
            existing = list_repost_assessments(uid)

            def _retryable_ready(original_id: str) -> bool:
                data = _record_dict(existing[original_id])
                if data.get("assessment_status") != "retryable_unknown":
                    return False
                evaluated_at = int(data.get("evaluated_at") or 0)
                # 时间戳缺失/时钟偏差（>= 当前固定时钟）时不阻塞恢复，只对明确
                # “最近失败且早于当前”的记录应用最小退避窗口。
                if evaluated_at >= current:
                    return True
                return (current - evaluated_at) >= RETRYABLE_ASSESSMENT_BACKOFF_SECONDS

            pending = [
                original_id
                for original_id in scope_ids
                if original_id not in attempted_this_job
                and (
                    original_id not in existing
                    or _retryable_ready(original_id)
                    or (
                        original_id in existing
                        and _assessment_ready_for_manual_review(
                            existing[original_id], now_ts=current
                        )
                    )
                )
            ]
            if not pending:
                break  # 本次 scope 已完成
            batch = pending[:ASSESSMENT_BATCH_SIZE]
            processed_batch = 0
            made_batch = 0
            for original_id in batch:
                if breaker.stopped:
                    break
                _check_cancel(cancel_check)
                # 关键：任何网络/LLM/_assess_original 之前登记，防止同 Job 重复评估。
                attempted_this_job.add(original_id)
                before = _semantic(existing.get(original_id))
                attempts_before = budget_scope.total_attempts
                try:
                    assessment = _assess_original(
                        client,
                        original_id=original_id,
                        uid=uid,
                        now_ts=current,
                        activities_map=activities_map,
                    )
                except RequestBudgetExhausted:
                    # V22：单条处理一半（如 detail 已成功、notice 前耗尽）→ 不写 final，
                    # 保持 pending，下一窗口继续；已完成的其它记录照常保存。
                    budget_exhausted = True
                    stop_reason = "REQUEST_BUDGET_EXHAUSTED"
                    break
                except AssessmentRateLimited as exc:
                    breaker.rate_limited = True
                    breaker.stopped = True
                    breaker.risk_code = _risk_code_from_message(exc)
                    breaker.stop_message = str(exc)
                    _record_global_platform_risk("scan_expired_reposts", exc)
                    break
                except RemoteStateUnknown as exc:
                    assessment = CandidateAssessment(
                        "blocked", f"原动态状态无法可靠判断：{exc}",
                        "remote_unknown", classification_source="public_classifier",
                    )
                except Exception as exc:  # noqa: BLE001 - 识别结构化风控，其余异常向上传播
                    risk = _risk_code_from_message(exc)
                    if risk is None:
                        raise
                    breaker.rate_limited = True
                    breaker.stopped = True
                    breaker.risk_code = risk
                    _record_global_platform_risk("scan_expired_reposts", exc)
                    breaker.stop_message = (
                        f"本任务命中平台风控（{risk}），"
                        "已停止后续评估；已经完成的结果已保存。"
                    )
                    break
                _persist_assessment(uid, original_id, assessment, now_ts=current)
                if budget_scope.total_attempts == attempts_before:
                    # 本地 activities 复用/无远程请求：单独计数 cache hit（0 budget）。
                    local_cache_hits += 1
                    budget_scope.note_cache_hit()
                processed_batch += 1
                processed_total += 1
                evaluated += 1
                breaker.note_success()
                after = _after_semantic(assessment)
                if after != before:
                    made_batch += 1
                    made_progress_total += 1
                phase = "本地评估" if original_id in activities_map else "远程分类"
                _progress(
                    on_progress,
                    len(attempted_this_job),
                    max(1, scope_total),
                    f"第{batch_no + 1}批：{processed_batch}/{len(batch)} 累计{len(attempted_this_job)}/{scope_total}（{phase}）",
                    f"{original_id}：{assessment.level}",
                )
            batches_done += 1
            if breaker.stopped:
                break
            if budget_exhausted:
                stop_reason = "REQUEST_BUDGET_EXHAUSTED"
                break
            if processed_batch == 0:
                stop_reason = "NO_PROCESSED_PROGRESS"
                break
            if made_batch == 0:
                # 仍有 pending 但状态无任何语义进展：停止，避免同 Job 无限重试。
                stop_reason = "NO_STATE_PROGRESS"
                break

    refreshed = list_repost_assessments(uid)
    remaining = sum(
        1
        for original_id in scope_ids
        if original_id not in refreshed
        or _record_dict(refreshed[original_id]).get("assessment_status") == "retryable_unknown"
    )
    if budget_exhausted:
        _log_budget_paused(budget_scope, "scan")
        _log_budget_recovered(budget_scope, "scan")
        message = _budget_paused_message(budget_scope.snapshot())
    elif breaker.stop_message:
        message = breaker.stop_message
    elif stop_reason == "NO_PROCESSED_PROGRESS":
        message = "本轮无可处理记录，已停止。"
    elif stop_reason == "NO_STATE_PROGRESS":
        message = (
            f"已完成 {evaluated} 条后无状态进展，为避免重复评估已停止；"
            f"仍有 {remaining} 条待未来评估。"
        )
    elif remaining:
        message = f"本次完成 {evaluated} 条，仍有 {remaining} 条待评估（可在后续任务中继续）。"
    else:
        message = f"智能评估完成，共处理 {evaluated} 条原动态（{batches_done} 批）。"
    return {
        "uid": uid,
        "history_total": len(reposts),
        "evaluated_originals": evaluated,
        "pending_originals": remaining,
        "budget": ASSESSMENT_BUDGET_PER_ROUND,
        "job_budget": MAX_ASSESSMENTS_PER_JOB,
        "batch_size": ASSESSMENT_BATCH_SIZE,
        "frozen_scope": len(scope_ids),
        "batches_done": batches_done,
        "attempted_this_job": len(attempted_this_job),
        "made_progress": made_progress_total,
        "rate_limited": breaker.rate_limited,
        "risk_code": breaker.risk_code,
        "stopped_early": bool(breaker.stopped or budget_exhausted),
        "stop_reason": stop_reason,
        "message": message,
        "candidates": load_persisted_candidates(uid),
        **budget_scope.result_fields(
            exhausted=budget_exhausted,
            partial=budget_exhausted,
        ),
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
    """人工清除清理维护的本地风控暂停；0 远程，不启动任务。"""
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
    """兼容旧调用：冷却到期后仅清除本地暂停（0 远程，不启动任务）。

    只针对 cleanup maintenance 的兼容字段（maintenance_risk_paused /
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
    assess: bool = True,
) -> dict[str, Any]:
    """兼容旧入口：仅由显式清理 Job 执行增量同步与保守评估，绝不删除。

    0 远程快速退出：风控暂停中 或 sync_needed=false 且无待评估 original。
    明确平台风控（-352/429/限流）→ 持久化 maintenance_risk_paused，需人工恢复。
    普通临时错误 → 仅结束本轮，后续须再次由用户启动任务。
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
                _record_global_platform_risk("auto_maintain", exc)
                breaker.risk_code = _risk_code_from_message(exc)
                breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"
            else:
                breaker.stop_message = (
                    f"增量同步失败，保持待同步，请下次手动同步：{exc}"
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
        if sync_result.get("budget_exhausted"):
            # V22：同步阶段已耗尽预算 → 0 assessment，结构化 paused 返回。
            return {
                "uid": uid,
                "synced": False,
                "evaluated_originals": 0,
                "sync_needed": bool(getattr(get_checkpoint(uid), "sync_needed", False)),
                "rate_limited": False,
                "risk_code": None,
                "stopped_early": True,
                "stop_reason": "REQUEST_BUDGET_EXHAUSTED",
                "message": str(
                    sync_result.get("message") or "本轮 Bilibili 安全请求预算已用完"
                ),
                "guard_reconciliation": guard_reconciliation,
                "candidates": load_persisted_candidates(uid),
                **_copy_budget_fields(sync_result),
            }
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
    budget_exhausted = False
    budget_scope: object | None = None
    if ordered and assess:
        # 评估阶段使用全新的 client。首次 NAV 也纳入明确风控持久化暂停。
        with bilibili_request_budget_scope() as budget_scope, client_factory() as client:
            try:
                _require_verified_login(client)
            except RequestBudgetExhausted:
                # V22：窗口未恢复 → 0 远程请求，结构化 paused 返回。
                budget_exhausted = True
                _log_budget_cooling(budget_scope, "auto_maintain verify")
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
                _record_global_platform_risk("auto_maintain", exc)
                breaker.risk_code = _risk_code_from_message(exc)
                breaker.stop_message = "自动维护因平台限制已暂停，请稍后手动恢复。"

            for original_id in ordered:
                if (
                    evaluated >= AUTO_ASSESSMENT_BUDGET
                    or breaker.stopped
                    or budget_exhausted
                ):
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
                except RequestBudgetExhausted:
                    # V22：单条处理一半 → 不写 final，保持 pending，下一窗口继续。
                    budget_exhausted = True
                    break
                except AssessmentRateLimited as exc:
                    breaker.rate_limited = True
                    breaker.stopped = True
                    pause_maintenance_risk(
                        uid,
                        f"自动维护命中平台风控已暂停，需人工恢复：{exc}",
                    )
                    _record_global_platform_risk("auto_maintain", exc)
                    breaker.risk_code = _risk_code_from_message(exc)
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
                        _record_global_platform_risk("auto_maintain", exc)
                        breaker.risk_code = _risk_code_from_message(exc)
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
    if budget_exhausted:
        if budget_scope is not None:
            _log_budget_paused(budget_scope, "auto_maintain")
            _log_budget_recovered(budget_scope, "auto_maintain")
            message = _budget_paused_message(budget_scope.snapshot())  # type: ignore[attr-defined]
        else:
            message = "Bilibili 请求安全窗口仍在冷却，本轮不发送新的评估请求"
    elif breaker.stop_message:
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
        "risk_code": breaker.risk_code,
        "stopped_early": bool(breaker.stopped or budget_exhausted),
        "stop_reason": "REQUEST_BUDGET_EXHAUSTED" if budget_exhausted else "",
        "message": message,
        "guard_reconciliation": guard_reconciliation,
        "candidates": load_persisted_candidates(uid),
        **_budget_result_fields(budget_scope, exhausted=budget_exhausted),
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
    """计算 per-repost 暂缓状态：用户暂缓优先，其次人工评估下一次资格时间。"""
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
        deferred_until = _strict_positive_int(getattr(assessment, "eligible_after", None))
        if deferred_until is None:
            deferred_until = lottery_time + MANUAL_REVIEW_DEFER_SECONDS
        if int(time.time()) < deferred_until:
            return {
                "defer_reason": "recent_reliable_lottery",
                "reason_code": "recent_reliable_lottery",
                "reason": "官方可靠开奖时间距今未满 30 天，暂缓人工清理。",
                "deferred_until": deferred_until,
            }
    if getattr(assessment, "assessment_level", "") == "manual_review":
        status = str(getattr(assessment, "assessment_status", "final") or "final")
        if status == "refreshable":
            deferred_until = _manual_review_recheck_at(assessment, fallback_now=int(time.time()))
            if int(time.time()) < deferred_until:
                return {
                    "defer_reason": "manual_review_recheck",
                    "reason_code": getattr(assessment, "reason_code", None) or "manual_review_recheck",
                    "reason": "本次人工确认已保留，达到下一次检查条件后可重新评估。",
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
        or _assessment_ready_for_manual_review(
            assessments[record.original_dynamic_id], now_ts=int(time.time())
        )
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
                        "下一次手动同步会保守地补同步一次。"
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
                    f"单次智能评估最多 {AUTO_ASSESSMENT_BUDGET} 条，人工评估最多 "
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
                    f"转发清理因平台限制已暂停：{reason}"
                    f"（暂停于 {_health_dt(paused_at)}）。"
                    "请人工恢复后再次手动执行；恢复操作不会立即联网。"
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

    def _result(uid: str, items: list[dict[str, str]], *, reason: str = "", message: str = "") -> dict[str, Any]:
        processed = len(items)
        return {
            "uid": uid,
            "requested_count": len(requested),
            "processed_count": processed,
            "unprocessed_count": len(requested) - processed,
            "deleted_count": sum(item["status"] == "deleted" for item in items),
            "failed_count": sum(item["status"] == "delete_failed" for item in items),
            "unknown_count": sum(item["status"] == "unknown" for item in items),
            "skipped_count": sum(item["status"] == "skipped" for item in items),
            "items": items,
            "rate_limited": reason in {"formal_risk", "platform_limit"},
            "stopped_early": bool(reason),
            "stop_reason": reason,
            "message": message or "删除任务处理完成",
        }

    if cancel_check is not None and cancel_check():
        return _result("", [], reason="cancelled", message="用户取消，剩余条目未处理")
    profile_id = get_runtime_profile_id()
    database_path = str(db_path().resolve())
    items: list[dict[str, str]] = []
    stopped = False
    stop_reason = ""
    stop_kind = ""
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
            if cancel_check is not None and cancel_check():
                stop_kind = "cancelled"
                stop_reason = "用户取消，剩余条目未处理"
                break
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
                if level in {"manual_review", "safe"}:
                    recheck = _assess_original(
                        client,
                        original_id=original_id,
                        uid=uid,
                        now_ts=int(time.time()),
                        activities_map=activities_map,
                    )
                    allowed = {"safe", "manual_review"} if level == "manual_review" else {"safe"}
                    if recheck.level not in allowed:
                        raise ValueError(recheck.reason)
                else:
                    raise ValueError("候选评估等级无效，禁止删除")

                # Rechecks may involve remote reads: validate the frozen
                # identity again at the mutation boundary, not just at entry.
                csrf = _assert_frozen_runtime(
                    profile_id=profile_id, database_path=database_path, uid=uid,
                )
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
                        stop_kind = "formal_risk" if risk in (-352, -509, 429) else "platform_limit"
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
                            stop_kind = "formal_risk" if code in (-352, -509) else "platform_limit"
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
                stop_kind = "formal_risk" if risk in (-352, -509, 429) else "platform_limit"
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

    return _result(uid, items, reason=stop_kind, message=stop_reason)




# ---------------------------------------------------------------------------
# M6: 手动触发的 strict-safe 批量清理（保留 cleanup_auto_delete 契约名）
# ---------------------------------------------------------------------------


def _auto_delete_setting_path():
    from src.data_paths import get_profile_dir

    return get_profile_dir() / "auto_delete_enabled.json"


def get_auto_delete_enabled() -> bool:
    """读取 per-Profile 的历史命名开关；仅许可手动触发的批量清理。"""
    import json
    import os

    path = _auto_delete_setting_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(raw.get("enabled")) if isinstance(raw, dict) else False


def set_auto_delete_enabled(enabled: bool) -> bool:
    """设置手动批量清理许可（原子写，保留旧文件名与 JSON key）。"""
    import json
    import os
    import tempfile

    from src.data_paths import get_atomic_path_lock

    path = _auto_delete_setting_path()
    with get_atomic_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".auto_delete.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"enabled": bool(enabled)}, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    return bool(enabled)


def _strict_safe(record: object, stored: object, *, now_ts: int) -> tuple[bool, str]:
    """正向白名单 strict-safe：任何条件不满足即不可纳入批量删除。"""
    row = _record_dict(record)
    if str(row.get("delete_status") or "") != "active":
        return False, "delete_status 非 active"
    if stored is None:
        return False, "无 final assessment"
    assessment = _record_dict(stored)
    if assessment.get("assessment_status") != "final":
        return False, "assessment 非 final"
    if assessment.get("assessment_level") != "safe":
        return False, "assessment_level 非 safe"
    if assessment.get("lottery_time_reliable") is not True:
        return False, "开奖时间不可靠"
    eligible = assessment.get("eligible_after")
    if eligible is not None:
        try:
            if int(eligible) > int(now_ts):
                return False, "未到安全缓冲时间"
        except (TypeError, ValueError):
            return False, "eligible_after 非法"
    if _defer_state(record, stored) is not None:
        return False, "deferred"
    return True, ""


def _repost_delete_barrier_blocked(uid: str) -> bool:
    """delete_pending / unknown 任一存在 → 本轮禁止任何 DELETE。"""
    counts = count_repost_delete_statuses(uid)
    return (int(counts.get("delete_pending") or 0) + int(counts.get("unknown") or 0)) > 0


def cleanup_has_pending_local_work() -> bool:
    """只读判断清理是否仍有本地待办；绝不创建 client 或发起网络请求。"""
    _, uid_int = require_login()
    uid = str(uid_int)
    checkpoint = get_checkpoint(uid)
    if bool(getattr(checkpoint, "sync_needed", False)) or reconciliation_needs_sync(uid):
        return True

    records = list_evaluable_reposts(uid)
    assessments = list_repost_assessments(uid)
    for record in records:
        original_id = str(getattr(record, "original_dynamic_id", "") or "")
        stored = assessments.get(original_id)
        if stored is None or getattr(stored, "assessment_status", "final") == "retryable_unknown":
            return True

    # DELETE 的 unknown barrier 不是预算续跑可解决的待办；保留原有人工处理语义。
    if not get_auto_delete_enabled() or _repost_delete_barrier_blocked(uid):
        return False
    now_ts = int(time.time())
    for record in records:
        original_id = str(getattr(record, "original_dynamic_id", "") or "")
        ok, _ = _strict_safe(record, assessments.get(original_id), now_ts=now_ts)
        if not ok:
            continue
        guard = get_guard(uid, original_id)
        if guard is None or guard.repost_status not in {"pending", "unknown", "suspected"}:
            return True
    return False


def auto_cleanup_delete(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    """手动触发的 strict-safe 清理：每批 ≤20，当前 Job 连续处理 frozen scope。

    规则（超集为 0 删除）：
    - auto_delete_enabled=False → 0 DELETE；
    - 启动或每批存在 delete_pending/unknown → 0 DELETE（barrier）；
    - 只有 final+safe+开奖时间可靠+eligible_after<=now 且无 defer 的候选可处理；
    - 每条 DELETE 前重新验证 strict-safe + 身份 + safe 复核 + participation_guard；
    - claim_delete_pending 成功后才发出 DELETE；claim 失败只跳过该条并记录（该条 0 DELETE），批间 barrier 兜底；绝不无 claim 发 DELETE。
    - DELETE retries=0；transport/响应不可确认 → unknown durable + 整 Job 立即停止；
    - 明确业务拒绝 → delete_failed durable + 整 Job 停止；
    - 平台成功且本地 deleted durable 才计 deleted_success，然后才下一条；
    - -352/-509/429 → 0 retry、停止全部后续批次、进入既有 risk/cooldown。
    """
    current = int(now_ts if now_ts is not None else time.time())
    _check_cancel(cancel_check)
    if not get_auto_delete_enabled():
        return _cleanup_delete_result(
            uid="", scope_total=0, processed=0, deleted=0, attempted=0,
            stop_reason="DISABLED", stop_message="自动删除未开启（0 DELETE）",
        )
    breaker = _AssessmentBreaker()
    profile_id = get_runtime_profile_id()
    database_path = str(db_path().resolve())
    with client_factory() as client:
        _, uid_int = _require_verified_login(client)
        uid = str(uid_int)
        if _repost_delete_barrier_blocked(uid):
            return _cleanup_delete_result(
                uid=uid, scope_total=0, processed=0, deleted=0, attempted=0,
                stop_reason="BARRIER", stop_message="存在 delete_pending/unknown，0 DELETE",
            )
        activities_map = {
            str(item.get("dynamic_id") or ""): item
            for item in load_activities()
            if isinstance(item, dict) and item.get("dynamic_id")
        }
        assessments = list_repost_assessments(uid)
        records = list_evaluable_reposts(uid)

        def _is_scope_candidate(record: object) -> bool:
            row = _record_dict(record)
            original_id = str(row.get("original_dynamic_id") or "").strip()
            ok, _ = _strict_safe(record, assessments.get(original_id), now_ts=current)
            if not ok:
                return False
            guard = get_guard(uid, original_id)
            if guard is not None and guard.repost_status in {"pending", "unknown", "suspected"}:
                return False
            return True

        scope_ids = sorted(
            (str(_record_dict(r).get("repost_dynamic_id") or "") for r in records if _is_scope_candidate(r))
        )
        scope_total = len(scope_ids)
        max_batches = max(1, (scope_total + AUTO_DELETE_BATCH_SIZE - 1) // AUTO_DELETE_BATCH_SIZE) if scope_total else 0
        attempted: set[str] = set()
        items: list[dict[str, str]] = []
        processed_total = 0
        deleted_success = 0
        made_progress_total = 0
        batches_done = 0
        stop_reason = ""
        stop_message = ""
        _progress(on_progress, 0, max(1, scope_total), f"自动安全清理：scope {scope_total} 条（frozen）")

        def _record_risk(risk_code: int | None, exc: object) -> None:
            if risk_code not in (-352, -509, 429):
                return
            try:
                from web.auto_remote_state import record_auto_remote_risk

                record_auto_remote_risk(
                    trigger_stage="cleanup_auto_delete",
                    reason=str(exc or "") or "",
                    code=str(risk_code),
                )
            except Exception:
                import logging

                logging.getLogger("repost_cleanup").exception(
                    "记录自动删除平台风控冷却失败 code=%s", risk_code
                )

        for batch_no in range(max_batches):
            _check_cancel(cancel_check)
            if breaker.stopped:
                break
            if get_runtime_profile_id() != profile_id or str(db_path().resolve()) != database_path:
                stop_reason = "RUNTIME_MISMATCH"
                stop_message = "运行期间 Profile/DB 路径变化，已停止"
                break
            if _repost_delete_barrier_blocked(uid):
                stop_reason = "BARRIER"
                stop_message = "批间出现 delete_pending/unknown，后续 0 DELETE"
                break
            assessments = list_repost_assessments(uid)
            remaining = [
                rid
                for rid in scope_ids
                if rid not in attempted
            ]
            if not remaining:
                break
            batch = remaining[:AUTO_DELETE_BATCH_SIZE]
            processed_batch = 0
            made_batch = 0
            for rid in batch:
                if breaker.stopped:
                    break
                _check_cancel(cancel_check)
                attempted.add(rid)
                original_id = ""
                status = "skipped"
                message = "安全条件未通过"
                try:
                    csrf = _assert_frozen_runtime(profile_id=profile_id, database_path=database_path, uid=uid)
                    record = get_repost(uid, rid)
                    if record is None:
                        raise ValueError("转发历史中不存在该目标")
                    row = _record_dict(record)
                    original_id = str(row.get("original_dynamic_id") or "").strip()
                    stored = assessments.get(original_id)
                    ok, why = _strict_safe(record, stored, now_ts=current)
                    if not ok:
                        raise ValueError(why)
                    guard = get_guard(uid, original_id)
                    if guard is not None and guard.repost_status in {"pending", "unknown", "suspected"}:
                        raise ValueError("participation_guard 未就绪")
                    _verify_owned_repost_detail(
                        client, uid=uid, repost_dynamic_id=rid, original_dynamic_id=original_id
                    )
                    recheck = _assess_original(
                        client, original_id=original_id, uid=uid, now_ts=current,
                        activities_map=activities_map,
                    )
                    if recheck.level != "safe":
                        raise ValueError("安全复核未通过（不再 safe）")
                    if not claim_delete_pending(uid, rid, requested_at=current):
                        raise ValueError("claim_delete_pending 失败")
                    try:
                        payload = client.post_json(
                            DELETE_REPOST_URL,
                            {"dyn_id_str": rid},
                            params={"platform": "web", "csrf": csrf},
                            referer=f"https://space.bilibili.com/{uid}/dynamic",
                            retries=0,
                            raise_on_code=False,
                        )
                    except Exception as exc:
                        risk = _risk_code_from_message(exc)
                        error = f"删除结果无法确认：{type(exc).__name__}"
                        try:
                            mark_delete_result(uid, rid, status="unknown", error=error)
                        except Exception:
                            pass  # 原 pending 保留为安全屏障
                        status, message = "unknown", error
                        if risk is not None:
                            _record_risk(risk, exc)
                        stop_reason = "DELETE_UNKNOWN"
                        stop_message = error
                        breaker.stopped = True
                        breaker.stop_message = error
                    else:
                        code = payload.get("code") if isinstance(payload, dict) else None
                        if type(code) is not int:
                            error = "删除响应缺少合法整数 code，无法确认"
                            try:
                                mark_delete_result(uid, rid, status="unknown", error=error)
                            except Exception:
                                pass
                            status, message = "unknown", error
                            stop_reason = "DELETE_UNKNOWN"
                            stop_message = error
                            breaker.stopped = True
                            breaker.stop_message = error
                        elif code == 0:
                            save_error = ""
                            saved = False
                            try:
                                saved = bool(
                                    mark_delete_result(
                                        uid, rid, status="deleted", deleted_at=int(time.time())
                                    )
                                )
                            except Exception as exc:
                                save_error = f"平台已返回成功但本地状态落盘失败：{exc}"
                            if saved:
                                status, message = "deleted", "删除成功"
                            else:
                                try:
                                    mark_delete_result(uid, rid, status="unknown", error=save_error)
                                except Exception:
                                    pass
                                status, message = "unknown", save_error or "本地 deleted 落盘失败"
                                stop_reason = "DELETE_UNKNOWN"
                                stop_message = message
                                breaker.stopped = True
                                breaker.stop_message = message
                        else:
                            api_message = str(payload.get("message") or payload.get("msg") or "").strip()
                            error = f"Bilibili API error {code}: {api_message}".rstrip()
                            try:
                                saved = bool(
                                    mark_delete_result(uid, rid, status="delete_failed", error=error)
                                )
                            except Exception:
                                saved = False
                            status, message = ("delete_failed" if saved else "unknown"), error
                            if code in (-352, -509, 429):
                                _record_risk(code, error)
                                breaker.rate_limited = True
                            # 批量路径：任何明确业务拒绝都停止整 Job，留给人工检查
                            stop_reason = "DELETE_FAILED"
                            stop_message = error
                            breaker.stopped = True
                            breaker.stop_message = error
                except AssessmentRateLimited as exc:
                    risk = _risk_code_from_message(exc)
                    status, message = "skipped", str(exc)
                    _record_risk(risk, exc)
                    stop_reason = "RISK"
                    stop_message = str(exc)
                    breaker.stopped = True
                    breaker.stop_message = str(exc)
                except (RemoteStateUnknown, RuntimeError, ValueError) as exc:
                    # 安全验证/读取失败：不能证明 safe → 保守跳过本条（可继续其它候选）
                    status, message = "skipped", str(exc)
                finally:
                    items.append(
                        _delete_failure_item(
                            repost_id=rid, original_id=original_id,
                            status=status, message=message,
                        )
                    )
                processed_batch += 1
                processed_total += 1
                if status in ("deleted", "delete_failed", "unknown"):
                    made_batch += 1
                    made_progress_total += 1
                if status == "deleted":
                    deleted_success += 1
                _progress(
                    on_progress,
                    len(attempted),
                    max(1, scope_total),
                    f"第{batch_no + 1}批：{processed_batch}/{len(batch)} 累计{len(attempted)}/{scope_total}（{status}）",
                    f"{rid}：{message}",
                )
                if breaker.stopped:
                    break
            batches_done += 1
            if breaker.stopped or stop_reason:
                break
            if processed_batch == 0:
                stop_reason = "NO_PROCESSED_PROGRESS"
                break
            if made_batch == 0:
                stop_reason = "NO_STATE_PROGRESS"
                break

        if not stop_reason and not breaker.stopped and batches_done == max_batches and processed_total == scope_total:
            stop_message = f"自动安全清理完成：删除 {deleted_success} 条（{batches_done} 批）"
        elif stop_reason:
            stop_message = stop_message or stop_reason
        elif scope_total == 0:
            stop_message = "没有符合 strict-safe 的候选（0 DELETE）"

        return _cleanup_delete_result(
            uid=uid,
            scope_total=scope_total,
            processed=processed_total,
            deleted=deleted_success,
            attempted=len(attempted),
            stop_reason=stop_reason,
            stop_message=stop_message,
            batches_done=batches_done,
            items=items,
            rate_limited=breaker.rate_limited,
            stopped_early=bool(stop_reason or breaker.stopped),
        )


def _cleanup_delete_result(*, uid: str, scope_total: int, processed: int, deleted: int,
                        attempted: int, stop_reason: str, stop_message: str,
                        batches_done: int = 0, items: list | None = None,
                        rate_limited: bool = False, stopped_early: bool = False) -> dict[str, Any]:
    counts = {}
    if uid:
        try:
            counts = count_repost_delete_statuses(uid)
        except Exception:
            counts = {}
    return {
        "uid": uid,
        "scope_size": scope_total,
        "processed": processed,
        "deleted_count": deleted,
        "attempted": attempted,
        "batches_done": batches_done,
        "delete_counts": counts,
        "stop_reason": stop_reason,
        "stopped_early": stopped_early,
        "rate_limited": rate_limited,
        "message": stop_message or "自动安全清理结束",
        "items": items or [],
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
    "AUTO_DELETE_BATCH_SIZE",
    "auto_cleanup_delete",
    "cleanup_has_pending_local_work",
    "auto_maintain",
    "get_auto_delete_enabled",
    "cleanup_maintenance_cycle",
    "set_auto_delete_enabled",
    "cleanup_health_check",
    "defer_candidate",
    "delete_reposts",
    "load_persisted_candidates",
    "repost_cleanup_summary",
    "restore_candidate",
    "scan_expired_reposts",
    "sync_repost_history",
]


# ---------------------------------------------------------------------------
# M8: 兼容旧的 cleanup_maintenance_cycle Job；Scheduler 已不再投递。
# 同一 Job：sync/维护 → 连续评估 → 仅当评估干净才进入 strict-safe 批量删除。
# ---------------------------------------------------------------------------


def cleanup_maintenance_cycle(
    *,
    on_progress: ProgressCallback | None = None,
    cancel_check: CancelCheck | None = None,
    now_ts: int | None = None,
    client_factory: Callable[[], BilibiliClient] = BilibiliClient,
) -> dict[str, Any]:
    from src.repost_cleanup import auto_maintain, auto_cleanup_delete, scan_expired_reposts

    steps = ["同步与本地维护", "连续智能评估", "strict-safe 自动删除"]
    phases: list[dict[str, Any]] = []
    stage = 0
    shared_manager: Any = None
    shared_client: BilibiliClient | None = None
    shared_entered = False

    def _shared_client_factory() -> _SharedClientLease:
        nonlocal shared_manager, shared_client, shared_entered
        if shared_manager is None:
            shared_manager = client_factory()
            shared_client = shared_manager.__enter__()
            shared_entered = True
        if shared_client is None:
            raise RuntimeError("清理周期无法初始化 Bilibili client")
        return _SharedClientLease(shared_client)

    def _report(step: int, message: str, log_line: str | None = None) -> None:
        _progress(on_progress, min(step, len(steps)), len(steps), message, log_line)

    def _run_stage(func, label: str, *, use_shared_client: bool = True) -> dict[str, Any]:
        nonlocal stage
        stage += 1
        _report(stage, f"{label}…", f"{label}开始")
        result = func(
            on_progress=None,
            cancel_check=cancel_check,
            now_ts=now_ts,
            client_factory=_shared_client_factory if use_shared_client else client_factory,
        )
        phases.append({"stage": label, "result": result})
        _report(stage, f"{label}完成", f"{label}完成")
        return result

    try:
        _check_cancel(cancel_check)
        stage1 = _run_stage(
            lambda **kwargs: auto_maintain(assess=False, **kwargs),
            "同步与本地维护",
        )
        _check_cancel(cancel_check)
        # stage1 门控：auto_maintain 对风险/失败是"捕获→落盘暂停→优雅返回"，
        # 必须读取其返回结果；未安全完成同步 → 0 assessment、0 DELETE。
        stage1_blocked = bool(
            stage1.get("rate_limited")
            or stage1.get("stopped_early")
            or stage1.get("stop_reason")
            or stage1.get("synced") is not True
        )
        if stage1_blocked:
            if stage1.get("budget_exhausted"):
                reason = "request_budget_exhausted"
                skip_message = (
                    "Bilibili 请求预算已用完，本轮 0 assessment、0 DELETE；"
                    "额度恢复后请手动再次执行，系统会从已有进度继续。"
                )
            else:
                reason = "pre_maintenance_blocked"
                skip_message = "前置维护未安全完成（风控/失败），本轮 0 assessment、0 DELETE。"
            stage2 = {"skipped": True, "reason": reason, "message": skip_message}
            stage3 = {"skipped": True, "reason": reason, "message": "前置维护未安全完成，自动删除未执行：0 DELETE。"}
            phases.append({"stage": "连续智能评估", "result": stage2})
            phases.append({"stage": "strict-safe 自动删除", "result": stage3})
            _report(
                len(steps),
                "前置维护未安全完成，已跳过评估与自动删除",
                str(stage1.get("message") or "stage1 未安全完成"),
            )
            return {
                "ok": True,
                "phases": phases,
                "message": skip_message,
            }
        stage2 = _run_stage(
            lambda **kwargs: scan_expired_reposts(**kwargs),
            "连续智能评估",
        )
        _check_cancel(cancel_check)
        # 进入删除的保守条件：评估无 risk/hard/partial 且 backlog 已全部收口
        scan = stage2
        blocked = bool(scan.get("rate_limited") or scan.get("stopped_early") or scan.get("stop_reason"))
        if blocked or int(scan.get("pending_originals") or 0) > 0:
            stage3 = {
                "skipped": True,
                "reason": "assessment_blocked_or_pending",
                "message": "评估阶段未完全收口，跳过自动删除（0 DELETE）",
            }
            phases.append({"stage": "strict-safe 自动删除", "result": stage3})
            _report(len(steps), "本轮自动删除已跳过（保守条件未满足）", str(stage3["message"]))
        else:
            stage3 = _run_stage(
                auto_cleanup_delete,
                "strict-safe 自动删除",
                use_shared_client=False,
            )
        return {
            "ok": True,
            "phases": phases,
            "message": f"自动维护周期完成：{len(phases)} 阶段",
        }
    except Exception:  # noqa: BLE001
        raise
    finally:
        if shared_manager is not None and shared_entered:
            shared_manager.__exit__(None, None, None)
