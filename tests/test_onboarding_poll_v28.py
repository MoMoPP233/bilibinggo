"""V28：M7 onboarding QR poll 修复专项（全离线：MockTransport/fixture，零真实网络）。

覆盖 CASE 1-18：
- 共享 poll 请求语义（PASSPORT_HEADERS/query/timeout/有限重试/4xx 分类/协议校验）
- 状态语义（waiting/scanned/success/expired/unknown）
- 错误分类 + 脱敏日志（无 qrcode_key/Cookie）
- Cookie 完整性 + commit 链
- 路由 HTTP 状态映射（404/400/503/502）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from src import bilibili_login as blogin
from src.login_onboarding import (
    OnboardingError,
    onboarding_complete,
    onboarding_create,
    onboarding_poll,
    onboarding_start_remote,
)
from src.profile_manager import list_profiles
from web.app import app

route_client = TestClient(app)

COOKIE_SET_HEADERS = [
    ("set-cookie", "SESSDATA=sess-1; Path=/; Domain=.bilibili.com"),
    ("set-cookie", "bili_jct=jct-1; Path=/; Domain=.bilibili.com"),
    ("set-cookie", "DedeUserID=90001; Path=/; Domain=.bilibili.com"),
    ("set-cookie", "DedeUserID__ckMd5=ck-1; Path=/; Domain=.bilibili.com"),
]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from src import data_paths

    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}),
        encoding="utf-8",
    )
    (account / "cookies.txt").write_text("SESSDATA=old;DedeUserID=88001", encoding="utf-8")
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    data_paths.get_runtime_profile_id()
    monkeypatch.setattr(blogin, "QR_POLL_RETRY_BACKOFF_SECONDS", 0.0)
    yield base
    data_paths.reset_runtime_profile_for_tests()


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """让 onboarding 使用共享 factory 的离线 MockTransport client。"""

    def factory(*, timeout: float = blogin.PASSPORT_TIMEOUT_SECONDS) -> httpx.Client:
        return httpx.Client(
            headers=blogin.PASSPORT_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(blogin, "create_passport_client", factory)


def _session_file(root: Path, onboarding_id: str) -> dict:
    return json.loads(
        (root / "shared" / "onboarding" / f"{onboarding_id}.json").read_text(encoding="utf-8")
    )


# ---------- CASE 1-3：QR 创建与共享请求语义 ----------


def test_case1_start_remote_url_and_key_from_same_response(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        blogin,
        "generate_qrcode",
        lambda: ("https://passport.bilibili.com/h5-app/passport/login/scan?x=1", "key-1"),
    )
    result = onboarding_start_remote()
    staged_path = root / "shared" / "onboarding" / f"{result['onboarding_id']}.json"
    staged_text = staged_path.read_text(encoding="utf-8")
    # V31：普通 session JSON 不含 qr_url/qr_key 明文
    assert "scan?x=1" not in staged_text
    assert "key-1" not in staged_text
    assert '"qr_url"' not in staged_text
    assert '"qr_key"' not in staged_text
    assert json.loads(staged_text)["has_qr_key"] is True
    secret = root / "shared" / "onboarding" / ".secrets" / f"{result['onboarding_id']}.qr_key"
    assert secret.read_text(encoding="utf-8") == "key-1"
    assert "cookie" not in result
    assert "qrcode_key" not in result
    assert "qr_url" not in result


def test_case2_poll_request_carries_qrcode_key(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"code": blogin.POLL_WAITING}})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="key-42")
    onboarding_poll(session["onboarding_id"])
    assert seen["qrcode_key"] == "key-42"
    assert seen["source"] == "main-fe-header"


def test_case3_poll_uses_shared_passport_headers(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return httpx.Response(200, json={"data": {"code": blogin.POLL_WAITING}})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    onboarding_poll(session["onboarding_id"])
    assert seen["user-agent"].startswith("Mozilla/5.0")
    assert seen["referer"] == "https://www.bilibili.com/"
    assert seen["origin"] == "https://www.bilibili.com"


def test_create_passport_client_has_passport_headers_and_redirects() -> None:
    client = blogin.create_passport_client()
    try:
        assert client.headers["User-Agent"].startswith("Mozilla/5.0")
        assert client.headers["Referer"] == "https://www.bilibili.com/"
        assert client.headers["Origin"] == "https://www.bilibili.com"
        assert client.follow_redirects is True
    finally:
        client.close()


# ---------- CASE 4-7：状态语义 ----------


def test_case4_waiting_is_not_an_error(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": {"code": 86101}}))
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "waiting"
    assert _session_file(root, session["onboarding_id"])["status"] == "waiting"


def test_case5_scanned_waits_for_phone_confirmation(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": {"code": 86090}}))
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "scanned"
    assert _session_file(root, session["onboarding_id"])["status"] == "waiting"


def test_case6_success_stages_cookie_header_string(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": {"code": 0, "url": ""}}, headers=COOKIE_SET_HEADERS
        ),
    )
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "success"
    staged_text = (
        root / "shared" / "onboarding" / f"{session['onboarding_id']}.json"
    ).read_text(encoding="utf-8")
    # V31：Cookie 不进普通 JSON；只记 has_cookie
    assert "SESSDATA" not in staged_text
    assert "bili_jct" not in staged_text
    assert "DedeUserID" not in staged_text
    assert '"cookie"' not in staged_text
    staged = json.loads(staged_text)
    assert staged["status"] == "scanned"
    assert staged["has_cookie"] is True
    cookie = (
        root / "shared" / "onboarding" / ".secrets" / f"{session['onboarding_id']}.cookie"
    ).read_text(encoding="utf-8")
    assert isinstance(cookie, str)
    assert "SESSDATA=sess-1" in cookie
    assert "DedeUserID=90001" in cookie
    assert "{'SESSDATA'" not in cookie  # 不得把 dict repr 当成 Cookie header
    # 扫码成功后 qr_key secret 立即删除（gate 16）
    assert not (
        root / "shared" / "onboarding" / ".secrets" / f"{session['onboarding_id']}.qr_key"
    ).exists()


def test_case7_expired_marks_session_failed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": {"code": 86038}}))
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "expired"
    assert _session_file(root, session["onboarding_id"])["status"] == "failed"


# ---------- CASE 8-13：重试与分类 ----------


def test_case8_transport_error_retries_then_succeeds(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json={"data": {"code": 86101}})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "waiting"
    assert calls["n"] == 2


def test_case9_upstream_5xx_retries_then_succeeds(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502, json={"message": "bad gateway"})
        return httpx.Response(200, json={"data": {"code": 86101}})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    result = onboarding_poll(session["onboarding_id"])
    assert result["status"] == "waiting"
    assert calls["n"] == 2


def test_case10_persistent_transport_failure_is_network_error(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("down", request=request)

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="key-secret-123")
    with caplog.at_level(logging.WARNING, logger="binggo.login_onboarding"):
        with pytest.raises(OnboardingError) as exc:
            onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_network_error"
    assert calls["n"] == 3
    # CASE 16：脱敏日志包含 type/attempt，不含 qrcode_key/Cookie
    assert "type=ConnectError" in caplog.text
    assert "attempt=3" in caplog.text
    assert session["onboarding_id"][:8] in caplog.text
    assert "key-secret-123" not in caplog.text
    assert "SESSDATA" not in caplog.text


def test_case11_persistent_5xx_is_upstream_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={"message": "unavailable"})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_upstream_error"
    assert calls["n"] == 3


def test_case12_http_4xx_is_not_retried(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(412, json={"message": "blocked"})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with caplog.at_level(logging.WARNING, logger="binggo.login_onboarding"):
        with pytest.raises(OnboardingError) as exc:
            onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_http_error"
    assert calls["n"] == 1
    assert "http_status=412" in caplog.text
    assert "type=HTTPStatusError" in caplog.text


def test_case13_non_json_response_is_protocol_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, text="<html>blocked</html>", headers={"content-type": "text/html"})

    _install_transport(monkeypatch, handler)
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_protocol_error"
    assert calls["n"] == 1


# ---------- CASE 14-15：未知/缺失状态不无限等待 ----------


def test_case14_unknown_business_code_stops_polling(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch, lambda request: httpx.Response(200, json={"data": {"code": 424242}})
    )
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_protocol_error"
    assert _session_file(root, session["onboarding_id"])["status"] == "failed"
    # 后续 poll 返回 failed 终态，而不是 waiting
    follow_up = onboarding_poll(session["onboarding_id"])
    assert follow_up["status"] == "failed"


@pytest.mark.parametrize("payload", [{"data": {}}, {}])
def test_case15_missing_status_is_protocol_error(
    root: Path, monkeypatch: pytest.MonkeyPatch, payload: dict
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json=payload))
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "poll_protocol_error"


# ---------- CASE 17-18：Cookie 完整性与 commit 链 ----------


def test_case17_complete_chain_creates_profile(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": {"code": 0, "url": ""}}, headers=COOKIE_SET_HEADERS
        ),
    )
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    assert onboarding_poll(session["onboarding_id"])["status"] == "success"
    result = onboarding_complete(session["onboarding_id"], nickname="n1")
    assert result["created_new"] is True
    cookie_file = root / "profiles" / "account-2" / "cookies.txt"
    assert cookie_file.read_text(encoding="utf-8").startswith("SESSDATA=sess-1")


def test_case18_incomplete_cookie_fails_without_profile(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = [h for h in COOKIE_SET_HEADERS if "DedeUserID=" not in h[1]]
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": {"code": 0, "url": ""}}, headers=headers
        ),
    )
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="k")
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(session["onboarding_id"])
    assert exc.value.code == "cookie_incomplete"
    staged = _session_file(root, session["onboarding_id"])
    # V31：cookie incomplete 为终态失败，且不留下 cookie/qr_key secret
    assert staged["status"] == "failed"
    assert staged["has_cookie"] is False
    secrets_dir = root / "shared" / "onboarding" / ".secrets"
    assert not (secrets_dir / f"{session['onboarding_id']}.cookie").exists()
    assert not (secrets_dir / f"{session['onboarding_id']}.qr_key").exists()
    assert [p["profile_id"] for p in list_profiles()] == ["account-1"]


# ---------- 路由 HTTP 状态映射 ----------


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("session_missing", 404),
        ("session_expired", 404),
        ("session_closed", 400),
        ("session_incomplete", 400),
        ("poll_network_error", 503),
        ("poll_upstream_error", 503),
        ("poll_http_error", 502),
        ("poll_protocol_error", 502),
        ("cookie_incomplete", 502),
    ],
)
def test_poll_route_maps_onboarding_error_codes(
    monkeypatch: pytest.MonkeyPatch, code: str, expected_status: int
) -> None:
    import src.login_onboarding as lo

    def boom(_onboarding_id: str):
        raise lo.OnboardingError(code, f"boom-{code}")

    monkeypatch.setattr(lo, "onboarding_poll", boom)
    resp = route_client.post("/api/accounts/onboarding/xyz/poll")
    assert resp.status_code == expected_status
    assert resp.json()["error"]["message"] == f"boom-{code}"


def test_onboarding_routes_do_not_require_login(monkeypatch: pytest.MonkeyPatch) -> None:
    import src.login_onboarding as lo

    monkeypatch.setattr(
        lo,
        "onboarding_poll",
        lambda _onboarding_id: {"onboarding_id": "x", "status": "waiting"},
    )
    resp = route_client.post("/api/accounts/onboarding/x/poll")
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "result": {"onboarding_id": "x", "status": "waiting"},
    }


# ---------- legacy 回归：共享 helper 委托 ----------


def test_legacy_poll_request_delegates_to_shared_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, str] = {}

    def fake_shared(client, qrcode_key, **kwargs):
        captured["key"] = qrcode_key
        return {"data": {"code": 86101}}, object()

    monkeypatch.setattr(blogin, "poll_qrcode_request", fake_shared)
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    try:
        body, _ = blogin._poll_request(client, "k-legacy")
    finally:
        client.close()
    assert captured["key"] == "k-legacy"
    assert body == {"data": {"code": 86101}}


def test_shared_helper_4xx_category_and_no_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, json={"message": "forbidden"})

    monkeypatch.setattr(blogin, "QR_POLL_RETRY_BACKOFF_SECONDS", 0.0)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(blogin.QrPollError) as exc:
            blogin.poll_qrcode_request(client, "k")
    finally:
        client.close()
    assert exc.value.category == "http_error"
    assert exc.value.http_status == 403
    assert exc.value.attempts == 1
    assert calls["n"] == 1
