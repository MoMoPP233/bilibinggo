"""本地读取路径与远程请求性能优化 V3 测试（全 fake，零真实 Bilibili）。

覆盖：account 短 TTL hit/miss/expiry、login/logout/Cookie/-101 invalidate、
transient failure fallback 不续期、Profile 隔离、single-flight、
settings/cleanup/following-feed 页面读取 0 remote、危险删除仍保留实时身份预检。
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import app
from web.account_service import (
    ACCOUNT_PROFILE_TTL_SECONDS,
    clear_login_cookie,
    get_account_profile,
    invalidate_account_cache,
)

COOKIE_A = "SESSDATA=token-a; bili_jct=csrf-a; DedeUserID=11111"
COOKIE_B = "SESSDATA=token-b; bili_jct=csrf-b; DedeUserID=22222"


class _CountingClient:
    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request_json(self, url, params=None, *, referer=None, retries=0):
        type(self).calls += 1
        time.sleep(0.02)  # 模拟远程延迟，便于并发合并观察
        if "nav/stat" in url:
            return {"code": 0, "data": {"following": 10, "dynamic_count": 3}}
        return {
            "code": 0,
            "data": {
                "isLogin": True,
                "mid": 11111,
                "uname": "tester",
                "face": "https://example.com/a.jpg",
            },
        }


class _ExpiredClient:
    """按调用次数返回：第 1 次成功，之后抛网络错误（用于失败不续期测试）。"""

    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request_json(self, url, params=None, *, referer=None, retries=0):
        type(self).calls += 1
        if type(self).calls > 1:
            raise RuntimeError("网络请求失败: handshake timed out")
        if "nav/stat" in url:
            return {"code": 0, "data": {"following": 10, "dynamic_count": 3}}
        return {
            "code": 0,
            "data": {
                "isLogin": True,
                "mid": 11111,
                "uname": "tester",
                "face": "https://example.com/a.jpg",
            },
        }


class _InvalidClient:
    """第 2 次起返回 -101（明确登出）。"""

    calls: int = 0

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def request_json(self, url, params=None, *, referer=None, retries=0):
        type(self).calls += 1
        if type(self).calls >= 2:
            return {"code": -101, "data": {}, "message": "未登录"}
        if "nav/stat" in url:
            return {"code": 0, "data": {"following": 10, "dynamic_count": 3}}
        return {
            "code": 0,
            "data": {
                "isLogin": True,
                "mid": 11111,
                "uname": "tester",
                "face": "https://example.com/a.jpg",
            },
        }


@pytest.fixture
def cookie_a_path(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "profile-a" / "cookies.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COOKIE_A, encoding="utf-8")
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: path)
    invalidate_account_cache()
    return path


class _Clock:
    """可控 monotonic 时钟。"""

    def __init__(self) -> None:
        self.base = time.monotonic()
        self.offset = 0.0

    def now(self) -> float:
        return self.base + self.offset


def test_account_ttl_hit_only_first_call_fetches(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    first = get_account_profile()
    second = get_account_profile()
    assert first["logged_in"] is True
    assert second["logged_in"] is True
    assert _CountingClient.calls == 2


def test_account_ttl_expiry_allows_remote_refresh(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    clock = _Clock()
    monkeypatch.setattr("web.account_service.time.monotonic", clock.now)
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    get_account_profile()
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 0.5
    refreshed = get_account_profile()
    assert refreshed["logged_in"] is True
    assert _CountingClient.calls == 4


def test_logout_invalidates_success_cache(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    get_account_profile()
    clear_login_cookie()
    # cookie 已删除：立即不再显示登录，且不需要任何远程请求。
    profile = get_account_profile()
    assert profile["logged_in"] is False
    assert _CountingClient.calls == 2


def test_cookie_update_invalidates_cache(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    get_account_profile()
    # 模拟登录/换号重写 cookie（内容与大小都变化 → 指纹失效）。
    cookie_a_path.write_text(COOKIE_B + "   ", encoding="utf-8")
    again = get_account_profile()
    assert again["logged_in"] is True
    assert _CountingClient.calls == 4


def test_confirmed_101_invalidates_and_never_returns_stale_login(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    clock = _Clock()
    monkeypatch.setattr("web.account_service.time.monotonic", clock.now)
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _InvalidClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    monkeypatch.setattr(
        "web.account_service._load_account_cache",
        lambda: {"uname": "old_user", "mid": 11111},
    )
    first = get_account_profile()
    assert first["logged_in"] is True
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 1.0
    second = get_account_profile()
    assert second["logged_in"] is False
    assert "过期" in second["message"]
    # -101 后旧成功快照被清：后续仍然远程验证，而不是继续显示“已登录”。
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 2.0
    third = get_account_profile()
    assert third["logged_in"] is False
    assert _InvalidClient.calls == 4


def test_transient_failure_fallback_does_not_extend_verified_time(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    clock = _Clock()
    monkeypatch.setattr("web.account_service.time.monotonic", clock.now)
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _ExpiredClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    monkeypatch.setattr(
        "web.account_service._load_account_cache",
        lambda: {"uname": "cached", "mid": 11111},
    )
    first = get_account_profile()  # 真实成功（verified_at = t0）
    assert first["logged_in"] is True
    # TTL 内直接命中成功快照（无远程、无失败语义）。
    clock.offset = 1.0
    inside_ttl = get_account_profile()
    assert inside_ttl["logged_in"] is True
    assert inside_ttl.get("network_error") is None
    # TTL 到期后的远程失败：回退旧可信快照（fallback），verified_at 不前移。
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 1.0
    failed = get_account_profile()
    assert failed["network_error"] is True
    # 再次越过原 verified_at 后仍然继续远程（证明失败没有把 TTL 续期）。
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 3.0
    expired_call = get_account_profile()
    assert expired_call["network_error"] is True
    assert _ExpiredClient.calls == 4


def test_profile_isolation_two_cookie_paths(tmp_path: Path, monkeypatch) -> None:
    path_a = tmp_path / "pa" / "cookies.txt"
    path_b = tmp_path / "pb" / "cookies.txt"
    for path in (path_a, path_b):
        path.parent.mkdir(parents=True, exist_ok=True)

    class _ProfileClient(_CountingClient):
        def request_json(self, url, params=None, *, referer=None, retries=0):
            type(self).calls += 1
            path = path_a  # 闭包外部决定当前 mid 由测试按调用设置
            mid = 11111
            uname = "user-a"
            if type(self)._active is path_b:
                mid = 22222
                uname = "user-b"
            if "nav/stat" in url:
                return {"code": 0, "data": {"following": 1, "dynamic_count": 1}}
            return {
                "code": 0,
                "data": {"isLogin": True, "mid": mid, "uname": uname, "face": ""},
            }

    _ProfileClient._active = path_a
    monkeypatch.setattr("web.account_service.BilibiliClient", _ProfileClient)
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)

    path_a.write_text(COOKIE_A, encoding="utf-8")
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: path_a)
    first = get_account_profile()
    assert first["uname"] == "user-a"

    path_b.write_text(COOKIE_B, encoding="utf-8")
    _ProfileClient._active = path_b
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: path_b)
    second = get_account_profile()
    assert second["uname"] == "user-b"  # B 不能用 A 的快照


def test_concurrent_miss_single_flight(
    cookie_a_path: Path, monkeypatch,
) -> None:

    _CountingClient.calls = 0
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(get_account_profile())
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors
    assert all(item["logged_in"] is True for item in results)
    assert _CountingClient.calls == 2


# ---------- 页面读取 0 remote ----------


@pytest.fixture
def local_cookie_env(tmp_path: Path, monkeypatch) -> Path:
    path = tmp_path / "local" / "cookies.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COOKIE_A, encoding="utf-8")
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: path)
    return path


def _assert_no_remote_client(monkeypatch) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("本地页面读取不应创建 BilibiliClient")

    monkeypatch.setattr("web.account_service.BilibiliClient", forbidden)
    monkeypatch.setattr("src.bilibili_client.BilibiliClient", forbidden)


@pytest.mark.parametrize("path", ["/api/settings", "/api/settings/llm"])
def test_settings_endpoints_zero_remote(
    isolated_home: Path, local_cookie_env: Path, monkeypatch, path: str,
) -> None:
    _assert_no_remote_client(monkeypatch)
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 200
    body = resp.json()
    assert "llm" in body


@pytest.mark.parametrize(
    "path",
    [
        "/api/repost-cleanup/history",
        "/api/repost-cleanup/candidates",
        "/api/repost-cleanup/health",
        "/api/auto/following-feed",
    ],
)
def test_local_page_reads_zero_remote(
    isolated_home: Path, local_cookie_env: Path, monkeypatch, path: str,
) -> None:
    _assert_no_remote_client(monkeypatch)
    client = TestClient(app)
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_delete_preflight_keeps_remote_account_verification(
    isolated_home: Path, local_cookie_env: Path, monkeypatch,
) -> None:
    """危险删除入口绝不改成本地身份：远程 NAV 预检必须保留。"""
    client = TestClient(app)
    with patch(
        "web.app.get_account_profile",
        return_value={"logged_in": True, "mid": 11111},
    ) as profile_spy, patch(
        "web.app.runner.try_start",
        return_value=None,  # 模拟已有任务：预检发生在 busy 判断之前
    ):
        resp = client.post(
            "/api/repost-cleanup/delete",
            json={
                "repost_dynamic_ids": ["5000000000000000001"],
                "confirmed": True,
                "manual_review_confirmed": False,
            },
        )
    assert resp.status_code == 409
    profile_spy.assert_called_once()


# ---------- 用户主动“刷新账号” = force 绕过 TTL ----------


def test_force_refresh_bypasses_ttl_and_refreshes_success_cache(
    cookie_a_path: Path, monkeypatch,
) -> None:
    _CountingClient.calls = 0
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _CountingClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    get_account_profile()  # 普通第一次：真实验证（nav+stat）
    get_account_profile()  # TTL 内普通调用：0 新远程
    assert _CountingClient.calls == 2
    forced = get_account_profile(force=True)  # TTL 仍有效但主动刷新：真实验证
    assert forced["logged_in"] is True
    assert _CountingClient.calls == 4
    get_account_profile()  # force 成功后普通调用命中新 cache
    assert _CountingClient.calls == 4


def test_force_101_invalidates_success_cache_immediately(
    cookie_a_path: Path, monkeypatch,
) -> None:
    _InvalidClient.calls = 0
    clock = _Clock()
    monkeypatch.setattr("web.account_service.time.monotonic", clock.now)
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _InvalidClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    normal = get_account_profile()
    assert normal["logged_in"] is True
    # force 主动刷新 → 明确 -101：旧成功 cache 立即失效，返回 confirmed logout。
    forced = get_account_profile(force=True)
    assert forced["logged_in"] is False
    assert "过期" in forced["message"]
    # 失效后普通调用也真实远程（TTL 内也不复用旧快照）。
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 1.0
    after = get_account_profile()
    assert after["logged_in"] is False
    assert _InvalidClient.calls == 4


def test_force_transient_error_does_not_move_verified_at(
    cookie_a_path: Path, monkeypatch,
) -> None:
    _ExpiredClient.calls = 0
    clock = _Clock()
    monkeypatch.setattr("web.account_service.time.monotonic", clock.now)
    monkeypatch.setattr(
        "web.account_service.BilibiliClient",
        _ExpiredClient,
    )
    monkeypatch.setattr("src.profile_manager.update_profile_metadata", lambda **k: None)
    monkeypatch.setattr(
        "web.account_service._load_account_cache",
        lambda: {"uname": "cached", "mid": 11111},
    )
    first = get_account_profile()  # 真实成功（verified_at=t0）
    assert first["logged_in"] is True
    # force 刷新时网络失败：不假 logout、不把失败当成功缓存（verified_at 不前移）。
    clock.offset = 1.0
    forced = get_account_profile(force=True)
    assert forced["network_error"] is True
    assert forced["cookie_saved"] is True
    # 过了原 verified_at 后普通调用仍会真实远程（证明失败没有续期）。
    clock.offset = ACCOUNT_PROFILE_TTL_SECONDS + 2.0
    after = get_account_profile()
    assert after["network_error"] is True
    assert _ExpiredClient.calls == 4
