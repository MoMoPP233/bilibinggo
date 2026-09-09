"""Onboarding staging TTL 清理（V9）：过期敏感 staging 不残留，receipt 脱敏保留。

覆盖 A-I：过期 staged credential 删除 / QR PNG 删除 / 未过期保留 /
complete 后敏感 material 清理 / receipt 幂等 / receipt 无凭据 /
不触碰 Profile / 无线程 timer / M7 行为回归（由 M7 套件验证）。
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from src import data_paths
from src.login_onboarding import (
    SESSION_TTL_SECONDS,
    onboarding_complete,
    onboarding_create,
    onboarding_store_scanned_cookie,
    onboarding_sweep_expired,
)

COOKIE_A = "SESSDATA=abc;bili_jct=xyz;DedeUserID=90001;DedeUserID__ckMd5=x"


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    yield base
    data_paths.reset_runtime_profile_for_tests()


def _onboarding_dir(root: Path) -> Path:
    return root / "shared" / "onboarding"


def _create_session(root: Path, *, age_seconds: int = 0) -> str:
    session = onboarding_create(qr_url="https://qr.test/1", qr_key="secretkey")
    onboarding_id = session["onboarding_id"]
    if age_seconds:
        _age_session(root, onboarding_id, age_seconds)
    return onboarding_id


def _age_session(root: Path, onboarding_id: str, age_seconds: int) -> None:
    path = _onboarding_dir(root) / f"{onboarding_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = int(time.time()) - age_seconds
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_png(root: Path, onboarding_id: str) -> Path:
    png = _onboarding_dir(root) / f"{onboarding_id}.png"
    png.write_bytes(b"fake-png")
    return png


def test_a_expired_staged_session_credentials_deleted(root: Path) -> None:
    onboarding_id = _create_session(root, age_seconds=0)
    onboarding_store_scanned_cookie(
        onboarding_id, "SESSDATA=secret;bili_jct=secret2", now=int(time.time())
    )
    path = _onboarding_dir(root) / f"{onboarding_id}.json"
    assert "secret" in path.read_text(encoding="utf-8")
    _age_session(root, onboarding_id, SESSION_TTL_SECONDS + 120)

    removed = onboarding_sweep_expired()

    assert onboarding_id in removed
    assert not path.exists()


def test_b_expired_session_qr_png_deleted(root: Path) -> None:
    onboarding_id = _create_session(root, age_seconds=SESSION_TTL_SECONDS + 120)
    png = _write_png(root, onboarding_id)
    assert png.exists()

    onboarding_sweep_expired()

    assert not png.exists()


def test_c_fresh_session_kept(root: Path) -> None:
    onboarding_id = _create_session(root, age_seconds=0)
    onboarding_store_scanned_cookie(
        onboarding_id, "SESSDATA=secret;bili_jct=secret2", now=int(time.time())
    )
    path = _onboarding_dir(root) / f"{onboarding_id}.json"

    onboarding_sweep_expired()

    assert path.exists()


def test_d_complete_success_removes_sensitive_staging(root: Path, monkeypatch) -> None:
    import src.login_onboarding as lo

    fake_result = {
        "profile_id": "account-2",
        "uid": "90001",
        "display_name": "new",
        "created_new": True,
        "restart_required": True,
    }
    monkeypatch.setattr(lo, "commit_staged_login", lambda **kwargs: dict(fake_result))
    onboarding_id = onboarding_create(qr_url="https://qr.test/1", qr_key="secretkey")[
        "onboarding_id"
    ]
    onboarding_store_scanned_cookie(
        onboarding_id, COOKIE_A, now=int(time.time())
    )
    png = _write_png(root, onboarding_id)
    assert png.exists()

    result = onboarding_complete(onboarding_id)

    assert result == fake_result
    assert not png.exists()


def test_e_receipt_duplicate_complete_idempotent(root: Path, monkeypatch) -> None:
    import src.login_onboarding as lo

    fake_result = {
        "profile_id": "account-2",
        "uid": "90001",
        "display_name": "new",
        "created_new": True,
        "restart_required": True,
    }
    monkeypatch.setattr(lo, "commit_staged_login", lambda **kwargs: dict(fake_result))
    onboarding_id = onboarding_create(qr_url="https://qr.test/1", qr_key="secretkey")[
        "onboarding_id"
    ]
    onboarding_store_scanned_cookie(onboarding_id, COOKIE_A, now=int(time.time()))

    first = onboarding_complete(onboarding_id)
    second = onboarding_complete(onboarding_id)

    assert second == first == fake_result


def test_f_receipt_contains_no_credentials(root: Path, monkeypatch) -> None:
    import src.login_onboarding as lo

    fake_result = {
        "profile_id": "account-2",
        "uid": "90001",
        "display_name": "new",
        "created_new": True,
        "restart_required": True,
    }
    monkeypatch.setattr(lo, "commit_staged_login", lambda **kwargs: dict(fake_result))
    onboarding_id = onboarding_create(qr_url="https://qr.test/1", qr_key="secretkey")[
        "onboarding_id"
    ]
    onboarding_store_scanned_cookie(onboarding_id, COOKIE_A, now=int(time.time()))

    onboarding_complete(onboarding_id)

    path = _onboarding_dir(root) / f"{onboarding_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    for key in ("cookie", "qr_url", "qr_key"):
        assert key not in payload, key
    text = path.read_text(encoding="utf-8")
    assert "SESSDATA" not in text
    assert "bili_jct" not in text
    assert "secretkey" not in text


def test_g_sweep_never_touches_current_profile(root: Path) -> None:
    _create_session(root, age_seconds=SESSION_TTL_SECONDS + 120)
    profile_json = root / "profiles" / "account-1" / "profile.json"
    cookies = root / "profiles" / "account-1" / "cookies.txt"
    before_json = profile_json.read_text(encoding="utf-8")
    before_cookies = cookies.read_text(encoding="utf-8")

    onboarding_sweep_expired()

    assert profile_json.read_text(encoding="utf-8") == before_json
    assert cookies.read_text(encoding="utf-8") == before_cookies


def test_h_sweep_creates_no_threads_or_timers(root: Path) -> None:
    _create_session(root, age_seconds=SESSION_TTL_SECONDS + 120)
    before = threading.active_count()
    result = onboarding_sweep_expired()
    after = threading.active_count()
    assert isinstance(result, list)
    assert after == before
