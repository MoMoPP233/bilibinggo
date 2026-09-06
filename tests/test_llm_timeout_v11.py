"""V11：LLM 请求 timeout 层级一致性（全 fake，V6 guard 生效，零真实 Provider）。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.llm_client as llm  # noqa: E402
from web import auto_remote_state as state_mod  # noqa: E402


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeHttp:
    """可抛 timeout/error 的假 httpx.Client（记录调用次数与退出）。"""

    def __init__(self, *, error: BaseException | None = None):
        self.error = error
        self.calls = 0
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True
        return False

    def post(self, url, headers=None, json=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return _FakeResponse({"choices": [{"message": {"content": '{"ok": true}'}}]})


def _config() -> SimpleNamespace:
    return SimpleNamespace(base_url="http://127.0.0.1:9", model_name="fake-model", api_key="sk-test")


def test_timeout_constructor_splits_connect_and_read() -> None:
    assert llm._LLM_TIMEOUT.connect == 12.0
    assert llm._LLM_TIMEOUT.read == 90.0


@pytest.mark.parametrize(
    "exc",
    [
        httpx.ReadTimeout("read", request=None),
        httpx.ConnectTimeout("connect", request=None),
    ],
)
def test_timeout_is_not_auto_retried(exc) -> None:
    fake = _FakeHttp(error=exc)
    with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
        with pytest.raises(RuntimeError):
            llm.test_llm_connection(_config())
    assert fake.calls == 1  # 单次尝试，绝不静默重试昂贵 LLM 调用


def test_connect_failure_fails_fast_single_attempt() -> None:
    fake = _FakeHttp(error=httpx.ConnectTimeout("connect refused", request=None))
    with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
        with pytest.raises(RuntimeError):
            llm.test_llm_connection(_config())
    assert fake.calls == 1
    assert fake.closed is True  # 同步 httpx 客户端随 ctx 退出关闭底层 transport


def test_success_before_timeout_returns_label() -> None:
    fake = _FakeHttp()
    with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
        result = llm.test_llm_connection(_config())
    assert "fake-model" in result
    assert fake.calls == 1


def test_provider_transient_status_retry_behavior_unchanged() -> None:
    # 非 timeout 的 5xx/429 保持既有重试条件（不新增/不删除原语义）。
    request = httpx.Request("POST", "http://llm/chat/completions")
    err = httpx.HTTPStatusError(
        "503", request=request, response=httpx.Response(503, request=request)
    )
    assert llm._llm_retryable(err) is True
    assert llm._llm_retryable(httpx.ReadTimeout("t", request=None)) is False


def test_llm_timeout_never_touches_platform_risk() -> None:
    # LLM 层不引用 platform_risk；cooldown 记录函数若被意外调用则测试失败。
    with patch.object(
        state_mod, "record_auto_remote_risk", side_effect=AssertionError("LLM 不应触发 Bilibili cooldown")
    ):
        fake = _FakeHttp(error=httpx.ReadTimeout("read", request=None))
        with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
            with pytest.raises(RuntimeError):
                llm.test_llm_connection(_config())
    assert state_mod.matches_platform_risk("LLM request timed out after 90s") is False
    assert state_mod.matches_platform_risk("LLM provider HTTP 429") is False


def test_backend_own_timeout_error_is_explicit() -> None:
    fake = _FakeHttp(error=httpx.ReadTimeout("read timeout after 90 seconds", request=None))
    with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
        with pytest.raises(RuntimeError, match="无法连接"):
            llm.test_llm_connection(_config())


# ---------- V11 闭合：test connection 单次 attempt vs 正式业务 retry ----------


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://llm/chat/completions")
    response = httpx.Response(status, request=request, json={"message": "provider error"})
    return httpx.HTTPStatusError("status", request=request, response=response)


class _RaisingResponse:
    def __init__(self, exc):
        self._exc = exc

    def raise_for_status(self):
        raise self._exc

    def json(self):
        return {}


def _fake_client_raising(exc):
    fake = _FakeHttp()

    class Client:
        def __init__(self, **kwargs):
            self.closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            fake.closed = True
            return False

        def post(self, url, headers=None, json=None):
            fake.calls += 1
            return _RaisingResponse(exc)

    return fake, Client


@pytest.mark.parametrize(
    "exc",
    [
        _http_status_error(500),
        _http_status_error(429),
        _http_status_error(503),
    ],
)
def test_test_connection_http_errors_single_attempt(exc) -> None:
    fake, Client = _fake_client_raising(exc)
    with patch.object(llm.httpx, "Client", Client):
        with pytest.raises(RuntimeError):
            llm.test_llm_connection(_config())
    assert fake.calls == 1


def test_test_connection_invalid_response_is_single_attempt() -> None:
    # 测试连接只看连接/HTTP 结果；即使 provider 返回不可用响应也只尝试一次。
    fake = _FakeHttp()
    real_response_payload = {"choices": [{"message": {"content": "not-json-{broken"}}]}

    def post(url, headers=None, json=None):
        fake.calls += 1
        return _FakeResponse(real_response_payload)

    fake.post = post
    with patch.object(llm.httpx, "Client", lambda **kwargs: fake):
        result = llm.test_llm_connection(_config())
    assert "fake-model" in result
    assert fake.calls == 1


def test_business_chat_json_transient_500_keeps_retry_semantics() -> None:
    exc = _http_status_error(500)
    fake, Client = _fake_client_raising(exc)
    with patch.object(llm.httpx, "Client", Client):
        with pytest.raises(RuntimeError):
            llm.chat_json(system="s", user="u", config=_config())
    assert fake.calls == 3  # 正式业务非 timeout transient 仍为既有 attempts=3


def test_business_chat_json_timeout_still_no_retry() -> None:
    exc = httpx.ReadTimeout("read", request=None)
    fake, Client = _fake_client_raising(exc)
    with patch.object(llm.httpx, "Client", Client):
        with pytest.raises(RuntimeError):
            llm.chat_json(system="s", user="u", config=_config())
    assert fake.calls == 1


# ---------- V11 总 deadline（async TOTAL，非 read 别名） ----------


class _FakeAsyncClient:
    def __init__(self, mode: str = "pending"):
        self.mode = mode
        self.calls = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True
        return False

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        if self.mode == "pending":
            await asyncio.Event().wait()
        elif self.mode == "slow-drip":
            for _ in range(200):
                await asyncio.sleep(0.02)  # 持续“有进展”，总耗时远超 deadline
        raise AssertionError("unexpected return path")


import asyncio  # noqa: E402


def _run_async_total_test(mode: str) -> tuple[_FakeAsyncClient, float]:
    fake = _FakeAsyncClient(mode=mode)
    started = time.monotonic()

    def run() -> None:
        import asyncio as _asyncio

        async def main() -> None:
            with patch.object(llm.httpx, "AsyncClient", lambda **kwargs: fake):
                try:
                    await llm.test_llm_connection_async(_config(), total_timeout_sec=0.1)
                except RuntimeError:
                    return
                raise AssertionError("TOTAL deadline 未触发")

        _asyncio.run(main())

    import threading

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=5)
    if thread.is_alive():
        raise AssertionError("async deadline 任务未退出")
    return fake, time.monotonic() - started


def test_total_deadline_terminates_pending_provider_request() -> None:
    import time as _time

    fake, elapsed = _run_async_total_test("pending")
    assert fake.calls == 1
    assert fake.closed is True  # AsyncClient context 已关闭
    assert elapsed < 3.0  # ≈0.1s deadline，非真实等待


def test_total_deadline_not_read_timeout_alias_with_slow_progress() -> None:
    fake, elapsed = _run_async_total_test("slow-drip")
    assert fake.calls == 1
    assert fake.closed is True
    assert elapsed < 3.0
    assert fake.closed and elapsed > 0  # 触发的是 TOTAL deadline，而非单次 read timeout
