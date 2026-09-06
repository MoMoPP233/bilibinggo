from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import httpx

from src.llm_config import LlmConfig, load_llm_config
from src.retry_utils import is_transient_http_error, retry_call

import threading

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


def _format_llm_http_error(exc: httpx.HTTPStatusError) -> str:
    status = exc.response.status_code
    message = ""
    try:
        body = exc.response.json()
        if isinstance(body.get("error"), dict):
            message = str(body["error"].get("message") or "").strip()
        else:
            message = str(body.get("message") or body.get("detail") or "").strip()
    except Exception:
        message = (exc.response.text or "").strip()[:200]
    if status == 401:
        return f"API Key 无效或已过期{('：' + message) if message else ''}"
    if status == 404:
        return f"接口地址或模型不存在{('：' + message) if message else ''}"
    if status in {408, 429, 500, 502, 503, 504}:
        return f"LLM 服务暂时不可用（HTTP {status}）{('：' + message) if message else ''}"
    return f"连接失败（HTTP {status}）{('：' + message) if message else ''}"


def _llm_retryable(exc: BaseException) -> bool:
    # 超时（connect/read/pool）绝不自动重试：避免对昂贵 LLM 调用静默重复。
    # 普通网络错误按原有语义判断；HTTP 5xx/429 等瞬时错误维持既有重试。
    if isinstance(exc, httpx.TimeoutException):
        return False
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
    try:
        # 连接测试=单次尝试（兼容旧调用方；真正总窗口由 async 路由保证）。
        _post_chat_completion(url=url, headers=headers, payload=payload, attempts=1)
    except httpx.HTTPStatusError as exc:
        raise RuntimeError(_format_llm_http_error(exc)) from exc
    except httpx.RequestError as exc:
        raise RuntimeError(f"无法连接接口地址：{exc}") from exc
    return f"{config.model_name} @ {config.base_url}"


def chat_json(*, system: str, user: str, config: LlmConfig | None = None) -> dict[str, Any]:
    cfg = config or load_llm_config()
    if not cfg:
        raise RuntimeError("未配置 LLM，请检查数据根目录下的 shared/llm.env")

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
            raise RuntimeError(_format_llm_http_error(exc)) from exc
        except httpx.RequestError as exc:
            raise RuntimeError(f"无法连接 LLM 接口：{exc}") from exc

    content = (
        ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    ).strip()

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
        raise RuntimeError(f"LLM 返回无法解析为 JSON: {text[:200]}")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise RuntimeError("LLM 返回的 JSON 不是对象")
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
        return RuntimeError(_format_llm_http_error(exc))
    if isinstance(exc, (httpx.RequestError, TimeoutError, asyncio.TimeoutError)):
        return RuntimeError(f"无法连接接口地址：{exc}")
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
    try:
        async with asyncio.timeout(total_timeout_sec):
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(
                    _LLM_READ_TIMEOUT_SEC, connect=_LLM_CONNECT_TIMEOUT_SEC
                )
            ) as client:
                resp = await client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
    except (httpx.HTTPStatusError, httpx.RequestError, TimeoutError) as exc:
        raise _map_provider_error(exc) from exc
    return f"{config.model_name} @ {config.base_url}"
