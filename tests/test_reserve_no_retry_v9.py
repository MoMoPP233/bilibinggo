"""Reserve mutation 禁止自动重试（V9）：timeout / 5xx / transport unknown → 恰好 1 次 POST。

A. 正常成功只调用一次 POST
B. timeout：调用次数 = 1
C. 5xx / transport unknown：调用次数 = 1
D. 确认传入 retries=0
E. 其它只读 API 的 retry 行为不受影响（post_json/get_json 默认 retries=3 不变）
"""

from __future__ import annotations

import inspect
from unittest.mock import patch

import httpx
import pytest

from src.bilibili_client import BilibiliClient
from src.participation import _reserve_click


class _RetryAwareClient:
    """按真实 post_json retry 语义复刻：transient → retries+1 次尝试上限。"""

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls: list[dict] = []
        self.last_retries: int | None = None

    def post_json(self, url, payload, *, params=None, referer=None, retries=3, raise_on_code=True):
        self.calls.append({"url": url, "payload": dict(payload)})
        self.last_retries = retries
        for attempt in range(retries + 1):
            try:
                return self._emit()
            except (httpx.RequestError, httpx.HTTPStatusError) as exc:
                if attempt < retries:
                    continue
                if isinstance(exc, httpx.RequestError):
                    raise RuntimeError(f"网络请求失败: {exc}") from exc
                raise

    def _emit(self):
        outcome = self.outcome
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _run(outcome) -> _RetryAwareClient:
    client = _RetryAwareClient(outcome)
    with patch("src.participation.require_login", return_value=("csrf", 1)):
        try:
            _reserve_click(
                client,
                dynamic_id="100000000000000001",
                reserve_id=12345,
                reserve_total=100,
                button_status=1,
                dry_run=False,
            )
        except (RuntimeError, httpx.HTTPStatusError):
            pass
    return client


def test_a_reserve_success_calls_post_once() -> None:
    client = _run({"code": 0, "data": {"final_btn_status": 1, "toast": "预约成功"}})
    assert len(client.calls) == 1


def test_b_reserve_timeout_calls_post_exactly_once() -> None:
    client = _run(httpx.TimeoutException("timeout", request=None))
    assert len(client.calls) == 1


def test_c_reserve_5xx_calls_post_exactly_once() -> None:
    request = httpx.Request("POST", "https://api.bilibili.com/x/click/reserve")
    client = _run(httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request)))
    assert len(client.calls) == 1


def test_c2_reserve_httpx_raises_when_request_lost() -> None:
    request = httpx.Request("POST", "https://api.bilibili.com/x/click/reserve")
    outcome = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    client = _RetryAwareClient(outcome)
    with patch("src.participation.require_login", return_value=("csrf", 1)):
        with pytest.raises(httpx.HTTPStatusError):
            _reserve_click(
                client,
                dynamic_id="100000000000000001",
                reserve_id=12345,
                reserve_total=100,
                button_status=1,
                dry_run=False,
            )
    assert len(client.calls) == 1


def test_d_reserve_passes_retries_zero() -> None:
    client = _run({"code": 0, "data": {"final_btn_status": 1, "toast": "预约成功"}})
    assert client.last_retries == 0


def test_e_read_apis_keep_default_retries() -> None:
    signature = inspect.signature(BilibiliClient.post_json)
    assert signature.parameters["retries"].default == 3
    signature_get = inspect.signature(BilibiliClient.get_json)
    assert signature_get.parameters["retries"].default == 3
