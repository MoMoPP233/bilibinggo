from __future__ import annotations

from contextlib import nullcontext
from typing import Any

import httpx

from src.app_logging import get_logger
from src.bilibili_auth import require_login
from src.bilibili_client import BilibiliClient
from src.comment_breaker import (
    COMMENT_LIMIT_CODE,
    claim_comment_probe,
    clear_comment_breaker,
    comment_probe_due,
    is_comment_breaker_active,
    record_comment_probe_result,
)
from src.comment_confirmation import confirm_comment
from src.lottery_actions import comment_dynamic_probe
from src.participate_text import resolve_comment_rid_and_type, resolve_participate_text_for_activity

logger = get_logger("comment_probe")


def _unknown_exception(exc: BaseException) -> bool:
    return isinstance(exc, (httpx.HTTPError, TimeoutError, ConnectionError, OSError, RuntimeError))


def run_comment_recovery_probe(
    *,
    client: BilibiliClient | None = None,
    now: int | None = None,
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one low-frequency recovery comment mutation when due."""
    if not is_comment_breaker_active():
        return {"claimed": False, "reason": "inactive"}
    if not comment_probe_due(now=now):
        return {"claimed": False, "reason": "not_due"}
    if candidate is None:
        from web.activity_service import pick_comment_probe_candidate

        candidate = pick_comment_probe_candidate()
    if not candidate:
        return {"claimed": False, "reason": "no_candidate"}
    dynamic_id = str(candidate.get("dynamic_id") or "").strip()
    if not dynamic_id:
        return {"claimed": False, "reason": "no_candidate"}

    manager = nullcontext(client) if client is not None else BilibiliClient(warmup=False)
    with manager as active_client:
        csrf, uid = require_login()
        rid, comment_type, referer = resolve_comment_rid_and_type(active_client, dynamic_id)
        resolution = resolve_participate_text_for_activity(active_client, dynamic_id=dynamic_id)
        claim = claim_comment_probe(dynamic_id, now=now)
        if claim is None:
            return {"claimed": False, "reason": "not_due_or_lost_claim"}
        logger.info("评论探测：尝试1条")
        try:
            result = comment_dynamic_probe(
                active_client,
                rid=rid,
                comment_type=comment_type,
                message=resolution.text,
                csrf=csrf,
                referer=referer,
                dynamic_id=dynamic_id,
                uid=uid,
            )
        except Exception as exc:
            if _unknown_exception(exc):
                record_comment_probe_result("unknown", now=now)
                logger.info("评论探测：结果未知，1小时后换一条再试")
                return {"claimed": True, "result": "unknown", "dynamic_id": dynamic_id}
            record_comment_probe_result("inconclusive", now=now)
            logger.info("评论探测：本条无法判断，1小时后换一条再试")
            return {"claimed": True, "result": "inconclusive", "dynamic_id": dynamic_id}

        if result.detail == "恢复探测已取消":
            return {"claimed": True, "result": "cancelled", "dynamic_id": dynamic_id}
        if result.ok:
            confirm_comment(dynamic_id, uid=uid)
            record_comment_probe_result("success", now=now)
            clear_comment_breaker()
            logger.info("评论探测：已恢复，等待下一轮参与")
            return {"claimed": True, "result": "success", "dynamic_id": dynamic_id}
        if result.detail.startswith(f"code={COMMENT_LIMIT_CODE}"):
            record_comment_probe_result("12014", now=now, last_seen=True)
            logger.info("评论探测：仍受限，1小时后再次探测")
            return {"claimed": True, "result": "12014", "dynamic_id": dynamic_id}
        record_comment_probe_result("inconclusive", now=now)
        logger.info("评论探测：本条无法判断，1小时后换一条再试")
        return {"claimed": True, "result": "inconclusive", "dynamic_id": dynamic_id}
