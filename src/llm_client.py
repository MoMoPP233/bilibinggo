from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from src.llm_config import LlmConfig, load_llm_config
from src.retry_utils import is_transient_http_error, retry_call

import threading


class LlmConfigError(RuntimeError):
    """LLM 配置/鉴权失败（401/403/404、model/endpoint 不存在等）——不重试。"""


class LlmConfigUnavailableError(LlmConfigError):
    """配置/鉴权已明确失败且 fingerprint 未变：跳过重复远程请求（自动任务快速失败）。

    属于 recoverable configuration failure：用户修改 Host/Model/API Key 后
    fingerprint 改变即自动恢复；数据库状态仍不得保存成业务 final。
    """


class LlmTemporaryError(RuntimeError):
    """LLM 临时 provider 故障（429/5xx/timeout/连接中断）——可有限重试/retryable。"""


class LlmProtocolError(RuntimeError):
    """LLM 协议不兼容（HTML/空响应/error object/缺 choices/content 非法等）——不可静默成空。"""


_llm_lock = threading.Semaphore(3)
# 生成/读取允许较长窗口；连接（connect）快速失败，不共享同一长超时。
_LLM_READ_TIMEOUT_SEC = 90.0
_LLM_CONNECT_TIMEOUT_SEC = 12.0
_LLM_TIMEOUT = httpx.Timeout(
    _LLM_READ_TIMEOUT_SEC, connect=_LLM_CONNECT_TIMEOUT_SEC
)
_LLM_RETRY_ATTEMPTS = 3
# /api/settings/llm/test 的 backend TOTAL wall-clock deadline（覆盖整个单次
# provider 交互；read=90 只是单次 socket read 语义，不能当作总预算）。
_LLM_TEST_TOTAL_TIMEOUT_SEC = 90.0
# 连接测试只需极短回复，但 DeepSeek V4 等模型在部分网关下 max_tokens=1 会触发 400。
_LLM_TEST_MAX_TOKENS = 64

# 配置/鉴权失败后的最小状态：只保存脱敏 fingerprint（key|base|model 的不可逆 hash），
# 绝不保存 API Key 原文。进程内生效；配置变更（fingerprint 改变）后下一次任务自动恢复。
_unavailable_config_fingerprint: str | None = None


def _llm_config_fingerprint(config: LlmConfig) -> str:
    from src.llm_settings import _config_fingerprint

    return _config_fingerprint(config.api_key, config.base_url, config.model_name)


def _raise_if_llm_config_unavailable(config: LlmConfig) -> str:
    fingerprint = _llm_config_fingerprint(config)
    if _unavailable_config_fingerprint == fingerprint:
        raise LlmConfigUnavailableError(
            "LLM 配置/鉴权不可用且配置未变更（llm_configuration_unavailable），已跳过重复请求"
        )
    return fingerprint


def _mark_llm_config_unavailable(fingerprint: str) -> None:
    global _unavailable_config_fingerprint
    _unavailable_config_fingerprint = fingerprint


def _clear_llm_config_unavailable(fingerprint: str) -> None:
    global _unavailable_config_fingerprint
    if _unavailable_config_fingerprint == fingerprint:
        _unavailable_config_fingerprint = None


def llm_config_unavailable() -> bool:
    return _unavailable_config_fingerprint is not None


def reset_llm_config_unavailable_for_tests() -> None:
    global _unavailable_config_fingerprint
    _unavailable_config_fingerprint = None


def _format_llm_http_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    if status in {401, 403}:
        return "API Key 无效或权限不足"
    if status == 404:
        return "接口地址或模型名称不存在"
    if status == 429:
        return "LLM 服务当前限流，请稍后重试"
    if status in {408, 500, 502, 503, 504}:
        return f"LLM 服务暂时不可用（HTTP {status}）"
    return f"LLM 请求失败（HTTP {status}）"


def _map_http_error(exc: httpx.HTTPStatusError) -> RuntimeError:
    """HTTP 状态 → 结构化 LLM 错误（config/auth vs temporary provider）。"""
    status = exc.response.status_code
    message = _format_llm_http_error(exc)
    if status in {401, 403, 404}:
        return LlmConfigError(message)
    if status == 429 or 500 <= status <= 599 or status in {408}:
        return LlmTemporaryError(message)
    return LlmConfigError(message)


def _content_text(content: Any) -> str | None:
    """OpenAI-compatible content → 文本；无法安全解析返回 None（由上层抛 protocol error）。"""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str):
                parts.append(item["text"])
        text = "".join(parts).strip()
        return text or None
    return None


def _normalize_chat_response(data: Any) -> str:
    """单一 response normalization：业务请求与连接测试共用。

    支持 choices[0].message.content 为 string 或 text parts 数组；
    HTML/空/error object/缺 choices/缺 message/非法 content/reasoning-only
    一律抛 LlmProtocolError（绝不静默返回空串）。
    """
    if not isinstance(data, dict):
        raise LlmProtocolError("LLM 响应不是 JSON 对象（协议不兼容）")
    if isinstance(data.get("error"), (dict, list)):
        raise LlmProtocolError("LLM 返回了 error 对象（协议不兼容）")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmProtocolError("LLM 响应缺少 choices（协议不兼容）")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmProtocolError("LLM choices[0] 结构非法（协议不兼容）")
    message = first.get("message")
    if not isinstance(message, dict):
        raise LlmProtocolError("LLM 响应缺少 message（协议不兼容）")
    text = _content_text(message.get("content"))
    if text is None:
        raise LlmProtocolError("LLM 响应 content 为空或结构不支持（协议不兼容）")
    if text.startswith("<"):
        raise LlmProtocolError("LLM 返回内容不是 JSON（疑似 HTML，协议不兼容）")
    return text


def _llm_retryable(exc: BaseException) -> bool:
    # 超时（connect/read/pool）绝不自动重试：避免对昂贵 LLM 调用静默重复。
    # 普通网络错误按原有语义判断；HTTP 5xx/429 等瞬时错误维持既有重试。
    if isinstance(exc, httpx.TimeoutException):
        return False
    if isinstance(exc, LlmProtocolError):
        return False
    if isinstance(exc, LlmTemporaryError):
        return True
    if isinstance(exc, (json.JSONDecodeError, RuntimeError)):
        message = str(exc)
        if "无法解析出…JSON" in message or "不是对象" in message:
            return True
    return is_transient_http_error(exc)


def _supports_thinking_toggle(config: LlmConfig) -> bool:
    base = (config.base_url or "").lower()
    model = (config.model_name or "").lower()
    return "deepseek" in base or "deepseek" in model


def _build_chat_payload(
    config: LlmConfig,
    *,
    messages: list[dict[str, str]],
    temperature: float = 0,
    max_tokens: int | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": config.model_name,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens
    # DeepSeek V4 系列默认开启 thinking，结构化抽取不需要链式推理。
    if _supports_thinking_toggle(config):
        payload["thinking"] = {"type": "disabled"}
    return payload


def _post_chat_completion(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    attempts: int = _LLM_RETRY_ATTEMPTS,
) -> dict[str, Any]:
    """发起一次/多次 chat-completion。

    attempts 默认保留正式业务既有 3 次（非 timeout transient 错误）；
    「测试连接」必须 attempts=1：单次点击最多一次真实 provider 调用，
    无论 success/timeout/429/5xx/invalid 都直接返回第一次结果。
    """
    def _call() -> dict[str, Any]:
        with httpx.Client(timeout=_LLM_TIMEOUT) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    return retry_call(_call, attempts=attempts, retry_on=_llm_retryable)


def test_llm_connection(config: LlmConfig) -> str:
    url, headers, payload = _test_request(config)
    fingerprint = _llm_config_fingerprint(config)
    try:
        # 连接测试=单次尝试（兼容旧调用方；真正总窗口由 async 路由保证）。
        data = _post_chat_completion(url=url, headers=headers, payload=payload, attempts=1)
    except httpx.HTTPStatusError as exc:
        mapped = _map_http_error(exc)
        if isinstance(mapped, LlmConfigError):
            _mark_llm_config_unavailable(fingerprint)
        raise mapped from exc
    except httpx.RequestError as exc:
        raise LlmTemporaryError("LLM 服务连接超时或网络不可达") from exc
    # 必须与业务请求复用同一 normalizer：只有拿到非空文本才算真正连通。
    _normalize_chat_response(data)
    _clear_llm_config_unavailable(fingerprint)
    return f"{config.model_name} @ {config.base_url}"


def chat_json(*, system: str, user: str, config: LlmConfig | None = None) -> dict[str, Any]:
    cfg = config or load_llm_config()
    if not cfg:
        raise RuntimeError("未配置 LLM，请检查数据根目录下的 shared/llm.env")
    # 配置未变且上次已明确 Config/Auth 失败：不再重复远程请求（手动连接测试不受限）。
    fingerprint = _raise_if_llm_config_unavailable(cfg)

    payload = _build_chat_payload(
        cfg,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
    )
    headers = {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{cfg.base_url}/chat/completions"

    with _llm_lock:
        try:
            data = _post_chat_completion(url=url, headers=headers, payload=payload)
        except httpx.HTTPStatusError as exc:
            mapped = _map_http_error(exc)
            if isinstance(mapped, LlmConfigError):
                _mark_llm_config_unavailable(fingerprint)
            raise mapped from exc
        except httpx.RequestError as exc:
            raise LlmTemporaryError("LLM 服务连接超时或网络不可达") from exc

    content = _normalize_chat_response(data)
    # HTTP/协议层已证明当前配置可用：解除 fingerprint 封锁（配置变更也会自然放行）。
    _clear_llm_config_unavailable(fingerprint)

    def _parse() -> dict[str, Any]:
        return _extract_json_object(content)

    return retry_call(_parse, attempts=2, base_delay=0.5, multiplier=1.5)


def _extract_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise LlmProtocolError("LLM 返回内容不是合法 JSON（协议不兼容）")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise LlmProtocolError("LLM 返回的 JSON 不是对象（协议不兼容）")
    return parsed

def _test_request(config: LlmConfig):
    """构造测试连接请求的 (url, headers, payload)——sync/async 共用同一来源。"""
    payload = _build_chat_payload(
        config,
        messages=[{"role": "user", "content": "Reply with exactly: ok"}],
        temperature=0,
        max_tokens=_LLM_TEST_MAX_TOKENS,
    )
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }
    url = f"{config.base_url}/chat/completions"
    return url, headers, payload


def _map_provider_error(exc: BaseException) -> RuntimeError:
    if isinstance(exc, httpx.HTTPStatusError):
        return _map_http_error(exc)
    if isinstance(exc, (httpx.RequestError, TimeoutError, asyncio.TimeoutError)):
        return LlmTemporaryError("LLM 服务连接超时或网络不可达")
    return RuntimeError(str(exc))


async def test_llm_connection_async(
    config: LlmConfig,
    *,
    total_timeout_sec: float = _LLM_TEST_TOTAL_TIMEOUT_SEC,
) -> str:
    """测试连接的 async 单次实现：真正的 TOTAL wall-clock deadline。

    asyncio.timeout 包裹整个 provider 交互（AsyncClient 建立/写/读均在内）；
    deadline 触发会取消本地 async HTTP 任务并关闭 client。single attempt=1；
    远端 provider 是否立即停止生成无法保证，但本地请求生命周期必然结束。
    """
    url, headers, payload = _test_request(config)
    fingerprint = _llm_config_fingerprint(config)
    try:
        async with asyncio.timeout(total_timeout_sec):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _LLM_READ_TIMEOUT_SEC, connect=_LLM_CONNECT_TIMEOUT_SEC
                )
            ) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
    except (httpx.HTTPStatusError, httpx.RequestError, TimeoutError) as exc:
        mapped = _map_provider_error(exc)
        if isinstance(mapped, LlmConfigError):
            _mark_llm_config_unavailable(fingerprint)
        raise mapped from exc
    except ValueError as exc:
        raise LlmProtocolError("LLM 响应不是合法 JSON（协议不兼容）") from exc
    _normalize_chat_response(data)
    _clear_llm_config_unavailable(fingerprint)
    return f"{config.model_name} @ {config.base_url}"
