"""本地控制台安全边界（V8）：Host 校验 + 浏览器跨站写请求 Origin 校验。

实现为纯 ASGI 中间件（不缓冲 SSE/streaming body）：
- 只接受 loopback Host（localhost / 127.0.0.0/8 / ::1）或 httpx TestClient 的
  testserver 标识（仅当其 ASGI client 地址也是 'testclient' 时）。
- 对 POST/PUT/PATCH/DELETE：若请求携带 Origin，仅允许 loopback Origin；
  无 Origin（MCP/CLI/httpx 本地客户端）只要 Host 合法即放行。
- 被拒请求在进入业务 handler 前返回 400/403，不产生任何业务副作用。
"""

from __future__ import annotations

import ipaddress
from typing import Any

from starlette.datastructures import MutableHeaders  # noqa: F401  (类型参考)

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost."})
_TEST_CLIENT_HOST = "testclient"

REJECT_HOST_BODY = b"invalid host"
REJECT_ORIGIN_BODY = b"forbidden origin"


def is_loopback_hostname(host: str) -> bool:
    lowered = (host or "").strip().rstrip(".").lower()
    if not lowered:
        return False
    if lowered in _LOOPBACK_HOSTNAMES or lowered == "localhost.localdomain":
        return True
    try:
        return ipaddress.ip_address(lowered).is_loopback
    except ValueError:
        return False


def parse_host_header(raw: str) -> str | None:
    """从 Host 头取出 hostname（剥离端口；正确处理 [::1]:8787）。"""
    value = str(raw or "").strip()
    if not value:
        return None
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return None
        return value[1:end]
    # 无括号 IPv6（Host 头不允许）与无端口 hostname 均不含冒号陷阱：
    if value.count(":") == 0:
        return value
    # host:port 形式（仅允许单冒号）
    host, _, rest = value.rpartition(":")
    if not rest or not host or ":" in host:
        return None
    return host


def host_allowed(host_header: str, *, client_host: str | None) -> bool:
    hostname = parse_host_header(host_header)
    if hostname is None:
        return False
    if is_loopback_hostname(hostname):
        return True
    # httpx TestClient 使用 Host: testserver 且 ASGI client 地址为 testclient：
    # 只在测试 ASGI 环境中放行，真实 socket 连接无法伪造 scope['client']。
    if hostname.lower() == "testserver" and client_host == _TEST_CLIENT_HOST:
        return True
    return False


def origin_allowed(origin: str | None) -> bool:
    if not origin:
        return False
    value = str(origin).strip()
    if not value or value.lower() == "null":
        return False
    scheme, sep, rest = value.partition("://")
    if not sep or scheme.lower() not in {"http", "https"}:
        return False
    authority = rest.split("/", 1)[0]
    if "@" in authority or " " in authority:
        return False
    hostname = parse_host_header(authority)
    if hostname is None:
        return False
    return is_loopback_hostname(hostname)


def _scope_headers(scope: dict[str, Any]) -> dict[bytes, bytes]:
    headers: dict[bytes, bytes] = {}
    for key, value in scope.get("headers") or []:
        headers[key.lower()] = value
    return headers


async def _reject(scope: dict[str, Any], send: Any, *, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class LocalControlPlaneGuard:
    """Host/Origin 边界：必须最先执行（最外层）。"""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = _scope_headers(scope)
        client = scope.get("client")
        client_host = client[0] if isinstance(client, tuple) and client else None
        host_header = headers.get(b"host")
        host_value = host_header.decode("latin-1", "replace") if host_header else ""

        # Host 缺失或非 loopback（含 DNS rebinding 伪造 Host）一律拒绝。
        if not host_value or not host_allowed(host_value, client_host=client_host):
            await _reject(scope, send, status=400, body=REJECT_HOST_BODY)
            return

        method = str(scope.get("method") or "GET").upper()
        if method in _UNSAFE_METHODS:
            origin_header = headers.get(b"origin")
            if origin_header is not None:
                origin_value = origin_header.decode("latin-1", "replace")
                # 浏览器跨站写请求必须在进入 handler 前被拒。
                if not origin_allowed(origin_value):
                    await _reject(scope, send, status=403, body=REJECT_ORIGIN_BODY)
                    return

        await self.app(scope, receive, send)
