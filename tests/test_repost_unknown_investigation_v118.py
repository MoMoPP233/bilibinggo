"""Exercise actual HTTP status/JSON handling without a real connection."""
from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from src.bilibili_client import BilibiliClient
from src.lottery_actions import repost_dynamic
from src.participation_guard import get_guard
from src.platform_risk import risk_code_of

TARGET = "1251573729479098372"


@pytest.mark.parametrize("status, body, expected, formal", [
    (200, b'{"code":0,"data":{"dynamic_id":"999"}}', "confirmed", None),
    (200, b'{"code":false}', "unknown", None),
    (200, b'{"code":"0"}', "unknown", None),
    (200, b'{}', "unknown", None),
    (200, b'<html>bad</html>', "unknown", None),
    (500, b'{}', "unknown", None),
    (429, b'{}', "unknown", 429),
    (200, b'{"code":-352}', "unknown", -352),
    (200, b'{"code":-509}', "unknown", -509),
    (200, b'{"code":-315}', "unknown", None),
    (200, b'{"code":-412}', "unknown", None),
])
def test_repost_transport_matrix_preserves_guard_and_zero_retries(isolated_home, status, body, expected, formal):
    client = object.__new__(BilibiliClient)
    client._http_post = Mock(return_value=httpx.Response(status, content=body, request=httpx.Request("POST", "https://invalid.test")))
    def send():
        return repost_dynamic(client, dynamic_id=TARGET, my_uid=42, csrf="test", referer="local", content="generated")
    if formal is not None:
        with pytest.raises(Exception) as error:
            send()
        assert risk_code_of(error.value) == formal
    else:
        assert send().ok is (expected == "confirmed")
    assert get_guard(42, TARGET).repost_status == expected
    assert client._http_post.call_count == 1
    # Restart/log retention cannot grant another automatic POST.
    assert send().ok is (expected == "confirmed")
    assert client._http_post.call_count == 1


@pytest.mark.parametrize("error", [httpx.ReadTimeout("lost"), httpx.ConnectError("disconnected"), httpx.RemoteProtocolError("incomplete response")])
def test_repost_transport_loss_stays_unknown(isolated_home, error):
    client = object.__new__(BilibiliClient)
    def lost(*args, **kwargs):
        assert get_guard(42, TARGET).repost_status == "pending"
        raise error
    client._http_post = Mock(side_effect=lost)
    assert not repost_dynamic(client, dynamic_id=TARGET, my_uid=42, csrf="test", referer="local", content="generated").ok
    assert get_guard(42, TARGET).repost_status == "unknown"
    assert not repost_dynamic(client, dynamic_id=TARGET, my_uid=42, csrf="test", referer="local", content="generated").ok
    assert client._http_post.call_count == 1
