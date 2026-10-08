"""Explicit manual recovery for uncertain actions in the normal participation flow."""

from __future__ import annotations

from contextlib import nullcontext
from typing import Any

from src.bilibili_auth import require_login
from src.bilibili_client import BilibiliClient
from src.comment_breaker import is_comment_breaker_active
from src.comment_confirmation import get_comment_status
from src.data_paths import get_runtime_profile_id
from src.favorite_confirmation import get_favorite_status, peek_favorite_status
from src.lottery_actions import (
    ParticipationReadClient,
    comment_dynamic,
    current_participation_risk_breaker,
    favorite_dynamic,
    repost_dynamic,
)
from src.lottery_api import fetch_dynamic_detail
from src.manual_unknown_retry import begin_attempt, finish_attempt, generation
from src.participation_guard import get_guard, participation_gate
from src.participation_store import get_participation
from src.platform_risk import risk_code_of
from src.sources.common import is_valid_dynamic_id, opus_link
from web.auto_remote_state import is_risk_paused

_LABELS = {"comment": "评论", "favorite": "收藏", "repost": "转发"}


def _statuses(uid: str, dynamic_id: str, *, read_only: bool = False) -> dict[str, str | None]:
    repost = get_guard(uid, dynamic_id)
    return {
        "comment": get_comment_status(dynamic_id, uid=uid),
        "favorite": (
            peek_favorite_status(dynamic_id, uid=uid) if read_only
            else get_favorite_status(dynamic_id, uid=uid)
        ),
        "repost": repost.repost_status if repost else None,
    }


def inspect_unknown_actions(dynamic_id: str) -> dict[str, Any]:
    """Local-only UI preflight for an explicit full-pipeline retry click."""
    if not is_valid_dynamic_id(dynamic_id):
        raise ValueError("活动 ID 无效")
    uid = str(require_login()[1])
    statuses = _statuses(uid, dynamic_id, read_only=True)
    return {
        "dynamic_id": dynamic_id,
        "uid": uid,
        "actions": [
            {"action": action, "label": _LABELS[action], "status": status,
             "generation": generation(uid, dynamic_id, action)}
            for action, status in statuses.items() if status in {"pending", "unknown"}
        ],
    }


def retry_unknown_participation(
    dynamic_id: str,
    lottery_type: str,
    *,
    expected_profile_id: str,
    expected_uid: str,
    on_step=None,
    client: BilibiliClient | None = None,
):
    """Run the original pipeline; only this manual invocation may retry UNKNOWN.

    The participation gate, individual action guards, and audit attempts remain
    authoritative.  No text is supplied here: the regular text resolver runs.
    """
    if not expected_profile_id or get_runtime_profile_id() != expected_profile_id:
        raise RuntimeError("运行中的 Profile 与点击时不一致，已取消重新参与")
    if not expected_uid or str(require_login()[1]) != expected_uid:
        raise RuntimeError("登录账号与点击时不一致，已取消重新参与")
    if is_risk_paused():
        raise RuntimeError("平台风控暂停期间不能重新参与")
    from src.participation import participate_activity

    def guarded_step(step: int, total: int, message: str, action: str) -> None:
        if get_runtime_profile_id() != expected_profile_id:
            raise RuntimeError("运行中的 Profile 已变化，已停止重新参与")
        if str(require_login()[1]) != expected_uid:
            raise RuntimeError("登录账号已变化，已停止重新参与")
        if is_risk_paused():
            raise RuntimeError("平台风控暂停期间不能重新参与")
        if on_step is not None:
            on_step(step, total, message, action)

    return participate_activity(
        client, dynamic_id=dynamic_id, lottery_type=lottery_type,
        preflight=True, on_step=guarded_step, manual_unknown_retry=True,
    )


def retry_unknown_action(
    dynamic_id: str,
    action: str,
    *,
    expected_generation: int,
    expected_profile_id: str,
    retry_text: str = "",
    client: BilibiliClient | None = None,
) -> dict[str, Any]:
    if not is_valid_dynamic_id(dynamic_id) or action not in _LABELS:
        raise ValueError("人工重试目标或动作无效")
    if not expected_profile_id or get_runtime_profile_id() != expected_profile_id:
        raise RuntimeError("运行中的 Profile 与确认时不一致，已取消人工重试")
    if action in {"comment", "repost"} and (
        not isinstance(retry_text, str) or not retry_text.strip() or len(retry_text.strip()) > 233
    ):
        raise ValueError("人工重试必须明确填写本次评论或转发文字（最多 233 字）")
    if action == "favorite" and retry_text:
        raise ValueError("收藏人工重试不能附带其它动作内容")
    uid = str(require_login()[1])
    with participation_gate(uid, dynamic_id):
        if is_risk_paused():
            raise RuntimeError("平台风控暂停期间不能人工重试")
        if action == "comment" and is_comment_breaker_active():
            raise RuntimeError("评论发送暂停期间不能人工重试")
        local = get_participation(dynamic_id, uid=uid)
        if local is not None and local.user_status == "已参加":
            raise RuntimeError("当前活动已有明确参与成功记录，禁止重复操作")
        statuses = _statuses(uid, dynamic_id)
        if statuses[action] not in {"pending", "unknown"}:
            raise RuntimeError("目标动作已不处于结果未知状态")
        # This is a dedicated single-action path: no full participation loop,
        # and no like/follow or other previously successful mutation is sent.
        manager = nullcontext(client) if client is not None else BilibiliClient(warmup=False)
        with manager as active_client:
            reader = ParticipationReadClient(active_client)
            detail = fetch_dynamic_detail(reader, dynamic_id)
            reader.raise_if_failed()
            if not isinstance(detail, dict):
                raise RuntimeError("目标动态无法可靠读取，未发送人工重试")
            actual_id = detail.get("id_str") or detail.get("id")
            if actual_id is not None and str(actual_id) != dynamic_id:
                raise RuntimeError("目标动态身份不匹配，未发送人工重试")
            basic = detail.get("basic") or {}
            rid = str(basic.get("comment_id_str") or dynamic_id)
            comment_type = int(basic.get("comment_type") or 17)
            if action == "comment" and (not rid.isascii() or not rid.isdigit()
                                        or int(rid) <= 0 or comment_type <= 0):
                raise RuntimeError("评论目标身份无效，未发送人工重试")
            csrf, current_uid = require_login()
            if (str(current_uid) != uid or get_runtime_profile_id() != expected_profile_id
                    or is_risk_paused()):
                raise RuntimeError("账号、Profile 或风控状态已变化，未发送人工重试")
            breaker = current_participation_risk_breaker()
            if breaker is not None:
                breaker.check()
            attempt_id = begin_attempt(
                uid, dynamic_id, action, expected_generation=expected_generation,
                prior_status=statuses[action],
            )
            try:
                if action == "favorite":
                    result = favorite_dynamic(
                        active_client, dynamic_id=dynamic_id, csrf=csrf,
                        referer=opus_link(dynamic_id), manual_retry_attempt=attempt_id,
                    )
                elif action == "repost":
                    result = repost_dynamic(
                        active_client, dynamic_id=dynamic_id, my_uid=current_uid,
                        csrf=csrf, referer=opus_link(dynamic_id),
                        content=retry_text.strip(),
                        manual_retry_attempt=attempt_id,
                    )
                else:
                    result = comment_dynamic(
                        active_client, rid=rid, comment_type=comment_type,
                        message=retry_text.strip(), csrf=csrf,
                        referer=opus_link(dynamic_id), dynamic_id=dynamic_id,
                        uid=uid, manual_retry_attempt=attempt_id,
                    )
            except Exception as exc:
                # A process crash leaves this attempt pending; the original
                # guard remains pending/unknown and automatic work stays blocked.
                status = _statuses(uid, dynamic_id)[action]
                finish_attempt(
                    uid, dynamic_id, action, attempt_id,
                    "confirmed" if status == "confirmed" else "rejected" if status == "failed" else "unknown",
                )
                if risk_code_of(exc) is not None and breaker is not None:
                    breaker.trigger(risk_code_of(exc))
                raise
            status = _statuses(uid, dynamic_id)[action]
            outcome = "confirmed" if status == "confirmed" else "rejected" if status == "failed" else "unknown"
            finish_attempt(uid, dynamic_id, action, attempt_id, outcome)
            return {
                "dynamic_id": dynamic_id, "action": action, "status": outcome,
                "ok": outcome == "confirmed", "message": (
                    f"{_LABELS[action]}已确认成功" if outcome == "confirmed"
                    else f"{_LABELS[action]}结果仍未知，防重复保护继续生效"
                ),
                "detail": result.detail,
            }
