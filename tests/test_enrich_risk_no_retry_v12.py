"""Read-only enrich 链路的 structured risk 0-retry（V12）。

A. 明确 -352：总请求（client 构造）次数 = 1
B. 明确 -509：总请求次数 = 1
C. HTTP 429：总请求次数 = 1
D. transport timeout：保持既有有限只读 retry（ENRICH_RETRY_ATTEMPTS 次）
E. transient 5xx：保持既有设计
F. risk pause/cooldown 语义不变（识别入口仍是 platform_risk，0 retry 即上报上游）
"""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

import src.fetch_activity_info as fetch


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_enrich(exc_to_raise: Exception) -> int:
    constructs = {"n": 0}

    def fake_factory(*args, **kwargs):
        constructs["n"] += 1
        return _FakeClient()

    def boom(client, dynamic_id: str) -> bool:
        raise exc_to_raise

    with patch.object(fetch, "BilibiliClient", side_effect=fake_factory), patch.object(
        fetch, "is_dynamic_deleted", side_effect=boom
    ):
        try:
            fetch._enrich_one("100000000000000001", "lottery", participation=None)
        except Exception:
            pass
    return constructs["n"]


def test_a_minus_352_never_retried() -> None:
    assert _run_enrich(RuntimeError("API error -352: 风控校验失败")) == 1


def test_b_minus_509_never_retried() -> None:
    assert _run_enrich(RuntimeError("API error -509: 请求拦截")) == 1


def test_c_http_429_never_retried() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x/space/wbi/arc/search")
    exc = httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))
    assert _run_enrich(exc) == 1


def test_d_transport_timeout_keeps_finite_retry() -> None:
    exc = httpx.TimeoutException("timeout", request=httpx.Request("GET", "https://api.bilibili.com/x"))
    # ENRICH_RETRY_ATTEMPTS=3：每次尝试都会新建 client
    assert _run_enrich(exc) == fetch.ENRICH_RETRY_ATTEMPTS


def test_e_transient_500_keeps_finite_retry() -> None:
    request = httpx.Request("GET", "https://api.bilibili.com/x")
    exc = httpx.HTTPStatusError("500", request=request, response=httpx.Response(500, request=request))
    assert _run_enrich(exc) == fetch.ENRICH_RETRY_ATTEMPTS


def test_f_risk_error_propagates_for_upstream_handling() -> None:
    with patch.object(fetch, "BilibiliClient", side_effect=lambda *a, **k: _FakeClient()), patch.object(
        fetch, "is_dynamic_deleted",
        side_effect=RuntimeError("API error -352: 风控校验失败"),
    ):
        with pytest.raises(RuntimeError, match="API error -352"):
            fetch._enrich_one("1", "lottery", participation=None)
