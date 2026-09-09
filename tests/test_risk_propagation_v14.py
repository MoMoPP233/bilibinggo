"""Risk propagation V14：watch_user_profile 不再重试正式平台风控；-799 语义明确为 temporary。

A. -352 → 请求次数=1 且向上传播
B. -509 → 请求次数=1 且向上传播
C. HTTP429 → 请求次数=1 且向上传播
D. transport timeout → 保持原行为（不吞、不本地重试）
E. transient 5xx → 保持原行为
F. 正式 risk 不被普通失败吞掉
G. -799 仍可有限只读重试（temporary restriction，非 platform risk）
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import httpx
import pytest

from src.platform_risk import is_platform_risk_code
from src.watch_user_profile import resolve_watch_user_name


class _FakeWatchClient:
    def __init__(self, outcomes: list):
        self.outcomes = list(outcomes)
        self.calls = 0

    def request_json(self, url: str, params: dict | None = None, *, referer: str | None = None) -> dict:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _payload(code: int, message: str = "") -> dict:
    return {"code": code, "message": message, "data": {}}


def _status_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://api.bilibili.com/x/space/acc/info")
    return httpx.HTTPStatusError(str(status), request=request, response=httpx.Response(status, request=request))


def test_a_minus_352_single_request_and_propagates() -> None:
    client = _FakeWatchClient([_payload(-352, "风控")])
    with pytest.raises(RuntimeError, match="API error -352"):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_b_minus_509_single_request_and_propagates() -> None:
    client = _FakeWatchClient([_payload(-509, "拦截")])
    with pytest.raises(RuntimeError, match="API error -509"):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_c_http_429_single_request_and_propagates() -> None:
    client = _FakeWatchClient([_status_error(429)])
    with pytest.raises(httpx.HTTPStatusError):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_d_transport_timeout_propagates_unswallowed() -> None:
    exc = httpx.TimeoutException("timeout", request=httpx.Request("GET", "https://api.bilibili.com/x"))
    client = _FakeWatchClient([exc])
    with pytest.raises(httpx.TimeoutException):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_e_transient_5xx_propagates_unswallowed() -> None:
    client = _FakeWatchClient([_status_error(500)])
    with pytest.raises(httpx.HTTPStatusError):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_f_formal_risk_never_falls_back_to_mid_silently() -> None:
    # 连续两个端点都返回 -352：第一次即抛，绝不静默降级为 MID 字符串
    client = _FakeWatchClient([_payload(-352, "风控"), _payload(-352, "风控")])
    with pytest.raises(RuntimeError, match="API error -352"):
        resolve_watch_user_name(client, 123)
    assert client.calls == 1


def test_g_minus_799_still_allows_limited_readonly_retry(monkeypatch) -> None:
    client = _FakeWatchClient(
        [_payload(-799, "访问过于频繁"), {"code": 0, "data": {"name": "u"}}]
    )
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    assert resolve_watch_user_name(client, 123) == "u"
    assert client.calls == 2


@pytest.mark.parametrize(
    ("code", "expected"),
    [(-352, True), (-509, True), (429, True), (-799, False), (-404, False), (0, False), ("-352", True), (None, False)],
)
def test_is_platform_risk_code_helper(code, expected) -> None:
    assert is_platform_risk_code(code) is expected
