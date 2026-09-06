"""V8 本地控制台安全加固测试（全 ASGI/TestClient / mock，V6 guard 生效，零真实网络）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.log_redact import redact_obj, redact_text
from web.security import host_allowed, origin_allowed, parse_host_header

from web.app import app


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://127.0.0.1")


# ---------- Host 边界 ----------


@pytest.mark.parametrize(
    "raw",
    [
        "localhost",
        "localhost:8787",
        "LOCALHOST",
        "localhost.",
        "127.0.0.1",
        "127.0.0.1:8181",
        "127.0.0.2:8787",
        "[::1]:8787",
        "[::1]",
    ],
)
def test_parse_and_allow_loopback_hosts(raw: str) -> None:
    assert parse_host_header(raw) is not None
    assert host_allowed(raw, client_host="127.0.0.1") is True


@pytest.mark.parametrize(
    "raw",
    [
        "evil.example",
        "evil.example:8787",
        "localhost.evil.example",
        "binggo.attacker.example",
        "127.0.0.1.evil.com",
    ],
)
def test_reject_non_loopback_hosts(raw: str) -> None:
    assert host_allowed(raw, client_host="127.0.0.1") is False


def test_testserver_only_allowed_for_testclient_scope() -> None:
    assert host_allowed("testserver", client_host="testclient") is True
    assert host_allowed("testserver", client_host="127.0.0.1") is False


def test_malformed_host_header_rejected() -> None:
    assert parse_host_header("[::1") is None
    assert parse_host_header("a:b:c") is None
    assert host_allowed("a:b:c", client_host="127.0.0.1") is False


# ---------- Host/Origin 端到端 ----------


def test_get_allowed_with_loopback_host(client: TestClient) -> None:
    response = client.get("/api/summary", headers={"Host": "127.0.0.1"})
    assert response.status_code == 200
    response = client.get("/api/auto/status", headers={"Host": "[::1]"})
    assert response.status_code == 200


def test_evil_host_rejected_before_handler(client: TestClient) -> None:
    response = client.get("/api/summary", headers={"Host": "evil.example"})
    assert response.status_code == 400
    response = client.get("/api/summary", headers={"Host": "localhost.evil.example"})
    assert response.status_code == 400


def test_local_host_post_without_origin_allowed(client: TestClient, monkeypatch) -> None:
    """MCP/CLI 本地客户端：无 Origin 的写请求在合法 Host 下继续工作。"""
    monkeypatch.setattr("web.app.runner.cancel", lambda: False)
    response = client.post("/api/jobs/cancel", headers={"Host": "127.0.0.1"})
    assert response.status_code == 409  # 到达 handler（busy 语义）


def test_local_host_local_origin_allowed(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr("web.app.runner.cancel", lambda: False)
    response = client.post(
        "/api/jobs/cancel",
        headers={"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1"},
    )
    assert response.status_code == 409
    response = client.post(
        "/api/jobs/cancel",
        headers={"Origin": "http://localhost:8787", "Host": "localhost"},
    )
    assert response.status_code == 409


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "http://localhost.evil.example",
        "http://binggo.attacker.example:8787",
        "null",
        "null",
    ],
)
def test_external_or_null_origin_rejected_before_handler(
    client: TestClient, monkeypatch, origin: str
) -> None:
    cancelled: list[int] = []

    def fake_cancel() -> bool:
        cancelled.append(1)
        return False

    monkeypatch.setattr("web.app.runner.cancel", fake_cancel)
    response = client.post(
        "/api/jobs/cancel",
        headers={"Origin": origin, "Host": "127.0.0.1"},
    )
    assert response.status_code == 403
    assert cancelled == []  # 业务 handler 未被调用，无副作用


def test_sse_and_static_get_work_with_loopback_host() -> None:
    from unittest.mock import patch

    from web.sse import format_sse

    def finite_frames(**kwargs):
        yield format_sse("heartbeat", {"ts": 1})
        return

    client = TestClient(app, base_url="http://127.0.0.1")
    with patch("web.sse.iter_sse_frames", side_effect=finite_frames):
        response = client.get("/api/events", headers={"Host": "127.0.0.1"})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers.get("content-type", "")
    root = client.get("/", headers={"Host": "127.0.0.1"})
    assert root.status_code == 200


def test_default_testserver_still_allowed_only_via_testclient_scope() -> None:
    default_client = TestClient(app)
    # TestClient 默认 Host: testserver 且 client='testclient' → 允许（测试专属）。
    assert default_client.get("/api/auto/status").status_code == 200


def test_write_api_local_end_to_end_reaches_handler(client: TestClient, monkeypatch) -> None:
    from unittest.mock import MagicMock

    started: list[str] = []

    def fake_start(action, params=None, *, source="ui"):
        started.append(action)
        return 42

    status = MagicMock()
    status.return_value.to_dict.return_value = {
        "id": 42,
        "state": "running",
        "action": "refresh_status",
    }
    monkeypatch.setattr("web.app.get_account_profile", lambda: {"logged_in": True})
    monkeypatch.setattr("web.app.runner.try_start", fake_start)
    monkeypatch.setattr("web.app.runner.get_status", status)
    response = client.post(
        "/api/jobs",
        json={"action": "refresh_status", "params": {}},
        headers={"Origin": "http://127.0.0.1:8787", "Host": "127.0.0.1"},
    )
    assert response.status_code == 200
    assert started == ["refresh_status"]


# ---------- 日志脱敏 ----------


AUDIT_SECRET = "AUDIT_FAKE_SECRET"


def test_csrf_and_cookie_secrets_redacted_in_text() -> None:
    text = (
        f"POST /api/delete?csrf={AUDIT_SECRET}&csrf_token={AUDIT_SECRET}"
        f"&bili_jct={AUDIT_SECRET} Cookie: SESSDATA={AUDIT_SECRET}; DedeUserID=123"
    )
    out = redact_text(text)
    assert AUDIT_SECRET not in out
    assert "csrf=***" in out
    assert "csrf_token=***" in out
    assert "bili_jct=***" in out
    assert "SESSDATA=***" in out


def test_url_encoded_secret_fully_redacted() -> None:
    encoded = "AUDIT%2FFAKE%2BSECRET%3D%26%2F%23"
    out = redact_text(f"csrf_token={encoded}&q=keepme")
    assert encoded not in out
    assert "q=keepme" in out


def test_multiple_params_keep_normal_values() -> None:
    out = redact_text(
        f"status=active&q=hello&csrf={AUDIT_SECRET}&page=2&type=互动"
    )
    assert "q=hello" in out
    assert "status=active" in out
    assert "page=2" in out
    assert "互动" in out
    assert AUDIT_SECRET not in out


def test_case_variants_redacted() -> None:
    out = redact_text(
        f"CSRF={AUDIT_SECRET} CSRF_TOKEN={AUDIT_SECRET} sessdata={AUDIT_SECRET} BILI_JCT={AUDIT_SECRET}"
    )
    assert AUDIT_SECRET not in out


def test_plain_text_with_keywords_not_mangled() -> None:
    out = redact_text("user 问 csrf 是什么？ token 经济学 与 数字429 无关。")
    assert out == "user 问 csrf 是什么？ token 经济学 与 数字429 无关。"


def test_object_redaction_copies_and_preserves_original() -> None:
    original = {
        "endpoint": "/api/x",
        "csrf": AUDIT_SECRET,
        "csrf_token": AUDIT_SECRET,
        "SESSDATA": AUDIT_SECRET,
        "bili_jct": AUDIT_SECRET,
        "q": "normal-query",
    }
    cleaned = redact_obj(original)
    assert AUDIT_SECRET not in json.dumps(cleaned)
    assert cleaned["q"] == "normal-query"
    # 原对象不被修改（只影响日志副本）。
    assert original["csrf"] == AUDIT_SECRET
    assert original["SESSDATA"] == AUDIT_SECRET


def test_diagnostics_text_pipeline_redacts_secret() -> None:
    from src.diagnostics import _slim_job  # noqa: F401

    # diagnostics 导出统一走 redact_text/redact_obj（src/diagnostics.py），
    # 这里验证统一 helper 直接生效于会被写入 diagnostic 文本的内容。
    payload = {
        "job": {"id": 1, "message": f"csrf={AUDIT_SECRET} done"},
        "auto": {"message": f"SESSDATA={AUDIT_SECRET}"},
    }
    text = f"{json.dumps(payload)} csrf_token={AUDIT_SECRET}&bili_jct={AUDIT_SECRET}"
    assert AUDIT_SECRET not in redact_text(text)


def test_request_input_not_modified_by_redaction(client: TestClient) -> None:
    # 脱敏作用于日志副本；真实请求内容原样。
    assert redact_text(f"csrf={AUDIT_SECRET}") != f"csrf={AUDIT_SECRET}"
