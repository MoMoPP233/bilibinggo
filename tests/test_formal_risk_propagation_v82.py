"""V82 formal Bilibili risk boundaries; all HTTP responses are offline fakes."""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from src import bilibili_client as bc
from src import llm_client
from src import message_api
from src import watch_feed
from src.job_store import get_job
from src.platform_risk import risk_code_of
from src.winner_scan import service as winmod
from web import auto_remote_state
from web.job_runner import JobRunner


def _response(url: str, *, code: int = 0, status: int = 200) -> httpx.Response:
    request = httpx.Request("GET", url)
    return httpx.Response(
        status,
        request=request,
        json={"code": code, "message": "平台返回", "data": {}},
    )


def _http_error(status: int = 429) -> httpx.HTTPStatusError:
    response = _response("https://api.bilibili.com/x/test", status=status)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=response.request, response=response
    )


class _MessageClient:
    def __init__(self, first: dict | Exception):
        self.first = first
        self.calls: list[str] = []

    def request_json(self, url, params=None, *, referer=None, retries=3):
        self.calls.append(url)
        if url.endswith("/at"):
            if isinstance(self.first, Exception):
                raise self.first
            return self.first
        if url.endswith("/reply"):
            return {"code": 0, "data": {"items": [], "cursor": {"is_end": True}}}
        if "get_sessions" in url:
            return {"code": 0, "data": {"session_list": []}}
        raise AssertionError(f"unexpected Bilibili request: {url}")


@pytest.fixture
def winner_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    profile = tmp_path / "winner-profile"
    profile.mkdir()
    monkeypatch.setattr(winmod, "get_profile_dir", lambda: profile)
    monkeypatch.setattr(winmod, "require_login", lambda: ("csrf", 101))
    return profile


@pytest.mark.parametrize("code", [-352, -509])
def test_message_payload_stops_winner_before_other_sources(winner_profile, code):
    client = _MessageClient({"code": code, "message": "平台返回"})
    with pytest.raises(RuntimeError) as caught:
        winmod.service.run(client, notify=False)
    assert risk_code_of(caught.value) == code
    assert len(client.calls) == 1
    assert client.calls[0].endswith("/at")


def test_message_plain_business_failure_keeps_winner_partial(winner_profile):
    client = _MessageClient({"code": -400, "message": "普通业务失败"})
    result = winmod.service.run(client, notify=False)
    assert result["partial"] is True
    assert result["sources"]["mention"]["status"] == "failed"
    assert risk_code_of(result["sources"]["mention"]["error"]) is None
    assert any(url.endswith("/reply") for url in client.calls)


def test_message_http_429_stops_winner_before_other_sources(winner_profile):
    client = _MessageClient(_http_error())
    with pytest.raises(httpx.HTTPStatusError) as caught:
        winmod.service.run(client, notify=False)
    assert risk_code_of(caught.value) == 429
    assert len(client.calls) == 1


class _SpaceClient:
    def __init__(self, outcomes: list[dict | Exception]):
        self.outcomes = list(outcomes)
        self.calls = 0

    def get_json(self, url, params=None, *, referer=None, wbi=False, retries=0):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize("code", [-352, -509])
def test_watch_payload_risk_has_one_attempt(monkeypatch, code):
    monkeypatch.setattr(watch_feed.time, "sleep", lambda *_: None)
    client = _SpaceClient([{"code": code, "message": "平台返回"}])
    with pytest.raises(RuntimeError) as caught:
        watch_feed._fetch_space_feed_page(client, mid=101, offset="")
    assert risk_code_of(caught.value) == code
    assert client.calls == 1


def test_watch_http_429_has_one_attempt(monkeypatch):
    monkeypatch.setattr(watch_feed.time, "sleep", lambda *_: None)
    client = _SpaceClient([_http_error()])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        watch_feed._fetch_space_feed_page(client, mid=101, offset="")
    assert risk_code_of(caught.value) == 429
    assert client.calls == 1


def test_watch_plain_network_error_still_retries(monkeypatch):
    monkeypatch.setattr(watch_feed.time, "sleep", lambda *_: None)
    error = httpx.ConnectError(
        "connection closed", request=httpx.Request("GET", watch_feed.SPACE_FEED_URL)
    )
    client = _SpaceClient([error, {"code": 0, "data": {"items": []}}])
    assert watch_feed._fetch_space_feed_page(client, mid=101, offset="") == {"items": []}
    assert client.calls == 2


def _client_with_http_responses(monkeypatch, responses: list[httpx.Response]):
    client = object.__new__(bc.BilibiliClient)
    client._img_key = "a" * 32
    client._sub_key = "b" * 32
    calls: list[str] = []

    def fake_get(url, *, params=None, headers=None):
        calls.append(url)
        return responses.pop(0)

    monkeypatch.setattr(client, "_http_get", fake_get)
    monkeypatch.setattr(bc.time, "sleep", lambda *_: None)
    return client, calls


def test_get_text_429_never_retries(monkeypatch):
    url = "https://www.bilibili.com/opus/123"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, status=429)])
    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_text(url, retries=3)
    assert risk_code_of(caught.value) == 429
    assert calls == [url]


def test_get_text_plain_http_failure_still_retries(monkeypatch):
    url = "https://www.bilibili.com/opus/123"
    client, calls = _client_with_http_responses(
        monkeypatch, [_response(url, status=503), httpx.Response(200, text="ok", request=httpx.Request("GET", url))]
    )
    assert client.get_text(url, retries=3) == "ok"
    assert calls == [url, url]


@pytest.mark.parametrize("code", [-352, -509])
def test_latest_video_primary_payload_risk_skips_fallback(monkeypatch, code):
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, code=code)])
    fallback: list[int] = []
    monkeypatch.setattr(client, "_get_latest_video_fallback", lambda mid, *, referer: fallback.append(mid))
    with pytest.raises(RuntimeError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) == code
    assert calls == [url]
    assert fallback == []


def test_latest_video_primary_http_429_skips_fallback(monkeypatch):
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, status=429)])
    fallback: list[int] = []
    monkeypatch.setattr(client, "_get_latest_video_fallback", lambda mid, *, referer: fallback.append(mid))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) == 429
    assert calls == [url]
    assert fallback == []


@pytest.mark.parametrize("code", [-315, -412])
def test_latest_video_non_risk_business_code_keeps_fallback(monkeypatch, code):
    url = "https://api.bilibili.com/x/space/wbi/arc/search"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, code=code)])
    fallback: list[int] = []

    def fake_fallback(mid, *, referer):
        fallback.append(mid)
        return {"bvid": "BV1"}

    monkeypatch.setattr(client, "_get_latest_video_fallback", fake_fallback)
    assert client.get_latest_video(101) == {"bvid": "BV1"}
    assert calls == [url]
    assert fallback == [101]


def test_latest_video_fallback_risk_is_not_wrapped(monkeypatch):
    client = object.__new__(bc.BilibiliClient)
    requests: list[str] = []

    def fake_json(url, *args, **kwargs):
        requests.append(url)
        if len(requests) == 1:
            return {"code": 0, "data": {"list": {"vlist": []}}}
        return message_api._require_ok({"code": -509, "message": "平台返回"}, action="旧接口")

    monkeypatch.setattr(client, "get_json", fake_json)
    with pytest.raises(RuntimeError) as caught:
        client.get_latest_video(101)
    assert risk_code_of(caught.value) == -509
    assert len(requests) == 2


@pytest.mark.parametrize("code", [-352, -509])
def test_video_detail_primary_payload_risk_skips_html(monkeypatch, code):
    url = "https://api.bilibili.com/x/web-interface/view"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, code=code)])
    html: list[str] = []
    monkeypatch.setattr(client, "_get_video_detail_from_page", lambda bvid, *, referer: html.append(bvid))
    with pytest.raises(RuntimeError) as caught:
        client.get_video_detail("BV1")
    assert risk_code_of(caught.value) == code
    assert calls == [url]
    assert html == []


def test_video_detail_http_429_skips_html(monkeypatch):
    url = "https://api.bilibili.com/x/web-interface/view"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, status=429)])
    html: list[str] = []
    monkeypatch.setattr(client, "_get_video_detail_from_page", lambda bvid, *, referer: html.append(bvid))
    with pytest.raises(httpx.HTTPStatusError) as caught:
        client.get_video_detail("BV1")
    assert risk_code_of(caught.value) == 429
    assert calls == [url]
    assert html == []


def test_video_detail_plain_failure_keeps_html_fallback(monkeypatch):
    url = "https://api.bilibili.com/x/web-interface/view"
    client, calls = _client_with_http_responses(monkeypatch, [_response(url, code=-400)])
    html: list[str] = []

    def fake_html(bvid, *, referer):
        html.append(bvid)
        return {"bvid": bvid}

    monkeypatch.setattr(client, "_get_video_detail_from_page", fake_html)
    assert client.get_video_detail("BV1") == {"bvid": "BV1"}
    assert calls == [url]
    assert html == ["BV1"]


def test_llm_http_429_and_plain_network_errors_are_not_bilibili_risk():
    llm_request = httpx.Request("POST", "https://llm.test/v1/chat/completions")
    llm_response = httpx.Response(429, request=llm_request)
    llm_error = httpx.HTTPStatusError("429", request=llm_request, response=llm_response)
    assert risk_code_of(llm_client._map_http_error(llm_error)) is None
    bili_request = httpx.Request("GET", "https://api.bilibili.com/x/test")
    for error in (
        httpx.ConnectError("closed", request=bili_request),
        httpx.ConnectError("TLS EOF", request=bili_request),
        httpx.TimeoutException("timeout", request=bili_request),
        httpx.ProxyError("proxy", request=bili_request),
    ):
        assert risk_code_of(error) is None


@pytest.mark.parametrize("source", ["auto", "ui"])
def test_winner_payload_risk_reaches_existing_job_terminal(
    isolated_home: Path,
    winner_profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
):
    risk_path = tmp_path / "risk-state.json"
    monkeypatch.setattr(auto_remote_state, "_state_path", lambda: risk_path)
    clients: list[_MessageClient] = []

    class FakeClient(_MessageClient):
        def __init__(self, *args, **kwargs):
            super().__init__({"code": -352, "message": "平台返回"})
            clients.append(self)

        def close(self):
            pass

    monkeypatch.setattr(bc, "BilibiliClient", FakeClient)
    runner = JobRunner()
    job_id = runner.try_start("winner_deep_scan", {}, source=source)
    assert job_id is not None
    deadline = time.monotonic() + 5
    while runner.resolve_job_status(job_id).state == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    terminal = runner.resolve_job_status(job_id)
    assert terminal.state == "error"
    assert (terminal.result or {}).get("risk_code") == -352
    assert get_job(job_id)["error_kind"] == "risk"
    assert clients and len(clients[0].calls) == 1
    assert auto_remote_state.is_risk_paused() is (source == "auto")
