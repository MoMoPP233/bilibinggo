from __future__ import annotations

import time

from src.bilibili_client import BilibiliClient, api_code
from src.platform_risk import is_platform_risk_code

SPACE_ACC_INFO_URL = "https://api.bilibili.com/x/space/acc/info"
SPACE_CARD_URL = "https://api.bilibili.com/x/web-interface/card"
# 仅 -799（临时反爬/频率受限）：不是正式 platform risk，允许有限只读重试。
# 正式平台风控（-352/-509/HTTP429）绝不在此层重试，立即上抛。
_RETRY_CODES = frozenset({-799})


def _extract_name(payload: dict) -> str:
    data = payload.get("data") or {}
    if not isinstance(data, dict):
        return ""
    card = data.get("card") or {}
    if not isinstance(card, dict):
        card = {}
    for candidate in (data.get("name"), card.get("name")):
        name = str(candidate or "").strip()
        if name:
            return name
    return ""


def resolve_watch_user_name(client: BilibiliClient, mid: int) -> str:
    """从 B 站拉取用户空间昵称；失败时回退为 MID 字符串，保证添加流程可用。"""
    if mid <= 0:
        raise ValueError("MID 无效")

    referer = f"https://space.bilibili.com/{mid}"
    endpoints = (
        (SPACE_ACC_INFO_URL, {"mid": mid}),
        (SPACE_CARD_URL, {"mid": mid, "photo": 0}),
    )
    last_message = ""

    for url, params in endpoints:
        for attempt in range(3):
            payload = client.request_json(url, params=params, referer=referer)
            code = api_code(payload)
            if is_platform_risk_code(code):
                # 正式平台风控（-352/-509/429）：请求次数=1，立即向上传播（不吞、不重试）
                message = str(payload.get("message") or payload.get("msg") or code)
                raise RuntimeError(f"API error {code}: {message}".rstrip())
            if code == 0:
                name = _extract_name(payload)
                if name:
                    return name
                last_message = "接口未返回昵称"
                break
            message = str(payload.get("message") or payload.get("msg") or code)
            last_message = message
            if code in _RETRY_CODES and attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            break

    return str(mid)
