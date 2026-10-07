"""V104: safe, structured diagnostics for the existing comment unknown path."""

from __future__ import annotations

import json
import logging
import ssl
from unittest.mock import Mock

import httpx
import pytest

from src import comment_confirmation, lottery_actions as actions
from src.bilibili_client import BilibiliClient
from src.platform_risk import risk_code_of


DYNAMIC_ID = "1251573729479098372"
UID = 42
SECRET = "Cookie=secret-cookie Authorization=secret-token comment-secret-body"


@pytest.fixture(autouse=True)
def _login(monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("fake-csrf", UID))


def _kwargs():
    return {
        "rid": DYNAMIC_ID, "comment_type": 17, "message": "comment-secret-body",
        "csrf": "secret-token", "referer": f"https://www.bilibili.com/opus/{DYNAMIC_ID}",
        "dynamic_id": DYNAMIC_ID, "uid": UID,
    }


def _diagnostics(caplog):
    return [
        record.binggo_fields
        for record in caplog.records
        if getattr(record, "binggo_fields", {}).get("event") == "participation.comment.unknown"
    ]


def _post_client(outcome):
    client = Mock()
    if isinstance(outcome, Exception):
        client.post_form.side_effect = outcome
    else:
        client.post_form.return_value = outcome
    client.get_json.side_effect = AssertionError("no extra Bilibili GET")
    client.request_json.side_effect = AssertionError("no extra Bilibili request")
    return client


def test_success_has_no_unknown_diagnostic(isolated_home, caplog):
    client = _post_client({"code": 0})
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        assert actions.comment_dynamic(client, **_kwargs()).ok
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "confirmed"
    assert _diagnostics(caplog) == []
    assert client.post_form.call_args.kwargs["retries"] == 0
    assert client.post_form.call_count == 1


@pytest.mark.parametrize(
    ("exception", "domain", "error_type"),
    [
        (httpx.ReadTimeout(SECRET), "network", "ReadTimeout"),
        (httpx.ConnectError(SECRET), "network", "ConnectError"),
    ],
)
def test_transport_unknown_identifies_type_without_secrets(
    isolated_home, caplog, exception, domain, error_type,
):
    client = _post_client(exception)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(type(exception)):
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert len(fields) == 1
    assert fields[0]["phase"] == "post"
    assert fields[0]["error_kind"] == domain
    assert fields[0]["extra"] == {
        "action": "comment", "result": "unknown", "error_type": error_type,
    }
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"
    assert client.post_form.call_count == 1
    assert client.post_form.call_args.kwargs["retries"] == 0
    assert SECRET not in caplog.text
    assert "secret-token" not in json.dumps(fields)


def test_tls_cause_is_network_unknown_not_formal_risk(isolated_home, caplog):
    error = httpx.ConnectError(SECRET)
    error.__cause__ = ssl.SSLError(SECRET)
    client = _post_client(error)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.ConnectError) as caught:
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["error_kind"] == "network"
    assert fields[0]["extra"]["error_type"] == "SSLError"
    assert risk_code_of(caught.value) is None
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"
    assert SECRET not in caplog.text


def test_network_exception_text_cannot_turn_diagnostic_into_risk(isolated_home, caplog):
    client = _post_client(httpx.ConnectError("API error -352: " + SECRET))
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.ConnectError):
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["error_kind"] == "network"
    assert "bili_code" not in fields[0]["extra"]


def test_http_429_keeps_formal_risk_and_unknown_barrier(isolated_home, caplog):
    request = httpx.Request("POST", actions.COMMENT_URL)
    response = httpx.Response(429, request=request, json={"code": 429})
    error = httpx.HTTPStatusError(SECRET, request=request, response=response)
    client = _post_client(error)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["error_kind"] == "risk"
    assert fields[0]["extra"]["http_status"] == 429
    assert risk_code_of(caught.value) == 429
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"
    assert client.post_form.call_count == 1
    assert SECRET not in caplog.text


def test_http_503_is_http_unknown_not_formal_risk(isolated_home, caplog):
    request = httpx.Request("POST", actions.COMMENT_URL)
    response = httpx.Response(503, request=request, json={"code": -1})
    error = httpx.HTTPStatusError(SECRET, request=request, response=response)
    client = _post_client(error)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["error_kind"] == "http"
    assert fields[0]["extra"]["http_status"] == 503
    assert risk_code_of(caught.value) is None
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"


@pytest.mark.parametrize("code", [-352, -509])
def test_formal_payload_risk_keeps_existing_contract_without_unknown_log(
    isolated_home, caplog, code,
):
    client = _post_client({"code": code, "message": SECRET})
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(actions.ParticipationRiskError) as caught:
            actions.comment_dynamic(client, **_kwargs())
    assert risk_code_of(caught.value) == code
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "failed"
    assert _diagnostics(caplog) == []
    assert client.post_form.call_count == 1


@pytest.mark.parametrize("code", [-315, -412])
def test_plain_codes_stay_unknown_not_formal_risk(isolated_home, caplog, code):
    client = _post_client({"code": code, "message": SECRET})
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        result = actions.comment_dynamic(client, **_kwargs())
    assert not result.ok
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"
    fields = _diagnostics(caplog)
    assert fields[0]["phase"] == "response_validate"
    assert fields[0]["error_kind"] == "response"
    assert fields[0]["extra"]["bili_code"] == code
    assert SECRET not in caplog.text
    assert client.post_form.call_count == 1


@pytest.mark.parametrize("payload", [None, {}, {"code": "0"}, {"code": True}])
def test_invalid_code_is_response_validation_unknown(isolated_home, caplog, payload):
    client = _post_client(payload)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        result = actions.comment_dynamic(client, **_kwargs())
    assert not result.ok
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"
    fields = _diagnostics(caplog)
    assert fields[0]["phase"] == "response_validate"
    assert fields[0]["error_kind"] == "response"
    assert fields[0]["extra"]["error_type"] == "InvalidCode"
    assert client.post_form.call_count == 1


def test_malformed_json_from_real_post_helper_is_parse_unknown(isolated_home, monkeypatch, caplog):
    client = object.__new__(BilibiliClient)
    request = httpx.Request("POST", actions.COMMENT_URL)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        return httpx.Response(200, request=request, text="{")

    monkeypatch.setattr(client, "_http_post", fake_post)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(RuntimeError, match="响应不是有效 JSON"):
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["phase"] == "response_parse"
    assert fields[0]["error_kind"] == "response"
    assert fields[0]["extra"]["error_type"] == "JSONDecodeError"
    assert len(calls) == 1
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"


@pytest.mark.parametrize(
    ("exception", "error_type"),
    [(httpx.ReadTimeout(SECRET), "ReadTimeout"), (httpx.ConnectError(SECRET), "ConnectError")],
)
def test_real_post_helper_transport_wrapper_preserves_safe_type(
    isolated_home, monkeypatch, caplog, exception, error_type,
):
    client = object.__new__(BilibiliClient)
    calls = []

    def fake_post(*args, **kwargs):
        calls.append((args, kwargs))
        raise exception

    monkeypatch.setattr(client, "_http_post", fake_post)
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(RuntimeError, match="网络请求失败"):
            actions.comment_dynamic(client, **_kwargs())
    fields = _diagnostics(caplog)
    assert fields[0]["error_kind"] == "network"
    assert fields[0]["extra"]["error_type"] == error_type
    assert len(calls) == 1
    assert SECRET not in caplog.text
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "unknown"


def test_failed_unknown_write_logs_safely_and_leaves_pending(isolated_home, monkeypatch, caplog):
    real_writer = actions._set_comment_status_unlocked

    def writer(target, *, uid, status):
        if status == "unknown":
            raise OSError(SECRET)
        return real_writer(target, uid=uid, status=status)

    monkeypatch.setattr(actions, "_set_comment_status_unlocked", writer)
    client = _post_client(httpx.ReadTimeout(SECRET))
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.ReadTimeout):
            actions.comment_dynamic(client, **_kwargs())
    assert comment_confirmation.get_comment_status(DYNAMIC_ID, uid=UID) == "pending"
    assert len(_diagnostics(caplog)) == 1
    assert any(
        getattr(record, "binggo_fields", {}).get("event") == "participation.comment.guard_write_failed"
        for record in caplog.records
    )
    assert SECRET not in caplog.text


def test_unknown_survives_restart_without_new_post(isolated_home, caplog):
    client = _post_client(httpx.ReadTimeout(SECRET))
    with caplog.at_level(logging.WARNING, logger="binggo.participation"):
        with pytest.raises(httpx.ReadTimeout):
            actions.comment_dynamic(client, **_kwargs())
    second = _post_client(AssertionError("must not POST again"))
    result = actions.comment_dynamic(second, **_kwargs())
    assert not result.ok
    second.post_form.assert_not_called()
    assert client.post_form.call_count == 1
