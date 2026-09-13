"""V31：onboarding secret staging 专项（普通 session JSON 不含 Cookie/qr_key/qr_url 明文）。

全离线：temporary DATA_ROOT + httpx.MockTransport；只 mock 外部 Bilibili 请求。
覆盖 gate 41 的 CASE 1-18、21-22 与 write_text_secret 原语断言。
"""

from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time
from pathlib import Path

import httpx
import pytest

from src import bilibili_login as blogin
from src import login_onboarding as lo
from src.login_onboarding import (
    OnboardingError,
    onboarding_complete,
    onboarding_create,
    onboarding_poll,
    onboarding_qr_image_path,
    onboarding_sweep_expired,
    onboarding_store_scanned_cookie,
)
from src.profile_manager import list_profiles

QR_URL = "https://account.bilibili.com/h5/scan-web?token=qr-url-secret-777"
QR_KEY = "qr-key-secret-777"
COOKIE_HEADER = "SESSDATA=sess-1; bili_jct=jct-1; DedeUserID=90001; DedeUserID__ckMd5=ck-1"

COOKIE_HEADERS = [
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


def _onboarding_dir(root: Path) -> Path:
    return root / "shared" / "onboarding"


def _session_text(root: Path, onboarding_id: str) -> str:
    return (_onboarding_dir(root) / f"{onboarding_id}.json").read_text(encoding="utf-8")


def _secret(root: Path, onboarding_id: str, kind: str) -> Path:
    return _onboarding_dir(root) / ".secrets" / f"{onboarding_id}.{kind}"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    def factory(*, timeout: float = blogin.PASSPORT_TIMEOUT_SECONDS) -> httpx.Client:
        return httpx.Client(
            headers=blogin.PASSPORT_HEADERS,
            follow_redirects=True,
            timeout=timeout,
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(blogin, "create_passport_client", factory)


def _success_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, json={"data": {"code": 0, "url": ""}}, headers=COOKIE_HEADERS
    )


def _create(root: Path, *, qr_key: str = QR_KEY) -> str:
    session = onboarding_create(qr_url=QR_URL, qr_key=qr_key)
    return str(session["onboarding_id"])


# ---------- CASE 1-2：create 后 JSON 无 qr_key；sidecar 可读 ----------


def test_case1_create_session_json_has_no_secret(root: Path) -> None:
    session = onboarding_create(qr_url=QR_URL, qr_key=QR_KEY)
    onboarding_id = session["onboarding_id"]
    text = _session_text(root, onboarding_id)
    assert QR_KEY not in text
    assert "qr-url-secret-777" not in text
    assert '"qr_key"' not in text
    assert '"qr_url"' not in text
    assert '"cookie"' not in text
    assert "SESSDATA" not in text
    payload = json.loads(text)
    assert payload["status"] == "waiting"
    assert payload["has_qr_key"] is True
    assert payload["has_cookie"] is False
    assert "qr_url" not in session
    assert "qrcode_key" not in session


def test_case2_qr_key_readable_from_secret_sidecar(root: Path) -> None:
    onboarding_id = _create(root)
    sidecar = _secret(root, onboarding_id, "qr_key")
    assert sidecar.is_file()
    assert sidecar.read_text(encoding="utf-8") == QR_KEY
    # PNG 在 create 时已渲染（qr_url 不持久化）
    assert onboarding_qr_image_path(onboarding_id).is_file()


# ---------- CASE 3-4：poll 使用 sidecar；缺失 fail-closed 0 远程 ----------


def test_case3_poll_uses_qr_key_from_sidecar(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"data": {"code": 86101}})

    _install_transport(monkeypatch, handler)
    onboarding_id = _create(root)
    result = onboarding_poll(onboarding_id)
    assert result["status"] == "waiting"
    assert seen["qrcode_key"] == QR_KEY


def test_case4_missing_qr_key_fails_closed_without_remote(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarding_id = _create(root)
    _secret(root, onboarding_id, "qr_key").unlink()

    calls = {"n": 0}

    def factory(*, timeout: float = blogin.PASSPORT_TIMEOUT_SECONDS) -> httpx.Client:
        calls["n"] += 1
        return httpx.Client(transport=httpx.MockTransport(_success_handler))

    monkeypatch.setattr(blogin, "create_passport_client", factory)
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(onboarding_id)
    assert exc.value.code == "session_secret_missing"
    assert calls["n"] == 0  # 0 远程请求


# ---------- CASE 5-7：poll success 后 JSON 无 Cookie；sidecar canonical ----------


def test_case5_poll_success_session_json_has_no_cookie(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    assert onboarding_poll(onboarding_id)["status"] == "success"

    text = _session_text(root, onboarding_id)
    assert "SESSDATA" not in text
    assert "bili_jct" not in text
    assert "DedeUserID" not in text
    assert '"cookie"' not in text
    payload = json.loads(text)
    assert payload["status"] == "scanned"
    assert payload["has_cookie"] is True
    assert payload["has_qr_key"] is False
    # gate 16：success 后 qr_key sidecar 立即删除
    assert not _secret(root, onboarding_id, "qr_key").exists()


def test_case6_cookie_written_via_secure_primitive(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorded: list[tuple[str, str]] = []
    real_write = lo.write_text_secret

    def spy(path, text, **kwargs):
        recorded.append((Path(path).name, text))
        return real_write(path, text, **kwargs)

    monkeypatch.setattr(lo, "write_text_secret", spy)
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)

    names = [name for name, _value in recorded]
    assert f"{onboarding_id}.qr_key" in names
    assert f"{onboarding_id}.cookie" in names
    assert ("", "") not in recorded


def test_case7_cookie_sidecar_is_canonical_header(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)
    cookie = _secret(root, onboarding_id, "cookie").read_text(encoding="utf-8")
    assert cookie == COOKIE_HEADER
    assert "{" not in cookie and "'" not in cookie


# ---------- CASE 8-11：complete 读取 sidecar / 清理 / receipt / 幂等 ----------


def test_case8_complete_reads_cookie_sidecar(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)
    result = onboarding_complete(onboarding_id)
    assert result["profile_id"] == "account-2"
    cookie_file = root / "profiles" / "account-2" / "cookies.txt"
    assert cookie_file.read_text(encoding="utf-8") == COOKIE_HEADER


def test_case9_complete_removes_all_secrets_and_png(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)
    onboarding_complete(onboarding_id)
    assert not _secret(root, onboarding_id, "qr_key").exists()
    assert not _secret(root, onboarding_id, "cookie").exists()
    assert not (_onboarding_dir(root) / f"{onboarding_id}.png").exists()


def test_case10_completed_receipt_has_no_secret(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)
    result = onboarding_complete(onboarding_id, nickname="n1")
    text = _session_text(root, onboarding_id)
    for forbidden in (
        QR_KEY,
        "qr-url-secret-777",
        "sess-1",
        "jct-1",
        "90001",
        '"uid"',
        '"cookie"',
        '"qr_key"',
        '"qr_url"',
    ):
        assert forbidden not in text, forbidden
    assert "uid" not in result  # DedeUserID 不进入返回体
    assert result["profile_id"] == "account-2"


def test_case11_repeat_complete_does_not_need_secret(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, _success_handler)
    onboarding_id = _create(root)
    onboarding_poll(onboarding_id)
    first = onboarding_complete(onboarding_id)
    assert not _secret(root, onboarding_id, "cookie").exists()
    second = onboarding_complete(onboarding_id)
    assert second == first


# ---------- CASE 12-14：expired / protocol / cookie incomplete 清 secret ----------


def test_case12_expired_clears_secrets(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(monkeypatch, lambda request: httpx.Response(200, json={"data": {"code": 86038}}))
    onboarding_id = _create(root)
    result = onboarding_poll(onboarding_id)
    assert result["status"] == "expired"
    assert json.loads(_session_text(root, onboarding_id))["status"] == "failed"
    assert not _secret(root, onboarding_id, "qr_key").exists()
    assert not _secret(root, onboarding_id, "cookie").exists()


def test_case13_protocol_failure_clears_secrets(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>blocked</html>", headers={"content-type": "text/html"}),
    )
    onboarding_id = _create(root)
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(onboarding_id)
    assert exc.value.code == "poll_protocol_error"
    assert not _secret(root, onboarding_id, "qr_key").exists()
    assert not _secret(root, onboarding_id, "cookie").exists()


def test_case14_cookie_incomplete_leaves_no_cookie_secret(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    headers = [h for h in COOKIE_HEADERS if "DedeUserID=" not in h[1]]
    _install_transport(
        monkeypatch,
        lambda request: httpx.Response(200, json={"data": {"code": 0, "url": ""}}, headers=headers),
    )
    onboarding_id = _create(root)
    with pytest.raises(OnboardingError) as exc:
        onboarding_poll(onboarding_id)
    assert exc.value.code == "cookie_incomplete"
    assert not _secret(root, onboarding_id, "cookie").exists()
    assert not _secret(root, onboarding_id, "qr_key").exists()
    assert json.loads(_session_text(root, onboarding_id))["status"] == "failed"
    assert [p["profile_id"] for p in list_profiles()] == ["account-1"]


# ---------- CASE 15-16：TTL sweep / orphan secret ----------


def test_case15_ttl_sweep_clears_secrets(root: Path) -> None:
    onboarding_id = _create(root)
    session_path = _onboarding_dir(root) / f"{onboarding_id}.json"
    payload = json.loads(session_path.read_text(encoding="utf-8"))
    payload["created_at"] = int(time.time()) - (lo.SESSION_TTL_SECONDS + 120)
    session_path.write_text(json.dumps(payload), encoding="utf-8")

    removed = onboarding_sweep_expired()

    assert onboarding_id in removed
    assert not session_path.exists()
    assert not _secret(root, onboarding_id, "qr_key").exists()
    assert not _secret(root, onboarding_id, "cookie").exists()
    assert not (_onboarding_dir(root) / f"{onboarding_id}.png").exists()


def test_case16_orphan_secret_swept_only_after_ttl(root: Path) -> None:
    stale_id = "deadbeefdeadbeef"
    fresh_id = "feedfacefeedface"
    stale = lo._secret_path(stale_id, "qr_key")
    fresh = lo._secret_path(fresh_id, "qr_key")
    lo.write_text_secret(stale, "orphan-stale")
    lo.write_text_secret(fresh, "orphan-fresh")
    old = time.time() - (lo.SESSION_TTL_SECONDS + 300)
    os.utime(stale, (old, old))

    onboarding_sweep_expired()

    assert not stale.exists()
    assert fresh.exists()
    fresh.unlink()


# ---------- CASE 17-18：日志脱敏 ----------


def test_case17_logs_never_contain_qr_key(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    onboarding_id = _create(root)
    _secret(root, onboarding_id, "qr_key").unlink()
    with caplog.at_level(logging.WARNING, logger="binggo.login_onboarding"):
        with pytest.raises(OnboardingError):
            onboarding_poll(onboarding_id)
    assert "kind=qr_key" in caplog.text
    assert QR_KEY not in caplog.text


def test_case18_logs_never_contain_cookie(
    root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    onboarding_id = _create(root)
    onboarding_store_scanned_cookie(onboarding_id, COOKIE_HEADER, now=int(time.time()))
    _secret(root, onboarding_id, "cookie").unlink()
    with caplog.at_level(logging.WARNING, logger="binggo.login_onboarding"):
        with pytest.raises(OnboardingError) as exc:
            onboarding_complete(onboarding_id)
    assert exc.value.code == "session_secret_missing"
    assert "kind=cookie" in caplog.text
    assert "SESSDATA" not in caplog.text
    assert "sess-1" not in caplog.text


# ---------- CASE 21-22：cookie secret 缺失 complete fail-closed，pointer 不变 ----------


def test_case21_22_cookie_missing_complete_fail_closed_pointer_unchanged(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    onboarding_id = _create(root)
    onboarding_store_scanned_cookie(onboarding_id, COOKIE_HEADER, now=int(time.time()))
    _secret(root, onboarding_id, "cookie").unlink()

    with pytest.raises(OnboardingError) as exc:
        onboarding_complete(onboarding_id)
    assert exc.value.code == "session_secret_missing"
    assert not (root / "profiles" / "account-2").exists()
    pointer = json.loads((root / "active_profile.json").read_text(encoding="utf-8"))
    assert pointer["profile_id"] == "account-1"


# ---------- POSIX 权限（Windows 下 secure_files harden 为 no-op，跳过） ----------


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_secret_sidecar_not_world_readable(root: Path) -> None:
    onboarding_id = _create(root)
    mode = stat.S_IMODE(_secret(root, onboarding_id, "qr_key").stat().st_mode)
    assert mode & 0o077 == 0
