"""pytest 外部网络隔离 guard（fail-closed）。

机制：
- 在 socket 实例 connect/connect_ex 层拦截：只允许 loopback（127.0.0.0/8、::1）
  与 AF_UNIX；任何其它目标在真正连接前抛 ExternalNetworkBlocked。
- 在 DNS 层拦截 getaddrinfo/gethostbyname：非 loopback 主机名与非 loopback
  数字地址在调用系统解析器前抛错，避免真实外部 DNS。
- 测试进程内清除 HTTP(S)/ALL proxy 环境变量，防止“环回代理转发公网”绕过；
  不改用户系统环境。
- 业务代码仍可用自己的 fake/mock（完全不触 socket 的路径不受影响）。
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path
from typing import Any

_PROXY_PAIRS = (
    ("HTTP_PROXY", "http_proxy"),
    ("HTTPS_PROXY", "https_proxy"),
    ("ALL_PROXY", "all_proxy"),
)
# Windows 环境变量大小写不敏感：内部统一以小写规范名保存/恢复，避免同变量双写互相覆盖。
_PROXY_ENV_KEYS = ("http_proxy", "https_proxy", "all_proxy")
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback"})


class ExternalNetworkBlocked(RuntimeError):
    """测试环境拒绝外部网络访问。请为本次请求提供 fake/mock，而不是真连公网。"""


def _host_kind(host: str | None) -> str | None:
    """返回 None 表示允许（loopback），否则返回用于报错的目标描述。"""
    if host is None or host == "":
        return None
    try:
        addr = ipaddress.ip_address(str(host).strip("[]"))
    except ValueError:
        lowered = str(host).strip().lower()
        if lowered in _LOOPBACK_HOSTNAMES or lowered.endswith(".localhost"):
            return None
        return str(host)
    if addr.is_loopback:
        return None
    return str(addr)


def _check_target(*, family: int, address: Any, what: str) -> None:
    af_unix = getattr(socket, "AF_UNIX", None)
    if af_unix is not None and family == af_unix:
        return
    if family not in (socket.AF_INET, socket.AF_INET6):
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: "
            f"{what} family={family} (unsupported family)"
        )
    if not isinstance(address, tuple) or not address:
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: {what} bad address={address!r}"
        )
    host = address[0]
    kind = _host_kind(str(host) if host is not None else None)
    if kind is not None:
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: host={kind} port={address[1]} {what}. "
            "Mock/fake this request instead."
        )


_orig_connect = socket.socket.connect
_orig_connect_ex = socket.socket.connect_ex
_orig_getaddrinfo = socket.getaddrinfo
_orig_gethostbyname = socket.gethostbyname
_orig_gethostbyname_ex = socket.gethostbyname_ex
_orig_proxy_env: dict[str, str | None] = {}


def _guarded_connect(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
    _check_target(family=getattr(self, "family", socket.AF_INET), address=address, what="connect")
    return _orig_connect(self, address, *args, **kwargs)


def _guarded_connect_ex(self: socket.socket, address: Any, *args: Any, **kwargs: Any) -> int:
    _check_target(family=getattr(self, "family", socket.AF_INET), address=address, what="connect_ex")
    return _orig_connect_ex(self, address, *args, **kwargs)


def _guarded_getaddrinfo(
    host: str | None,
    port: Any,
    family: int = 0,
    type_: int = 0,
    proto: int = 0,
    flags: int = 0,
) -> list[Any]:
    kind = _host_kind(str(host) if host is not None else None)
    if kind is not None:
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: host={kind} port={port} (DNS). "
            "Mock/fake this request instead."
        )
    return _orig_getaddrinfo(host, port, family, type_, proto, flags)


def _guarded_gethostbyname(hostname: str) -> str:
    kind = _host_kind(hostname)
    if kind is not None:
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: host={kind} (DNS). "
            "Mock/fake this request instead."
        )
    return _orig_gethostbyname(hostname)


def _guarded_gethostbyname_ex(hostname: str) -> tuple[str, list[str], list[str]]:
    kind = _host_kind(hostname)
    if kind is not None:
        raise ExternalNetworkBlocked(
            f"External network access is forbidden in tests: host={kind} (DNS). "
            "Mock/fake this request instead."
        )
    return _orig_gethostbyname_ex(hostname)


_installed = False


def install_offline_guard() -> None:
    """安装 fail-closed guard（幂等）。在 pytest_configure 中最早调用。"""
    global _installed
    if _installed:
        return
    _installed = True
    socket.socket.connect = _guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = _guarded_connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = _guarded_getaddrinfo
    socket.gethostbyname = _guarded_gethostbyname
    socket.gethostbyname_ex = _guarded_gethostbyname_ex
    _strip_proxy_env()


def strip_proxy_env() -> dict[str, str | None]:
    """仅在本测试进程内移除代理环境变量；返回原值以便恢复。

    同时弹出大小写两种写法（Windows 环境变量大小写不敏感），
    统一以 lowercase 规范名保存，避免恢复时二次写入互相覆盖。
    """
    saved: dict[str, str | None] = {}
    for upper, lower in _PROXY_PAIRS:
        value = os.environ.pop(upper, None)
        if value is None:
            value = os.environ.pop(lower, None)
        else:
            os.environ.pop(lower, None)
        saved[lower] = value
    return saved


def _strip_proxy_env() -> None:
    _orig_proxy_env.update(strip_proxy_env())


def restore_proxy_env() -> None:
    for key, value in _orig_proxy_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    _orig_proxy_env.clear()


def guard_active() -> bool:
    return _installed


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]
