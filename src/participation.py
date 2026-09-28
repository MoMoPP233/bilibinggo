from __future__ import annotations

import json
import time
from contextlib import nullcontext
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from src.bilibili_auth import require_login
from src.bilibili_client import BilibiliClient
from src.bilibili_request_budget import RequestBudgetExhausted
from src.comment_breaker import CommentLimitActive, is_comment_breaker_active
from src.lottery_actions import (
    ACTION_INTERVAL_SEC,
    DEFAULT_PARTICIPATE_TEXT,
    ActionResult,
    ParticipationReadClient,
    build_dynamic_context,
    current_participation_risk_breaker,
    execute_full_participation,
    follow_user,
    has_comment,
    is_following,
    resolve_sender_uid,
    _api_code,
    _raise_payload_auth,
    _raise_payload_risk,
    _risk_error_from_exc,
)
from src.lottery_api import (
    fetch_dynamic_detail,
    fetch_notice_for_interact,
    fetch_notice_for_reserve,
    is_upower_dynamic,
)
from src.participation_log import (
    ParticipationActionRecord,
    ParticipationOutcome,
    TRIPLE_LOCAL_FAILURE_BACKOFF_SECONDS,
    participation_succeeded,
    append_action_record_unlocked,
    serialize_actions,
)
from src.participation_store import get_participation, set_participation_unlocked
from src.participation_guard import (
    ParticipationBusyError,
    confirm_repost,
    get_guard,
    mark_repost_suspected,
    participation_gate,
)
from src.participate_preflight import (
    ActivityAlreadyJoined,
    InvalidActivityError,
    ensure_activity_participatable,
)
from src.invalid_activities import invalidate_stored_activity, is_self_follow_failure
from src.user_data_lock import user_data_lock
from src.participate_text import resolve_participate_text_for_activity
from src.fetch_activity_info import mark_enriched_joined
from src.lottery_classifier import PARTICIPATABLE_TYPES
from src.sources.common import is_valid_dynamic_id, opus_link

RESERVE_CLICK_URL = "https://api.bilibili.com/x/dynamic/feed/reserve/click"
RESERVE_RESERVED_STATUS = 2
RESERVE_PARTICIPATE_STEPS = 2
_execution_uid: ContextVar[str | None] = ContextVar("participation_uid", default=None)


def _mutation_result_unknown(exc: BaseException) -> bool:
    """Return whether a started mutation has an indeterminate remote result.

    BilibiliClient wraps transport/JSON failures in RuntimeError with the
    original exception as ``__cause__``.  Business responses are represented
    as ActionResult(ok=False) and therefore do not enter this path.
    """
    if isinstance(exc, (httpx.HTTPError, json.JSONDecodeError)):
        return True
    return isinstance(exc, RuntimeError) and isinstance(
        exc.__cause__, (httpx.HTTPError, json.JSONDecodeError)
    )


def _pre_mutation_network_error(exc: BaseException) -> bool:
    """Classify transport failure while still in the read/preflight phase."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError):
            status_code = getattr(current.response, "status_code", None)
            # Auth/rate-limit responses are actionable global conditions, not
            # transient queueable reads.  Keep the existing auth/risk paths.
            if status_code in {401, 403, 429}:
                return False
        if isinstance(current, (httpx.HTTPError, json.JSONDecodeError)):
            return True
        current = current.__cause__ or current.__context__
    text = str(exc).strip().lower()
    return isinstance(exc, RuntimeError) and any(
        marker in text
        for marker in ("网络请求失败", "connecterror", "connection refused", "ssl:", "timed out")
    )


def _network_requeue_result(
    *,
    dynamic_id: str,
    lottery_type: str,
    persist: bool,
    dry_run: bool,
) -> ParticipateResult:
    result = ParticipateResult(
        dynamic_id=dynamic_id,
        lottery_type=lottery_type,
        status="failed",
        message="网络请求失败，已重新回到队列",
        action_text="",
        actions=[],
        context_snapshot={"result_class": "failed_local", "requeue": True},
    )
    _persist_result(result=result, persist=persist, dry_run=dry_run)
    return result


def _deferred_comment_result(
    *,
    dynamic_id: str,
    lottery_type: str,
    actions: list[ActionResult] | None = None,
    action_text: str = "",
    context_snapshot: dict[str, Any] | None = None,
    persist: bool,
    dry_run: bool,
) -> ParticipateResult:
    result = ParticipateResult(
        dynamic_id=dynamic_id,
        lottery_type=lottery_type,
        status="deferred",
        message="评论发送已暂停：已达到当前评论发送上限",
        action_text=action_text,
        actions=list(actions or []),
        context_snapshot={
            **(context_snapshot or {}),
            "result_class": "deferred_comment_limit",
        },
    )
    _persist_result(result=result, persist=persist, dry_run=dry_run)
    return result


def _invalid_result(
    *,
    dynamic_id: str,
    lottery_type: str,
    message: str,
    reason: str,
    actions: list[ActionResult] | None = None,
    action_text: str = "",
    context_snapshot: dict[str, Any] | None = None,
    persist: bool,
    dry_run: bool,
) -> ParticipateResult:
    result = ParticipateResult(
        dynamic_id=dynamic_id,
        lottery_type=lottery_type,
        status="failed",
        message=message,
        action_text=action_text,
        actions=list(actions or []),
        context_snapshot={**(context_snapshot or {}), "result_class": "invalid_activity", "invalid_reason": reason},
    )
    _persist_result(result=result, persist=persist, dry_run=dry_run)
    if persist and not dry_run:
        invalidate_stored_activity(dynamic_id, reason=reason)
    return result


def _global_participation_reason(exc: BaseException) -> str | None:
    if isinstance(exc, RequestBudgetExhausted):
        return "budget_exhausted"
    if getattr(exc, "code", None) in {-101, 401}:
        return "auth"
    if getattr(getattr(exc, "response", None), "status_code", None) == 401:
        return "auth"
    text = str(exc)
    if text.startswith("profile_writer_busy:") or any(
        marker in text
        for marker in (
            "参与期间登录账号发生变化",
            "运行期间 Profile 发生变化",
            "运行中的 Profile 已变化",
            "运行时 Profile 不存在",
        )
    ):
        return "authority"
    return None


def _check_shared_mutation_breaker() -> None:
    breaker = current_participation_risk_breaker()
    if breaker is not None:
        breaker.check()

@dataclass
class ParticipateResult:
    dynamic_id: str
    lottery_type: str
    status: ParticipationOutcome
    message: str
    action_text: str
    actions: list[ActionResult]
    context_snapshot: dict[str, Any]

    def to_dict(self) -> dict:
        payload = {
            "dynamic_id": self.dynamic_id,
            "lottery_type": self.lottery_type,
            "status": self.status,
            "message": self.message,
            "action_text": self.action_text,
            "actions": serialize_actions(self.actions),
            "context_snapshot": self.context_snapshot,
        }
        if self.status == "skipped" and self.context_snapshot.get("dedup_reason"):
            payload["skipped"] = True
            payload["skip_reason"] = self.context_snapshot["dedup_reason"]
        result_class = self.context_snapshot.get("result_class")
        if result_class:
            payload["result_class"] = result_class
        return payload


def _confirmed(value: Any) -> bool:
    return value is True or (type(value) is int and value == 1)


def _dedup_skip(dynamic_id: str, lottery_type: str, reason: str, message: str) -> ParticipateResult:
    return ParticipateResult(
        dynamic_id=dynamic_id, lottery_type=lottery_type, status="skipped",
        message=message, action_text="", actions=[], context_snapshot={"dedup_reason": reason},
    )


def _save_platform_joined(dynamic_id: str, *, persist: bool, dry_run: bool) -> None:
    if persist and not dry_run:
        with user_data_lock():
            set_participation_unlocked(dynamic_id, "已参加", uid=_execution_uid.get())
        mark_enriched_joined(dynamic_id)


def _notice_snapshot(notice: dict | None) -> dict[str, Any]:
    if not notice:
        return {}
    return {
        "lottery_id": notice.get("lottery_id"),
        "sender_uid": notice.get("sender_uid"),
        "need_post": notice.get("need_post"),
        "followed": notice.get("followed"),
        "reposted": notice.get("reposted"),
        "participated": notice.get("participated"),
        "status": notice.get("status"),
        "lottery_time": notice.get("lottery_time"),
    }


def _context_snapshot(context: Any | None, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if context is not None:
        payload = {
            "sender_uid": getattr(context, "sender_uid", None),
            "liked": getattr(context, "liked", None),
            "followed": getattr(context, "followed", None),
            "favorited": getattr(context, "favorited", None),
            "favorite_available": getattr(context, "favorite_available", None),
            "reposted": getattr(context, "reposted", None),
            "commented": getattr(context, "commented", None),
            "comment_rid": getattr(context, "comment_rid", None),
        }
    if extra:
        payload.update(extra)
    return payload


def _persist_result(
    *,
    result: ParticipateResult,
    persist: bool,
    dry_run: bool,
) -> None:
    if not persist or dry_run:
        return
    joined = result.status == "joined" and participation_succeeded(
        result.actions,
        lottery_type=result.lottery_type,
    )
    recorded_at = int(time.time())
    snapshot = dict(result.context_snapshot or {})
    if result.status == "failed" and snapshot.get("result_class") == "failed_local" and not snapshot.get("requeue"):
        snapshot.setdefault(
            "backoff_until",
            recorded_at + TRIPLE_LOCAL_FAILURE_BACKOFF_SECONDS,
        )
    result.context_snapshot = snapshot
    record = ParticipationActionRecord(
        recorded_at=recorded_at,
        dynamic_id=result.dynamic_id,
        lottery_type=result.lottery_type,
        status=result.status,
        message=result.message,
        action_text=result.action_text,
        actions=serialize_actions(result.actions),
        context_snapshot=snapshot,
    )
    with user_data_lock():
        append_action_record_unlocked(record, uid=_execution_uid.get())
        if joined:
            set_participation_unlocked(result.dynamic_id, "已参加", uid=_execution_uid.get())
        elif result.status == "failed" and snapshot.get("result_class") == "failed_local" and snapshot.get("requeue"):
            set_participation_unlocked(result.dynamic_id, "未参加", uid=_execution_uid.get())
    if joined:
        mark_enriched_joined(result.dynamic_id)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _is_notice_active(notice: dict | None) -> tuple[bool, str]:
    if not notice:
        return False, "未找到抽奖信息"
    if _safe_int(notice.get("status")) != 0:
        return False, "抽奖已结束或不可参与"
    lottery_time = _safe_int(notice.get("lottery_time"))
    if lottery_time and lottery_time <= int(time.time()):
        return False, "已过开奖时间"
    return True, ""


def _resolve_action_text(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    action_text: str | None,
) -> tuple[str, dict[str, Any]]:
    if action_text is None:
        resolved = resolve_participate_text_for_activity(client, dynamic_id=dynamic_id)
        return resolved.text, {
            "participate_text_source": resolved.source,
            "participate_text_pool_size": resolved.pool_size,
        }
    text = (action_text or DEFAULT_PARTICIPATE_TEXT).strip() or DEFAULT_PARTICIPATE_TEXT
    return text, {
        "participate_text_source": "custom",
        "participate_text_pool_size": 0,
    }


def participate_five_action_lottery(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    lottery_type: Literal["互动抽奖", "转发抽奖"],
    action_text: str | None = None,
    dry_run: bool = False,
    persist: bool = True,
    on_step: Callable[[int, int, str, str], None] | None = None,
) -> ParticipateResult:
    if not dry_run and is_comment_breaker_active():
        return _deferred_comment_result(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            action_text=(action_text or DEFAULT_PARTICIPATE_TEXT).strip() or DEFAULT_PARTICIPATE_TEXT,
            persist=persist,
            dry_run=dry_run,
        )
    reader = client if isinstance(client, ParticipationReadClient) else ParticipationReadClient(client)
    text = action_text or DEFAULT_PARTICIPATE_TEXT
    text_meta: dict[str, Any] = {}
    notice: dict | None = None
    sender_uid: int | None = None

    try:
        detail_item = fetch_dynamic_detail(reader, dynamic_id)
        reader.raise_if_failed()
    except Exception as exc:
        if _pre_mutation_network_error(exc):
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                persist=persist,
                dry_run=dry_run,
            )
        raise
    if not detail_item:
        raise RuntimeError("无法获取动态详情，请稍后重试")
    if is_upower_dynamic(detail_item):
        result = ParticipateResult(
            dynamic_id=dynamic_id, lottery_type=lottery_type, status="skipped",
            message="充电专属抽奖，不参与", action_text=text, actions=[], context_snapshot={},
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    if lottery_type == "互动抽奖":
        try:
            resolved = fetch_notice_for_interact(reader, dynamic_id)
            reader.raise_if_failed()
        except Exception as exc:
            if _pre_mutation_network_error(exc):
                return _network_requeue_result(
                    dynamic_id=dynamic_id,
                    lottery_type=lottery_type,
                    persist=persist,
                    dry_run=dry_run,
                )
            raise
        if not resolved:
            result = ParticipateResult(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                status="failed",
                message="未找到互动抽奖信息",
                action_text=text,
                actions=[],
                context_snapshot={"result_class": "failed_local"},
            )
            _persist_result(result=result, persist=persist, dry_run=dry_run)
            return result
        notice, _, _ = resolved
        active, reason = _is_notice_active(notice)
        if not active:
            result = ParticipateResult(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                status="skipped",
                message=reason,
                action_text=text,
                actions=[],
                context_snapshot=_notice_snapshot(notice),
            )
            _persist_result(result=result, persist=persist, dry_run=dry_run)
            return result
        sender_uid = int(notice.get("sender_uid") or 0) or None

    uid = _execution_uid.get() or str(require_login()[1])
    guard = get_guard(uid, dynamic_id)
    if notice and _confirmed(notice.get("reposted")) and not dry_run:
        confirm_repost(uid, dynamic_id)
    if notice and _confirmed(notice.get("participated")):
        _save_platform_joined(dynamic_id, persist=persist, dry_run=dry_run)
        result = _dedup_skip(dynamic_id, lottery_type, "platform_joined", "平台已确认参与，已跳过")
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    try:
        context = build_dynamic_context(
            reader, dynamic_id=dynamic_id, sender_uid=sender_uid, action_text="",
            detail_item=detail_item, notice=notice,
            repost_confirmed=bool(guard and guard.repost_status == "confirmed"),
            check_comment=False, check_follow=False,
        )
        reader.raise_if_failed()
    except InvalidActivityError:
        raise
    except Exception as exc:
        if _pre_mutation_network_error(exc):
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                persist=persist,
                dry_run=dry_run,
            )
        raise
    if context.reposted is True:
        if not dry_run:
            confirm_repost(uid, dynamic_id)
    elif context.liked is True and context.favorited is True:
        if not dry_run:
            mark_repost_suspected(uid, dynamic_id)
        result = _dedup_skip(
            dynamic_id, lottery_type, "repost_suspected",
            "已点赞并收藏，但无法确认是否转发；疑似已参与，已暂停自动转发，需人工确认",
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    try:
        # Only after the suspected-participation branch is cleared do we read
        # follow/comment state and resolve the action text.
        context.followed = is_following(reader, uid=context.sender_uid, referer=context.referer)
        reader.raise_if_failed()
        text, text_meta = _resolve_action_text(reader, dynamic_id=dynamic_id, action_text=action_text)
        context.commented = has_comment(
            reader,
            rid=context.comment_rid,
            comment_type=context.comment_type,
            action_text=text,
            referer=context.referer,
            dynamic_id=dynamic_id,
        )
        reader.raise_if_failed()
    except Exception as exc:
        if _pre_mutation_network_error(exc):
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                persist=persist,
                dry_run=dry_run,
            )
        raise

    completed_actions: list[ActionResult] = []
    mutation_action: list[str | None] = [None]

    def on_mutation_start(action: str) -> None:
        mutation_action[0] = action

    try:
        actions, context = execute_full_participation(
            reader,
            dynamic_id=dynamic_id,
            sender_uid=sender_uid,
            action_text=text,
            dry_run=dry_run,
            on_step=on_step,
            context=context,
            on_action=completed_actions.append,
            on_mutation_start=on_mutation_start,
        )
    except Exception as exc:
        if isinstance(exc, CommentLimitActive):
            return _deferred_comment_result(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                actions=completed_actions,
                action_text=text,
                context_snapshot=_context_snapshot(
                    context,
                    extra={**text_meta, "notice": _notice_snapshot(notice)},
                ),
                persist=persist,
                dry_run=dry_run,
            )
        # 明确平台风控：保留已完成动作的记录，触发共享 breaker，并把风险上抛给
        # JobRunner（唯一 6h cooldown 出口），不能被普通失败语义吞掉/覆盖。
        risk = _risk_error_from_exc(exc)
        global_reason = _global_participation_reason(exc)
        if risk is not None or global_reason is not None:
            breaker = current_participation_risk_breaker()
            if breaker is not None:
                if risk is not None:
                    breaker.trigger(risk.code)
            result = ParticipateResult(
                dynamic_id=dynamic_id,
                lottery_type=lottery_type,
                status="failed",
                message=str(risk or exc),
                action_text=text,
                actions=completed_actions,
                context_snapshot=_context_snapshot(
                    context,
                    extra={
                        **text_meta,
                        "notice": _notice_snapshot(notice),
                        "result_class": "global_risk" if risk is not None else f"global_{global_reason}",
                    },
                ),
            )
            _persist_result(result=result, persist=persist, dry_run=dry_run)
            if risk is not None:
                raise risk from exc
            raise
        # Keep completed actions even when a later request fails. Unexpected
        # exceptions still propagate after preserving the diagnostic record.
        message = str(exc).strip() or "参与失败"
        if "无法获取动态详情" in message:
            message = "无法获取动态详情，请稍后重试"
        unknown = mutation_action[0] is not None and _mutation_result_unknown(exc)
        if unknown:
            action_label = {
                "like": "点赞",
                "follow": "关注",
                "favorite": "收藏",
                "repost": "转发",
                "comment": "评论",
                "reserve": "预约",
            }.get(str(mutation_action[0] or "操作"), "操作")
            message = f"{action_label}结果未知，已停止自动重试"
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            status="failed",
            message=message,
            action_text=text,
            actions=completed_actions,
            context_snapshot=_context_snapshot(
                context,
                extra={
                    **text_meta,
                    "notice": _notice_snapshot(notice),
                    "result_class": "unknown" if unknown else "failed_local",
                    **({"unknown_action": mutation_action[0]} if unknown else {}),
                },
            ),
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        if not isinstance(exc, (RuntimeError, httpx.HTTPError, json.JSONDecodeError)):
            raise
        return result

    self_follow_failure = next(
        (item for item in actions if item.action == "follow" and not item.ok and is_self_follow_failure(item.detail)),
        None,
    )
    if self_follow_failure is not None:
        return _invalid_result(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            message="不能关注自己，活动已删除",
            reason="self_follow_22001",
            actions=actions,
            action_text=text,
            context_snapshot=_context_snapshot(
                context,
                extra={**text_meta, "notice": _notice_snapshot(notice)},
            ),
            persist=persist,
            dry_run=dry_run,
        )

    if dry_run:
        status: ParticipationOutcome = "dry_run"
        message = "预演完成，未实际请求 B 站"
    elif participation_succeeded(actions, lottery_type=lottery_type):
        status = "joined"
        comment = next((item for item in actions if item.action == "comment"), None)
        if lottery_type == "互动抽奖" and comment and not comment.ok:
            message = "核心操作已完成（评论受限，已视为参与成功）"
        else:
            message = "五项操作均已完成" if lottery_type == "转发抽奖" else "核心操作均已完成"
    else:
        status = "failed"
        failed = [item for item in actions if not item.ok]
        message = failed[-1].detail if failed else "部分操作失败"

    snapshot = _context_snapshot(
        context,
        extra={"notice": _notice_snapshot(notice), **text_meta},
    )
    if status == "failed":
        # Some non-idempotent actions (notably repost) convert a transport
        # exception into a failed ActionResult after persisting their guard.
        # Preserve that uncertainty as an unknown barrier instead of treating
        # it as an ordinary local failure eligible for retry.
        unknown_action = next(
            (
                item.action
                for item in actions
                if not item.ok and "结果未知" in str(item.detail or "")
            ),
            None,
        )
        if unknown_action:
            action_label = {
                "like": "点赞",
                "follow": "关注",
                "favorite": "收藏",
                "repost": "转发",
                "comment": "评论",
                "reserve": "预约",
            }.get(str(unknown_action), "操作")
            message = f"{action_label}结果未知，已停止自动重试"
            snapshot["result_class"] = "unknown"
            snapshot["unknown_action"] = unknown_action
        else:
            snapshot.setdefault("result_class", "failed_local")
    result = ParticipateResult(
        dynamic_id=dynamic_id,
        lottery_type=lottery_type,
        status=status,
        message=message,
        action_text=text,
        actions=actions,
        context_snapshot=snapshot,
    )
    _persist_result(result=result, persist=persist, dry_run=dry_run)
    return result


def _resolve_reserve_info(client: BilibiliClient, dynamic_id: str) -> dict[str, Any]:
    item = fetch_dynamic_detail(client, dynamic_id)
    if not item:
        raise RuntimeError("无法获取动态详情，预约信息解析失败")
    additional = ((item.get("modules") or {}).get("module_dynamic") or {}).get("additional") or {}
    reserve = additional.get("reserve") or {}
    button = reserve.get("button") or {}
    reserve_id = reserve.get("rid")
    if not reserve_id:
        raise RuntimeError("动态中未找到预约组件 rid")
    sender_uid: int | None
    try:
        sender_uid = resolve_sender_uid(item)
    except RuntimeError:
        sender_uid = None
    return {
        "reserve_id": int(reserve_id),
        "reserve_total": int(reserve.get("reserve_total") or 0),
        "button_status": int(button.get("status") or 0),
        "title": str(reserve.get("title") or ""),
        "sender_uid": sender_uid,
        "referer": opus_link(dynamic_id),
    }


def _reserve_click(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    reserve_id: int,
    reserve_total: int,
    button_status: int,
    dry_run: bool,
) -> ActionResult:
    if button_status == RESERVE_RESERVED_STATUS:
        return ActionResult("reserve", True, "已预约，跳过")

    if dry_run:
        return ActionResult("reserve", True, f"将预约 reserve_id={reserve_id}")

    csrf, _ = require_login()
    referer = opus_link(dynamic_id)
    payload = client.post_json(
        RESERVE_CLICK_URL,
        {
            "reserve_id": reserve_id,
            "cur_btn_status": button_status,
            "dynamic_id_str": dynamic_id,
            "reserve_total": reserve_total,
            "spmid": "333.1369.0.0",
        },
        params={"csrf": csrf},
        referer=referer,
        raise_on_code=False,
        retries=0,  # non-idempotent mutation：transport unknown → 绝不自动重发
    )
    code = _api_code(payload)
    if code == 0:
        data = payload.get("data") or {}
        final_status = int(data.get("final_btn_status") or 0)
        toast = str(data.get("toast") or "")
        if final_status == RESERVE_RESERVED_STATUS:
            return ActionResult("reserve", True, toast or "预约成功")
        if "预约成功" in toast or "已参与" in toast:
            return ActionResult("reserve", True, toast)
        if "取消" in toast:
            return ActionResult("reserve", False, toast or "预约操作被取消")
        return ActionResult(
            "reserve",
            False,
            toast or f"预约结果未确认（status={final_status}）",
        )
    message = str(payload.get("message") or payload.get("msg") or "")
    if "已预约" in message:
        return ActionResult("reserve", True, "已预约")
    _raise_payload_auth(payload, "预约")
    _raise_payload_risk(payload, "预约")
    return ActionResult("reserve", False, f"code={code} {message}".strip())


def participate_reserve_lottery(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    dry_run: bool = False,
    persist: bool = True,
    on_step: Callable[[int, int, str, str], None] | None = None,
) -> ParticipateResult:
    notice: dict | None = None
    notice_error: Exception | None = None
    try:
        resolved = fetch_notice_for_reserve(client, dynamic_id)
        if resolved:
            notice, _, _ = resolved
            active, reason = _is_notice_active(notice)
            if not active:
                result = ParticipateResult(
                    dynamic_id=dynamic_id,
                    lottery_type="预约抽奖",
                    status="skipped",
                    message=reason,
                    action_text="",
                    actions=[],
                    context_snapshot=_notice_snapshot(notice),
                )
                _persist_result(result=result, persist=persist, dry_run=dry_run)
                return result
    except RuntimeError as exc:
        notice = None
        notice_error = exc
        if _pre_mutation_network_error(exc):
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type="预约抽奖",
                persist=persist,
                dry_run=dry_run,
            )

    try:
        reserve_info = _resolve_reserve_info(client, dynamic_id)
    except (RuntimeError, ValueError) as exc:
        if _pre_mutation_network_error(exc) or (notice_error is not None and _pre_mutation_network_error(notice_error)):
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type="预约抽奖",
                persist=persist,
                dry_run=dry_run,
            )
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type="预约抽奖",
            status="failed",
            message=str(exc),
            action_text="",
            actions=[],
            context_snapshot={**_notice_snapshot(notice), "result_class": "failed_local"},
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    total_steps = RESERVE_PARTICIPATE_STEPS
    sender_uid = _safe_int(reserve_info.get("sender_uid"))
    if not sender_uid:
        sender_uid = _safe_int((notice or {}).get("sender_uid"))
    if not sender_uid:
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type="预约抽奖",
            status="failed",
            message="无法解析 UP 主 UID，无法完成关注",
            action_text="",
            actions=[],
            context_snapshot={
                **_notice_snapshot(notice),
                "reserve_id": reserve_info["reserve_id"],
                "reserve_total": reserve_info["reserve_total"],
                "button_status": reserve_info["button_status"],
                "title": reserve_info["title"],
                "result_class": "failed_local",
            },
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    referer = str(reserve_info["referer"])
    follow_risk: Exception | None = None
    follow_global: Exception | None = None
    follow_global_reason: str | None = None
    follow_unknown = False
    follow_network = False

    if on_step:
        on_step(1, total_steps, f"正在关注（1/{total_steps}）", "follow")
    try:
        followed = is_following(client, uid=sender_uid, referer=referer)
        if isinstance(client, ParticipationReadClient):
            client.raise_if_failed()
        if dry_run:
            follow_action = ActionResult(
                "follow",
                True,
                f"uid={sender_uid} 已关注，跳过" if followed else f"将关注 uid={sender_uid}",
            )
        else:
            csrf, _ = require_login()
            if followed:
                follow_action = ActionResult("follow", True, f"uid={sender_uid} 已关注，跳过")
            else:
                _check_shared_mutation_breaker()
                follow_action = follow_user(client, uid=sender_uid, csrf=csrf, referer=referer)
    except RuntimeError as exc:
        risk = _risk_error_from_exc(exc)
        global_reason = _global_participation_reason(exc)
        if risk is not None:
            follow_risk = risk
            follow_action = ActionResult("follow", False, str(risk))
        elif global_reason is not None:
            follow_global = exc
            follow_global_reason = global_reason
            follow_action = ActionResult("follow", False, str(exc).strip() or "关注失败")
        else:
            follow_network = _pre_mutation_network_error(exc)
            follow_unknown = not follow_network and _mutation_result_unknown(exc)
            follow_action = ActionResult("follow", False, str(exc).strip() or "关注失败")

    actions: list[ActionResult] = [follow_action]
    snapshot = _context_snapshot(
        None,
        extra={
            **_notice_snapshot(notice),
            "sender_uid": sender_uid,
            "reserve_id": reserve_info["reserve_id"],
            "reserve_total": reserve_info["reserve_total"],
            "button_status": reserve_info["button_status"],
            "title": reserve_info["title"],
        },
    )

    if not follow_action.ok:
        if follow_network:
            return _network_requeue_result(
                dynamic_id=dynamic_id,
                lottery_type="预约抽奖",
                persist=persist,
                dry_run=dry_run,
            )
        if is_self_follow_failure(follow_action.detail):
            return _invalid_result(
                dynamic_id=dynamic_id,
                lottery_type="预约抽奖",
                message="不能关注自己，活动已删除",
                reason="self_follow_22001",
                actions=actions,
                context_snapshot=snapshot,
                persist=persist,
                dry_run=dry_run,
            )
        follow_result_class = (
            "global_risk" if follow_risk is not None else
            (f"global_{follow_global_reason}" if follow_global is not None else
             ("unknown" if follow_unknown else "failed_local"))
        )
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type="预约抽奖",
            status="failed",
            message=follow_action.detail,
            action_text="",
            actions=actions,
            context_snapshot={**snapshot, "result_class": follow_result_class},
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        if follow_risk is not None:
            breaker = current_participation_risk_breaker()
            if breaker is not None:
                breaker.trigger(getattr(follow_risk, "code", -352))
            raise follow_risk
        if follow_global is not None:
            raise follow_global
        return result

    reserve_mutation_started = False
    try:
        if not dry_run:
            time.sleep(ACTION_INTERVAL_SEC)

        if on_step:
            on_step(2, total_steps, f"正在预约（2/{total_steps}）", "reserve")
        _check_shared_mutation_breaker()
        reserve_mutation_started = True
        reserve_action = _reserve_click(
            client,
            dynamic_id=dynamic_id,
            reserve_id=reserve_info["reserve_id"],
            reserve_total=reserve_info["reserve_total"],
            button_status=reserve_info["button_status"],
            dry_run=dry_run,
        )
        actions.append(reserve_action)

        if dry_run:
            status: ParticipationOutcome = "dry_run"
            message = "预演完成，未实际请求 B 站"
        elif participation_succeeded(actions, lottery_type="预约抽奖"):
            status = "joined"
            message = "关注与预约均已完成"
        else:
            status = "failed"
            message = reserve_action.detail if not reserve_action.ok else "部分操作失败"

        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type="预约抽奖",
            status=status,
            message=message,
            action_text="",
            actions=actions,
            context_snapshot={
                **snapshot,
                **({"result_class": "failed_local"} if status == "failed" else {}),
            },
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result

    except Exception as exc:
        # 明确平台风控：保留已发起动作记录并触发共享 breaker，把风险上抛给 JobRunner。
        risk = _risk_error_from_exc(exc)
        global_reason = _global_participation_reason(exc)
        if risk is None and global_reason is None:
            unknown = reserve_mutation_started and _mutation_result_unknown(exc)
            message = str(exc).strip() or "预约失败"
            if unknown:
                message = "预约结果未知，已停止自动重试"
            result = ParticipateResult(
                dynamic_id=dynamic_id,
                lottery_type="预约抽奖",
                status="failed",
                message=message,
                action_text="",
                actions=actions,
                context_snapshot={
                    **snapshot,
                    "result_class": "unknown" if unknown else "failed_local",
                    **({"unknown_action": "reserve"} if unknown else {}),
                },
            )
            _persist_result(result=result, persist=persist, dry_run=dry_run)
            return result
        breaker = current_participation_risk_breaker()
        if breaker is not None:
            breaker.trigger(risk.code)
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type="预约抽奖",
            status="failed",
            message=str(risk or exc),
            action_text="",
            actions=actions,
            context_snapshot={
                **snapshot,
                "result_class": "global_risk" if risk is not None else f"global_{global_reason}",
            },
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        if risk is not None:
            raise risk from exc
        raise


def _participate_activity_unlocked(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    lottery_type: str,
    action_text: str | None = None,
    dry_run: bool = False,
    persist: bool = True,
    on_step: Callable[[int, int, str, str], None] | None = None,
) -> ParticipateResult:
    if not is_valid_dynamic_id(dynamic_id):
        raise ValueError("dynamic_id 无效")
    if lottery_type == "充电抽奖":
        result = ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            status="skipped",
            message="充电专属抽奖，不参与",
            action_text=action_text,
            actions=[],
            context_snapshot={},
        )
        _persist_result(result=result, persist=persist, dry_run=dry_run)
        return result
    if lottery_type not in PARTICIPATABLE_TYPES:
        raise RuntimeError(f"不支持的抽奖类型: {lottery_type}")
    if lottery_type == "互动抽奖":
        return participate_five_action_lottery(
            client,
            dynamic_id=dynamic_id,
            lottery_type="互动抽奖",
            action_text=action_text,
            dry_run=dry_run,
            persist=persist,
            on_step=on_step,
        )
    if lottery_type == "转发抽奖":
        return participate_five_action_lottery(
            client,
            dynamic_id=dynamic_id,
            lottery_type="转发抽奖",
            action_text=action_text,
            dry_run=dry_run,
            persist=persist,
            on_step=on_step,
        )
    if lottery_type == "预约抽奖":
        return participate_reserve_lottery(
            client,
            dynamic_id=dynamic_id,
            dry_run=dry_run,
            persist=persist,
            on_step=on_step,
        )
    raise RuntimeError(f"不支持的抽奖类型: {lottery_type}")


def participate_activity(
    client: BilibiliClient | None = None,
    *,
    dynamic_id: str,
    lottery_type: str,
    action_text: str | None = None,
    dry_run: bool = False,
    persist: bool = True,
    on_step: Callable[[int, int, str, str], None] | None = None,
    preflight: bool = False,
) -> ParticipateResult:
    """Web、自动与 CLI 共用入口：跨进程锁内先查本地，再访问当前目标。

    persist 控制参与日志/整体成功记录；非预演转发的防重记录始终持久化。
    """
    dynamic_id = str(dynamic_id or "").strip()
    if not is_valid_dynamic_id(dynamic_id):
        raise ValueError("dynamic_id 无效")
    if lottery_type not in PARTICIPATABLE_TYPES and lottery_type != "充电抽奖":
        raise RuntimeError(f"不支持的抽奖类型: {lottery_type}")
    _, account_uid = require_login()  # 只读本地 Cookie，不发登录/目标状态请求。
    uid = str(account_uid)

    def checked_step(step: int, total: int, message: str, action: str) -> None:
        if str(require_login()[1]) != uid:
            raise RuntimeError("参与期间登录账号发生变化，已停止操作")
        if on_step:
            on_step(step, total, message, action)

    try:
        with participation_gate(uid, dynamic_id):
            token = _execution_uid.set(uid)
            try:
                local = get_participation(dynamic_id, uid=uid)
                if local is not None and local.user_status == "已参加":
                    return _dedup_skip(dynamic_id, lottery_type, "already_joined", "本地已记录参加，已跳过")
                guard = get_guard(uid, dynamic_id)
                if guard and guard.repost_status in {"pending", "unknown", "suspected"}:
                    labels = {
                        "pending": "曾发起转发，结果尚未确认",
                        "unknown": "上次转发结果未知",
                        "suspected": "存在疑似参与痕迹",
                    }
                    return _dedup_skip(
                        dynamic_id, lottery_type, f"repost_{guard.repost_status}",
                        f"{labels[guard.repost_status]}，禁止自动重发，需人工确认",
                    )
                checked_step(0, RESERVE_PARTICIPATE_STEPS if lottery_type == "预约抽奖" else 5,
                             "本地防重检查通过，正在检查当前目标…", "check")
                if (
                    not dry_run
                    and lottery_type in {"互动抽奖", "转发抽奖"}
                    and is_comment_breaker_active()
                ):
                    return _deferred_comment_result(
                        dynamic_id=dynamic_id,
                        lottery_type=lottery_type,
                        action_text=(action_text or DEFAULT_PARTICIPATE_TEXT).strip()
                        or DEFAULT_PARTICIPATE_TEXT,
                        persist=persist,
                        dry_run=dry_run,
                    )
                # 本地明确成功/待确认时不会构造客户端，亦不会发生暖机请求。
                manager = nullcontext(client) if client is not None else BilibiliClient(warmup=False)
                with manager as active_client:
                    reader = (
                        active_client if isinstance(active_client, ParticipationReadClient)
                        else ParticipationReadClient(active_client)
                    )
                    if preflight:
                        try:
                            ensure_activity_participatable(
                                reader, dynamic_id, lottery_type_hint=lottery_type, uid=uid,
                            )
                            reader.raise_if_failed()
                        except ActivityAlreadyJoined as exc:
                            reader.raise_if_failed()
                            fresh_local = get_participation(dynamic_id, uid=uid)
                            if fresh_local and fresh_local.user_status == "已参加":
                                return _dedup_skip(dynamic_id, lottery_type, "already_joined", "本地已记录参加，已跳过")
                            if lottery_type == "互动抽奖":
                                resolved = fetch_notice_for_interact(reader, dynamic_id)
                                reader.raise_if_failed()
                                if resolved and _confirmed(resolved[0].get("reposted")) and not dry_run:
                                    confirm_repost(uid, dynamic_id)
                            if exc.item.get("platform_participated") is True:
                                _save_platform_joined(dynamic_id, persist=persist, dry_run=dry_run)
                            result = _dedup_skip(
                                dynamic_id, lottery_type, "platform_joined", "平台已确认参与，已跳过",
                            )
                            _persist_result(result=result, persist=persist, dry_run=dry_run)
                            return result
                        except InvalidActivityError as exc:
                            return _invalid_result(
                                dynamic_id=dynamic_id,
                                lottery_type=lottery_type,
                                message="无法打开活动链接，活动已删除",
                                reason=exc.reason,
                                persist=persist,
                                dry_run=dry_run,
                            )
                        except Exception as exc:
                            if _pre_mutation_network_error(exc):
                                return _network_requeue_result(
                                    dynamic_id=dynamic_id,
                                    lottery_type=lottery_type,
                                    persist=persist,
                                    dry_run=dry_run,
                                )
                            raise
                    checked_step(0, RESERVE_PARTICIPATE_STEPS if lottery_type == "预约抽奖" else 5,
                                 "检查当前目标参与状态…", "check")
                    return _participate_activity_unlocked(
                        reader, dynamic_id=dynamic_id, lottery_type=lottery_type,
                        action_text=action_text, dry_run=dry_run, persist=persist,
                        on_step=checked_step,
                    )
            finally:
                _execution_uid.reset(token)
    except InvalidActivityError as exc:
        return _invalid_result(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            message="无法打开活动链接，活动已删除",
            reason=exc.reason,
            persist=persist,
            dry_run=dry_run,
        )
    except ParticipationBusyError:
        return _dedup_skip(
            dynamic_id, lottery_type, "participation_busy", "当前账号正在处理这个动态，已跳过重复请求",
        )
    # Read-phase transport failures are classified at each boundary that can
    # prove no mutation has started.  Do not classify an arbitrary escaped
    # exception here: it may come from a non-idempotent mutation whose result
    # is unknown and must remain fail-closed.
