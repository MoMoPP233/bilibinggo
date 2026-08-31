from __future__ import annotations

import json

import httpx
import pytest

from src.bilibili_client import BilibiliClient
from src.lottery_api import (
    OPUS_DETAIL_FEATURES,
    OPUS_DETAIL_URL,
    OpusReadError,
    OpusRiskControlError,
    fetch_opus_detail_item_strict,
)

DYNAMIC_ID = "1240566780561195013"


class FakeClient:
    def __init__(self, payload=None, *, error: Exception | None = None):
        self.payload = payload
        self.error = error
        self.calls = []

    def request_json(self, url, params=None, *, referer=None, retries=3):
        self.calls.append((url, params, referer, retries))
        if self.error is not None:
            raise self.error
        return self.payload


def test_strict_fetch_preserves_raw_modules_and_numeric_item_type() -> None:
    item = {
        "type": 1,
        "basic": {"comment_type": 12, "article_type": 4},
        "modules": [
            {"module_content": None, "module_author": {"mid": 100680137}},
            {"module_content": {"paragraphs": []}, "module_author": None},
        ],
    }
    client = FakeClient({"code": 0, "data": {"item": item}})

    assert fetch_opus_detail_item_strict(client, DYNAMIC_ID) is item
    assert isinstance(item["modules"], list)
    assert client.calls == [
        (
            OPUS_DETAIL_URL,
            {"id": DYNAMIC_ID, "features": OPUS_DETAIL_FEATURES},
            f"https://www.bilibili.com/opus/{DYNAMIC_ID}",
            0,
        )
    ]


def test_strict_fetch_explicit_same_id_dynamic_fallback() -> None:
    client = FakeClient(
        {"code": 0, "data": {"item": None, "fallback": {"type": 1, "id": DYNAMIC_ID}}}
    )
    assert fetch_opus_detail_item_strict(client, DYNAMIC_ID) is None


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        {"item": None},
        {"item": {}},
        {"item": None, "fallback": {"type": 1, "id": "1240944449910996997"}},
        {"item": None, "fallback": {"type": 2, "id": DYNAMIC_ID}},
        {"item": None, "fallback": {"type": True, "id": DYNAMIC_ID}},
        {"item": {}, "fallback": {"type": 1, "id": DYNAMIC_ID}},
        {"fallback": {"type": 1, "id": DYNAMIC_ID}},
    ],
)
def test_strict_fetch_missing_item_never_becomes_implicit_dynamic(data) -> None:
    with pytest.raises(OpusReadError):
        fetch_opus_detail_item_strict(FakeClient({"code": 0, "data": data}), DYNAMIC_ID)


@pytest.mark.parametrize("code", [-404, 404, -403, -352, -509, -799])
def test_strict_fetch_known_api_read_failures(code: int) -> None:
    with pytest.raises(OpusReadError, match=str(code)):
        fetch_opus_detail_item_strict(
            FakeClient({"code": code, "message": "unavailable"}), DYNAMIC_ID
        )


@pytest.mark.parametrize("status,code", [(200, -352), (503, 0)])
def test_strict_fetch_real_client_does_not_retry_read_failures(monkeypatch, status, code) -> None:
    calls: list[str] = []

    def http_get(url, *, params=None, headers=None):
        calls.append(url)
        return httpx.Response(
            status,
            json={"code": code, "message": "risk control"},
            request=httpx.Request("GET", url),
        )

    with BilibiliClient(warmup=False) as client:
        monkeypatch.setattr(client, "_http_get", http_get)
        error_type = OpusRiskControlError if code == -352 else OpusReadError
        with pytest.raises(error_type):
            fetch_opus_detail_item_strict(client, DYNAMIC_ID)

    assert calls == [OPUS_DETAIL_URL]


@pytest.mark.parametrize("code", [-1, -400, -500, 12345])
def test_strict_fetch_unknown_api_code_is_not_skippable(code: int) -> None:
    with pytest.raises(RuntimeError) as caught:
        fetch_opus_detail_item_strict(FakeClient({"code": code}), DYNAMIC_ID)
    assert not isinstance(caught.value, OpusReadError)


@pytest.mark.parametrize("status", [404, 403, 429, 500])
def test_strict_fetch_http_failure_is_skippable(status: int) -> None:
    request = httpx.Request("GET", OPUS_DETAIL_URL)
    error = httpx.HTTPStatusError(
        "read failed", request=request, response=httpx.Response(status, request=request)
    )
    with pytest.raises(OpusReadError) as caught:
        fetch_opus_detail_item_strict(FakeClient(error=error), DYNAMIC_ID)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize("wrapped", [False, True])
def test_strict_fetch_network_failure_is_skippable(wrapped: bool) -> None:
    error: Exception = httpx.ConnectError("connection failed")
    if wrapped:
        outer = RuntimeError("网络请求失败")
        outer.__cause__ = error
        error = outer
    with pytest.raises(OpusReadError) as caught:
        fetch_opus_detail_item_strict(FakeClient(error=error), DYNAMIC_ID)
    assert caught.value.__cause__ is error


@pytest.mark.parametrize(
    "error",
    [RuntimeError("unexpected bug"), ValueError("invalid value"), json.JSONDecodeError("bad JSON", "", 0)],
)
def test_strict_fetch_unknown_errors_propagate_unchanged(error: Exception) -> None:
    with pytest.raises(type(error)) as caught:
        fetch_opus_detail_item_strict(FakeClient(error=error), DYNAMIC_ID)
    assert caught.value is error


def test_strict_fetch_wrapped_json_failure_propagates_unchanged() -> None:
    error = RuntimeError("响应不是有效 JSON")
    error.__cause__ = json.JSONDecodeError("bad JSON", "", 0)
    with pytest.raises(RuntimeError) as caught:
        fetch_opus_detail_item_strict(FakeClient(error=error), DYNAMIC_ID)
    assert caught.value is error


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"code": False},
        {"code": "0"},
        {"code": 0, "data": []},
        {"code": 0, "data": {"item": []}},
        {"code": 0, "data": {"item": "unexpected"}},
        {"code": 0, "data": {"item": None, "fallback": []}},
    ],
)
def test_strict_fetch_invalid_response_shape_is_not_skippable(payload) -> None:
    with pytest.raises((TypeError, ValueError)):
        fetch_opus_detail_item_strict(FakeClient(payload), DYNAMIC_ID)
