"""登录身份一致性保护（V8）：当前 Profile 扫码登录禁止漂移到其它 UID。

覆盖：
A. 同 UID 允许并正常保存 Cookie
B. UID 不一致拒绝，0 持久化
C. 扫码 UID 已属其它 Profile 拒绝
D. 拒绝发生在 save_cookies / mid 写入之前
E. Web 与 CLI 共用同一保护（login_with_qrcode 内置）
F. onboarding「扫码登录新账号」不受影响
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.bilibili_login import (
    BilibiliLoginError,
    POLL_SUCCESS,
    POLL_WAITING,
    login_with_qrcode,
    verify_login_uid_for_current_profile,
)


def _patch_profile(
    monkeypatch: pytest.MonkeyPatch,
    bound_mid: str,
    others: dict[str, str] | None = None,
) -> None:
    import src.profile_manager as pm

    monkeypatch.setattr(
        pm,
        "get_profile_metadata",
        lambda profile_id: {"profile_id": profile_id, "mid": bound_mid, "nickname": ""},
    )
    profiles = [
        {"profile_id": pid, "mid": mid, "nickname": ""}
        for pid, mid in (others or {}).items()
    ]
    monkeypatch.setattr(pm, "list_profiles", lambda: profiles)


def test_a_same_uid_allowed(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    assert verify_login_uid_for_current_profile({"DedeUserID": "111"}) == "111"


def test_b_mismatch_rejected_with_zero_persistence(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    saved: list[object] = []
    monkeypatch.setattr("src.bilibili_login.save_cookies", lambda s: saved.append(s))
    monkeypatch.setattr(
        "src.profile_manager.update_profile_metadata",
        lambda **values: saved.append(values),
    )
    with pytest.raises(BilibiliLoginError, match="扫码账号与当前 Profile 不一致"):
        verify_login_uid_for_current_profile({"DedeUserID": "222"})
    assert saved == []


def test_c_uid_belongs_to_other_profile_rejected(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111", others={"account-2": "222"})
    with pytest.raises(BilibiliLoginError, match="已绑定其它 Profile"):
        verify_login_uid_for_current_profile({"DedeUserID": "222"})


def test_missing_scanned_uid_rejected_when_profile_bound(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    with pytest.raises(BilibiliLoginError, match="无法从扫码结果获取账号 UID"):
        verify_login_uid_for_current_profile({"SESSDATA": "x"})


def test_fresh_profile_allows_first_bind(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="")
    assert verify_login_uid_for_current_profile({"DedeUserID": "333"}) == "333"


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self.headers = {}

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.cookies = type("Jar", (), {"jar": []})()
        self.poll_responses = [
            {"data": {"code": POLL_WAITING}},
            {"data": {"code": POLL_SUCCESS, "url": ""}},
        ]

    def get(self, url, **kwargs):
        if "generate" in url:
            return _FakeResponse(
                {"code": 0, "data": {"url": "https://example.com", "qrcode_key": "k1"}}
            )
        if "poll" in url:
            payload = (
                self.poll_responses.pop(0)
                if self.poll_responses
                else {"data": {"code": POLL_WAITING}}
            )
            return _FakeResponse(payload)
        return _FakeResponse({"code": 0})

    def close(self) -> None:
        return None


def _run_login(cookies: dict[str, str]) -> tuple[list[str], list[dict[str, str]]]:
    saved: list[str] = []
    synced: list[dict[str, str]] = []
    with (
        patch("src.bilibili_login.httpx.Client", _FakeClient),
        patch("src.bilibili_login._generate_qrcode_image", return_value=True),
        patch("src.bilibili_login._collect_login_cookies", return_value=cookies),
        patch("src.bilibili_login.save_cookies", lambda s: saved.append(s) or None),
        patch("src.bilibili_login._sync_profile_mid", lambda c: synced.append(c)),
    ):
        try:
            login_with_qrcode(open_image=False, timeout=10)
        except BilibiliLoginError:
            pass
    return saved, synced


def test_d_same_uid_full_flow_persists_cookie(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    saved, synced = _run_login(
        {"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "111"}
    )
    assert len(saved) == 1
    assert len(synced) == 1


def test_d_mismatch_full_flow_zero_persistence(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    saved, synced = _run_login(
        {"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "222"}
    )
    assert saved == []
    assert synced == []


def test_d_mismatch_full_flow_raises_clear_error(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    with patch("src.bilibili_login.httpx.Client", _FakeClient), patch(
        "src.bilibili_login._generate_qrcode_image", return_value=True
    ), patch(
        "src.bilibili_login._collect_login_cookies",
        return_value={"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "222"},
    ), patch("src.bilibili_login.save_cookies") as save_spy:
        with pytest.raises(BilibiliLoginError, match="扫码账号与当前 Profile 不一致"):
            login_with_qrcode(open_image=False, timeout=10)
    save_spy.assert_not_called()


def test_e_cli_path_uses_same_guard(monkeypatch) -> None:
    _patch_profile(monkeypatch, bound_mid="111")
    import scripts.bili_login as cli

    assert cli.login_with_qrcode is login_with_qrcode
    with patch("src.bilibili_login.httpx.Client", _FakeClient), patch(
        "src.bilibili_login._generate_qrcode_image", return_value=True
    ), patch(
        "src.bilibili_login._collect_login_cookies",
        return_value={"SESSDATA": "x", "bili_jct": "y", "DedeUserID": "222"},
    ), patch("src.bilibili_login.save_cookies") as save_spy:
        exit_code = cli._main_impl()
    assert exit_code == 1
    save_spy.assert_not_called()


def test_f_onboarding_uid_flow_unaffected() -> None:
    import src.login_onboarding as lo

    assert not hasattr(lo, "login_with_qrcode")
    assert not hasattr(lo, "verify_login_uid_for_current_profile")
    assert lo.uid_from_cookie("SESSDATA=x; DedeUserID=555") == "555"
