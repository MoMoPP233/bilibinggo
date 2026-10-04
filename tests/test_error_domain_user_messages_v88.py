"""V88：离线验证 DS-1 传输错误与正式风控不会串台。"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx
import pytest

from src.bilibili_client import BilibiliClient, BilibiliNetworkError
from src.llm_client import LlmTemporaryError
from src.platform_risk import risk_code_of
from web.job_runner import JobRunner
from web.user_messages import friendly_error


def _transport_error(error_type: type[httpx.RequestError], detail: str) -> httpx.RequestError:
    return error_type(detail, request=httpx.Request("GET", "https://api.bilibili.com/x/space/arc/search"))


@pytest.mark.parametrize(
    ("error_type", "detail"),
    [
        (httpx.ConnectError, "connection refused"),
        (httpx.ConnectError, "TLS EOF"),
        (httpx.ConnectTimeout, "timed out"),
        (httpx.ProxyError, "proxy unavailable"),
    ],
)
def test_ds1_both_video_endpoints_transport_failure_is_only_network(
    monkeypatch: pytest.MonkeyPatch, error_type: type[httpx.RequestError], detail: str,
) -> None:
    client = object.__new__(BilibiliClient)
    calls: list[str] = []

    def fake_get_json(url: str, **_kwargs: object) -> dict:
        calls.append(url)
        network = _transport_error(error_type, detail)
        raise RuntimeError(f"网络请求失败: {network}") from network

    monkeypatch.setattr(client, "get_json", fake_get_json)
    with pytest.raises(BilibiliNetworkError) as caught:
        client.get_latest_video(101)
    assert len(calls) == 2
    assert caught.value.code == "ACCOUNT_NETWORK_ERROR"
    assert risk_code_of(caught.value) is None
    message = friendly_error(caught.value)
    for wrong in ("风控", "-352", "-509", "-412", "重新扫码"):
        assert wrong not in f"{caught.value} {message}"
    assert "网络" in message


@pytest.mark.parametrize("code", [-352, -509])
def test_ds1_real_api_risk_stays_formal(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    client = object.__new__(BilibiliClient)
    calls: list[str] = []

    def fake_get_json(url: str, **_kwargs: object) -> dict:
        calls.append(url)
        raise RuntimeError(f"API error {code}: 平台返回")

    monkeypatch.setattr(client, "get_json", fake_get_json)
    with pytest.raises(RuntimeError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) == code
    assert len(calls) == 1


def test_ds1_real_http_429_stays_formal(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(BilibiliClient)
    calls: list[str] = []

    def fake_get_json(url: str, **_kwargs: object) -> dict:
        calls.append(url)
        request = httpx.Request("GET", url)
        raise httpx.HTTPStatusError("429", request=request, response=httpx.Response(429, request=request))

    monkeypatch.setattr(client, "get_json", fake_get_json)
    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) == 429
    assert len(calls) == 1


def test_ds1_video_detail_html_fallback_transport_error_is_network(monkeypatch: pytest.MonkeyPatch) -> None:
    client = object.__new__(BilibiliClient)

    def fail_json(_url: str, **_kwargs: object) -> dict:
        network = _transport_error(httpx.ConnectError, "TLS EOF")
        raise RuntimeError(f"网络请求失败: {network}") from network

    def fail_html(_bvid: str, *, referer: str) -> dict:
        network = _transport_error(httpx.ConnectError, "TLS EOF")
        raise RuntimeError(f"网络请求失败: {network}") from network

    monkeypatch.setattr(client, "get_json", fail_json)
    monkeypatch.setattr(client, "_get_video_detail_from_page", fail_html)
    with pytest.raises(BilibiliNetworkError) as caught:
        client.get_video_detail("BV1")
    assert risk_code_of(caught.value) is None
    assert "扫码" not in friendly_error(caught.value)


@pytest.mark.parametrize("code", [-315, -412])
def test_ds1_nonformal_business_code_remains_nonformal(monkeypatch: pytest.MonkeyPatch, code: int) -> None:
    client = object.__new__(BilibiliClient)

    def fake_get_json(_url: str, **_kwargs: object) -> dict:
        raise RuntimeError(f"API error {code}: 平台返回")

    monkeypatch.setattr(client, "get_json", fake_get_json)
    with pytest.raises(RuntimeError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) is None
    assert "重新扫码" not in str(caught.value)


@pytest.mark.parametrize("mode", ["network", "risk"])
def test_ds1_job_result_preserves_error_domain(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    import web.job_runner as job_runner

    client = object.__new__(BilibiliClient)

    def fake_get_json(_url: str, **_kwargs: object) -> dict:
        if mode == "risk":
            raise RuntimeError("API error -352: 平台返回")
        network = _transport_error(httpx.ConnectError, "TLS EOF")
        raise RuntimeError(f"网络请求失败: {network}") from network

    monkeypatch.setattr(client, "get_json", fake_get_json)

    def fake_run_action(_action: str, _params: dict, **_kwargs: object) -> dict:
        client.get_latest_video(101)
        raise AssertionError("unreachable")

    monkeypatch.setattr(job_runner, "run_action", fake_run_action)
    runner = JobRunner()
    job_id = runner.try_start("refresh_source", {"source_id": "DS-1"})
    assert job_id is not None
    deadline = time.monotonic() + 5
    while runner.resolve_job_status(job_id).state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    terminal = runner.resolve_job_status(job_id).to_dict()
    assert terminal["state"] == "error"
    contract = json.loads((Path(__file__).parent / "fixtures" / "v88_error_job_contract.json").read_text(encoding="utf-8"))[mode]
    assert terminal["action"] == contract["action"]
    assert terminal["message"] == contract["message"]
    assert {key: terminal["result"][key] for key in contract["result"]} == contract["result"]
    if mode == "risk":
        assert terminal["result"]["risk_code"] == -352
    else:
        assert terminal["result"]["error_code"] == "ACCOUNT_NETWORK_ERROR"
        assert "网络" in terminal["message"]
        assert "重新扫码" not in terminal["message"]


def test_llm_429_message_stays_llm_temporary() -> None:
    exc = LlmTemporaryError("LLM 服务当前限流，请稍后重试（HTTP 429）")
    assert risk_code_of(exc) is None
    assert "LLM" in friendly_error(exc)
    assert "B 站" not in friendly_error(exc)
