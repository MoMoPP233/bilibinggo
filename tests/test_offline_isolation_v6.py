"""V6 测试外网隔离验证：fail-closed guard 行为（全部零真实公网）。"""

from __future__ import annotations

import os
import socket

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.offline_guard import (
    ExternalNetworkBlocked,
    install_offline_guard,
    restore_proxy_env,
    strip_proxy_env,
)

PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy")


def test_external_hostname_dns_is_blocked_before_resolver() -> None:
    with pytest.raises(ExternalNetworkBlocked, match="api.bilibili.com"):
        socket.getaddrinfo("api.bilibili.com", 443)
    with pytest.raises(ExternalNetworkBlocked):
        socket.gethostbyname("www.bilibili.com")


def test_external_ipv4_socket_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    with pytest.raises(ExternalNetworkBlocked, match="203.0.113.1"):
        sock.connect(("203.0.113.1", 443))  # RFC 文档保留地址，不会真的触网
    sock.close()


def test_external_ipv6_socket_is_blocked() -> None:
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    with pytest.raises(ExternalNetworkBlocked, match="2001:db8"):
        sock.connect(("2001:db8::1", 443))  # RFC 文档保留地址
    sock.close()


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
    for key in PROXY_KEYS:
        assert os.environ.get(key) is None, f"{key} 不应在测试进程生效"


def test_proxy_strip_restore_roundtrip(monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:65000")
    saved = strip_proxy_env()
    assert "HTTPS_PROXY" not in os.environ
    assert "https_proxy" not in os.environ
    # 恢复本测试自己保存的原值（进程级 restore 只用于 session 收尾）。
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
            os.environ.pop(key.upper(), None)
        else:
            os.environ[key] = value
    assert os.environ.get("https_proxy") == "http://127.0.0.1:65000"
    os.environ.pop("https_proxy", None)
    os.environ.pop("HTTPS_PROXY", None)


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
