"""V6 测试外网隔离验证：fail-closed guard 行为（全部零真实公网）。"""

from __future__ import annotations

import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

import tests.offline_guard as offline_guard
from tests.offline_guard import (
    ExternalNetworkBlocked,
    install_offline_guard,
    restore_proxy_env,
    strip_proxy_env,
)

PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
)


class _LoopbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return


def test_external_hostname_dns_is_blocked_before_resolver(monkeypatch) -> None:
    def unexpected_resolver(*_args, **_kwargs):
        pytest.fail("external hostname reached the original resolver")

    monkeypatch.setattr(offline_guard, "_orig_getaddrinfo", unexpected_resolver)
    with pytest.raises(ExternalNetworkBlocked, match="api.bilibili.com"):
        socket.getaddrinfo("api.bilibili.com", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.gethostbyname("www.bilibili.com")


def test_external_ipv4_socket_is_blocked() -> None:
    for host in ("203.0.113.1", "8.8.8.8", "1.1.1.1"):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            with pytest.raises(ExternalNetworkBlocked, match=host):
                sock.connect((host, 443))
        finally:
            sock.close()


def test_external_ipv6_socket_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    with pytest.raises(ExternalNetworkBlocked, match="2001:db8"):
        sock.connect(("2001:db8::1", 443))  # RFC 文档保留地址
    sock.close()


def test_external_create_connection_is_blocked_before_dns() -> None:
    with pytest.raises(ExternalNetworkBlocked, match="8.8.8.8"):
        socket.create_connection(("8.8.8.8", 443), timeout=1)


def test_localhost_dns_allowed() -> None:
    result = socket.getaddrinfo("localhost", 80)
    assert result


def test_loopback_ipv4_connect_reaches_local_path() -> None:
    """guard 放行 127.0.0.1；无服务时表现为本地拒绝而不是“外网被拦”。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.connect(("127.0.0.1", 1))  # 任意未监听端口
    except OSError:
        pass  # 本地路径允许（拒绝连接是 OS 行为）
    except ExternalNetworkBlocked:
        pytest.fail("127.0.0.1 不应被外网 guard 拦截")
    finally:
        sock.close()


def test_loopback_ipv6_connect_reaches_local_path() -> None:
    try:
        sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    except OSError:
        pytest.skip("平台不支持 IPv6")
    try:
        sock.connect(("::1", 1))
    except OSError:
        pass
    except ExternalNetworkBlocked:
        pytest.fail("::1 不应被外网 guard 拦截")
    finally:
        sock.close()


def test_localhost_httpx_request_is_allowed() -> None:
    server = ThreadingHTTPServer(("localhost", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        response = httpx.get(f"http://localhost:{port}/health", timeout=5)
        assert response.status_code == 200
        assert response.text == "ok"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_httpx_direct_external_request_is_blocked() -> None:
    with pytest.raises(ExternalNetworkBlocked):
        httpx.get("https://api.bilibili.com/x/web-interface/nav", timeout=5)


def test_real_bilibili_client_without_mock_is_blocked() -> None:
    from src.bilibili_client import BilibiliClient

    with pytest.raises(ExternalNetworkBlocked):
        with BilibiliClient(warmup=False) as client:
            client.request_json(
                "https://api.bilibili.com/x/web-interface/nav",
                referer="https://www.bilibili.com",
                retries=0,
            )


def test_proxy_env_stripped_in_test_process() -> None:
    for key in PROXY_ENV_KEYS[:6]:
        assert os.environ.get(key) is None, f"{key} 不应在测试进程生效"
    assert os.environ.get("NO_PROXY") == "*"
    assert os.environ.get("no_proxy") == "*"


def test_local_proxy_cannot_bypass_external_guard(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:65000")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:65000")
    install_offline_guard()
    with pytest.raises(ExternalNetworkBlocked):
        httpx.get("https://api.bilibili.com/x/web-interface/nav", timeout=1)


def test_proxy_strip_restore_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:65000")
    monkeypatch.setenv("NO_PROXY", "example.com")
    saved = strip_proxy_env()
    assert "HTTPS_PROXY" not in os.environ
    assert "https_proxy" not in os.environ
    assert "NO_PROXY" not in os.environ
    assert "no_proxy" not in os.environ
    # 恢复本测试自己保存的原值（进程级 restore 只用于 session 收尾）。
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
            os.environ.pop(key.upper(), None)
        else:
            os.environ[key] = value
    assert os.environ.get("https_proxy") == "http://127.0.0.1:65000"
    assert os.environ.get("no_proxy") == "example.com"
    os.environ.pop("https_proxy", None)
    os.environ.pop("HTTPS_PROXY", None)
    # Keep the session-wide isolation active for following tests.
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def test_mock_transport_and_asgi_client_unaffected() -> None:
    # httpx MockTransport：完全不触 socket，guard 不干扰。
    def handler(request):
        return httpx.Response(200, json={"ok": True})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    response = client.get("https://api.bilibili.com/x/example")
    assert response.status_code == 200

    # FastAPI TestClient：ASGI in-process，不触 socket。
    from web.app import app

    test_client = TestClient(app)
    assert test_client.get("/api/auto/status").status_code == 200


def test_guard_installed_idempotent() -> None:
    install_offline_guard()  # 重复安装无副作用
    install_offline_guard()
    with pytest.raises(ExternalNetworkBlocked):
        socket.getaddrinfo("example.com", 443)
