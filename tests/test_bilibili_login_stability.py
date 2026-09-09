from __future__ import annotations

import httpx
import pytest

from src.bilibili_login import (
    BilibiliLoginError,
    POLL_SCANNED,
    POLL_SUCCESS,
    POLL_WAITING,
    _poll_status,
    _sync_profile_mid,
    generate_qrcode,
    save_cookies,
)


def test_poll_status_prefers_data_code() -> None:
    assert _poll_status({"code": 0, "data": {"code": POLL_WAITING}}) == POLL_WAITING
    assert _poll_status({"code": -1, "data": {"code": POLL_SCANNED}}) == POLL_SCANNED
    assert _poll_status({"code": POLL_SUCCESS, "data": {}}) == POLL_SUCCESS


def test_poll_status_invalid_returns_negative_one() -> None:
    assert _poll_status({}) == -1
    assert _poll_status({"data": {"code": "bad"}}) == -1


def test_save_cookies_rejects_empty(tmp_path, monkeypatch) -> None:
    cookie_path = tmp_path / "cookies.txt"
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: cookie_path)

    with pytest.raises(BilibiliLoginError, match="空"):
        save_cookies("   ")


def test_save_cookies_rejects_missing_fields(tmp_path, monkeypatch) -> None:
    cookie_path = tmp_path / "cookies.txt"
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: cookie_path)
    with pytest.raises(BilibiliLoginError, match="bili_jct"):
        save_cookies("SESSDATA=only")


def test_save_cookies_writes_atomically(tmp_path, monkeypatch) -> None:
    cookie_path = tmp_path / "cookies.txt"
    monkeypatch.setattr("src.app_paths.cookie_file", lambda: cookie_path)

    saved = save_cookies("SESSDATA=abc; bili_jct=xyz")
    assert saved == cookie_path
    assert cookie_path.read_text(encoding="utf-8") == "SESSDATA=abc; bili_jct=xyz"
    assert not cookie_path.with_name("cookies.txt.tmp").exists()


def test_login_cookie_mid_updates_current_profile(monkeypatch) -> None:
    recorded: list[dict[str, str]] = []
    monkeypatch.setattr(
        "src.profile_manager.update_profile_metadata",
        lambda **values: recorded.append(values),
    )

    _sync_profile_mid({"DedeUserID": "123456"})

    assert recorded == [{"mid": "123456"}]


class _QrClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, *, params: dict[str, str]) -> httpx.Response:
        self.calls.append((url, params))
        return self.response


def _qr_response(status: int, payload: dict) -> httpx.Response:
    request = httpx.Request(
        "GET",
        "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
    )
    return httpx.Response(
        status,
        json=payload,
        headers={"content-type": "application/json"},
        request=request,
    )


def test_generate_qrcode_get_extracts_url_and_key() -> None:
    client = _QrClient(
        _qr_response(
            200,
            {"code": 0, "data": {"url": "https://qr.test/1", "qrcode_key": "key-1"}},
        )
    )

    assert generate_qrcode(client) == ("https://qr.test/1", "key-1")
    assert client.calls == [
        (
            "https://passport.bilibili.com/x/passport-login/web/qrcode/generate",
            {"source": "main-fe-header"},
        )
    ]


def test_generate_qrcode_non_2xx_keeps_status_code(caplog) -> None:
    client = _QrClient(_qr_response(412, {"code": -412, "message": "request blocked"}))

    with pytest.raises(BilibiliLoginError, match="HTTP 412"):
        generate_qrcode(client)

    assert "status=412" in caplog.text
    assert "host=passport.bilibili.com" in caplog.text
    assert "path=/x/passport-login/web/qrcode/generate" in caplog.text


def test_generate_qrcode_business_error_keeps_bilibili_code() -> None:
    client = _QrClient(_qr_response(200, {"code": -400, "message": "bad request"}))

    with pytest.raises(BilibiliLoginError, match="B站 code -400"):
        generate_qrcode(client)
