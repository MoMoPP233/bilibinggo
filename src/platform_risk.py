"""平台风控统一判定来源（V2）。

规则（唯一来源，禁止对业务 payload / 正文做宽泛字符串搜索）：
- 结构化整数 code：-352 / -509（B 站 API 明确风控码）
- HTTP status：429（httpx.HTTPStatusError.response.status_code）
- 本项目自己生成的标准错误信封文本："API error <code>:" /
  "Bilibili API error <code>:" / "opus/detail API error <code>:"
  这类信封只来自客户端/API 层对真实返回码的包装，不会来自任意正文。

普通失败（HTTP 5xx、网络、JSON 结构、业务解析、本地 DB）一律不是风控；
正文/用户名/动态 ID 中出现 "429"、"风控"、"限流" 等字样也一律不是风控。
"""

from __future__ import annotations

import re

API_RISK_CODES = frozenset({-352, -509, 429})
RISK_HTTP_STATUSES = frozenset({429})

_API_CODE_ENVELOPE_RE = re.compile(
    r"(?:Bilibili\s+)?API error\s+(?P<code>-?\d+)\s*[:：]"
)

_OPUS_DETAIL_ENVELOPE_RE = re.compile(
    r"opus/detail API error\s+(?P<code>-?\d+)\s*[:：]"
)


def _coerce_code(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        code = int(value)
    except (TypeError, ValueError):
        return None
    return code


def _risk_code_from_text(text: str) -> int | None:
    for pattern in (_API_CODE_ENVELOPE_RE, _OPUS_DETAIL_ENVELOPE_RE):
        match = pattern.search(text)
        if not match:
            continue
        code = _coerce_code(match.group("code"))
        if code is not None and code in API_RISK_CODES:
            return code
    return None


def risk_code_of(exc_or_message: object) -> int | None:
    """返回明确平台风控码（-352/-509/429），无法可靠确认时返回 None。"""
    if exc_or_message is None:
        return None
    code = getattr(exc_or_message, "code", None)
    code = _coerce_code(code)
    if code is not None and code in API_RISK_CODES:
        return code
    response = getattr(exc_or_message, "response", None)
    status = _coerce_code(getattr(response, "status_code", None))
    if status is not None and status in RISK_HTTP_STATUSES:
        return status
    text = str(exc_or_message)
    if not text or text in ("None", ""):
        return None
    return _risk_code_from_text(text)


def matches_platform_risk(exc_or_message: object) -> bool:
    """布尔便捷判定：只认可靠结构化的平台风控，不扫描任意正文。"""
    return risk_code_of(exc_or_message) is not None
