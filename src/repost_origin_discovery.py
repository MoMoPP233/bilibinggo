"""One-level repost-origin discovery shared by Following Feed and DS9.

Only the immediate ``orig`` ID is discovered here. Classification and any
detail read for that ID remain in the existing activity pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import httpx

from src.bilibili_client import BilibiliClient
from src.bilibili_request_budget import RequestBudgetExhausted
from src.dead_links import is_dynamic_detail_permanently_gone
from src.lottery_api import DYNAMIC_DETAIL_URL
from src.platform_risk import risk_code_of
from src.sources.common import is_valid_dynamic_id, opus_link


@dataclass(frozen=True)
class OriginDiscovery:
    is_forward: bool
    original_id: str | None = None
    retryable: bool = False
    reason: str = ""


def _origin_id(item: dict[str, Any]) -> str | None:
    desc = item.get("desc") if isinstance(item.get("desc"), dict) else {}
    origin = item.get("orig")
    if not isinstance(origin, dict):
        origin = desc.get("orig")
    if not isinstance(origin, dict):
        return None
    origin_desc = origin.get("desc") if isinstance(origin.get("desc"), dict) else {}
    raw = origin.get("id_str") or origin.get("dynamic_id") or origin_desc.get("dynamic_id")
    value = str(raw or "").strip()
    return value if is_valid_dynamic_id(value) else None


def is_forward_item(item: dict[str, Any] | None) -> bool:
    if not isinstance(item, dict):
        return False
    desc = item.get("desc") if isinstance(item.get("desc"), dict) else {}
    kind = str(item.get("type") or desc.get("type") or "")
    return kind in {"1", "DYNAMIC_TYPE_FORWARD"} or isinstance(item.get("orig"), dict) or isinstance(desc.get("orig"), dict)


def discover_origin(
    item: dict[str, Any] | None,
    outer_id: str,
    *,
    client: BilibiliClient | None = None,
    cache: dict[str, OriginDiscovery] | None = None,
) -> OriginDiscovery:
    """Return one immediate original; one zero-retry detail read only if needed.

    A missing/invalid embedded ID cannot be interpreted as "no original" until
    the existing dynamic/detail endpoint has been checked. Remote uncertainty is
    explicitly retryable; a missing orig in a successful detail is terminal.
    """
    if not is_forward_item(item):
        return OriginDiscovery(False)
    embedded = _origin_id(item or {})
    if embedded:
        return OriginDiscovery(True, embedded)
    if cache is not None and outer_id in cache:
        return cache[outer_id]
    if client is None:
        return OriginDiscovery(True, retryable=True, reason="origin_detail_pending")

    try:
        payload = client.request_json(
            DYNAMIC_DETAIL_URL,
            {"id": outer_id},
            referer=opus_link(outer_id),
            retries=0,
        )
    except RequestBudgetExhausted:
        raise
    except httpx.HTTPStatusError as exc:
        if risk_code_of(exc) is not None:
            raise
        result = OriginDiscovery(True, reason="origin_deleted") if exc.response.status_code == 404 else OriginDiscovery(True, retryable=True, reason="origin_detail_http_failure")
    except (httpx.RequestError, RuntimeError) as exc:
        if risk_code_of(exc) is not None:
            raise
        if isinstance(exc, RuntimeError) and not (
            isinstance(exc.__cause__, (httpx.RequestError, json.JSONDecodeError))
            or str(exc).startswith(("网络请求失败:", "响应不是有效 JSON"))
        ):
            raise
        result = OriginDiscovery(True, retryable=True, reason="origin_detail_network_failure")
    else:
        if not isinstance(payload, dict) or type(payload.get("code")) is not int:
            raise ValueError("dynamic/detail 原动态补读响应无效")
        code = payload["code"]
        message = str(payload.get("message") or "")
        if code in (-352, -509):
            raise RuntimeError(f"API error {code}: 原动态补读命中平台风控")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        detail = data.get("item") if isinstance(data.get("item"), dict) else None
        if is_dynamic_detail_permanently_gone(item=detail, code=code, message=message):
            result = OriginDiscovery(True, reason="origin_deleted")
        elif code != 0:
            result = OriginDiscovery(True, retryable=True, reason=f"origin_detail_code_{code}")
        elif detail is None:
            result = OriginDiscovery(True, retryable=True, reason="origin_detail_missing_item")
        else:
            resolved = _origin_id(detail)
            result = OriginDiscovery(True, resolved, reason="" if resolved else "origin_unavailable")
    if cache is not None:
        cache[outer_id] = result
    return result
