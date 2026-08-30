from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from src import data_paths

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.app_paths import (
    app_bundle_root,
    ensure_user_dirs,
    install_root,
    is_frozen,
    platform_label,
    user_home,
)


def test_user_home_override(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.setenv("BINGGO_HOME", str(tmp_path))
    assert user_home() == tmp_path


def test_dev_mode_user_home_chooses_and_persists_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    selected = tmp_path / "chosen-data"
    locator = tmp_path / "locator.json"
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(locator))
    monkeypatch.setattr("src.data_paths._is_frozen", lambda: False)
    monkeypatch.setattr("src.data_paths._choose_data_root", lambda: selected)

    assert user_home() == selected.resolve()
    assert locator.is_file()
    assert json.loads(locator.read_text(encoding="utf-8")) == {
        "data_root": str(selected.resolve())
    }
    assert data_paths.get_data_root() == selected.resolve()


def test_frozen_installed_user_home_uses_persisted_data_root(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    selected = tmp_path / "selected-data"
    locator = tmp_path / "appdata" / "Binggo" / "data_root.json"
    locator.parent.mkdir(parents=True)
    locator.write_text(json.dumps({"data_root": str(selected)}), encoding="utf-8")
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(locator))
    monkeypatch.setattr("src.data_paths._is_frozen", lambda: True)
    assert user_home() == selected.resolve()


def test_frozen_portable_user_home(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    monkeypatch.setenv("BINGGO_PORTABLE", "1")
    monkeypatch.setattr("src.data_paths._portable_root", lambda: tmp_path)
    assert user_home() == tmp_path


def test_frozen_installed_user_home_requires_data_root_selection(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "missing-locator.json"))
    monkeypatch.setattr("src.data_paths._is_frozen", lambda: True)
    with pytest.raises(data_paths.DataRootNotConfiguredError, match="尚未选择"):
        user_home()


def test_frozen_darwin_portable_uses_app_parent(monkeypatch, tmp_path):
    monkeypatch.delenv("BINGGO_DATA_ROOT", raising=False)
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    monkeypatch.setenv("BINGGO_PORTABLE", "1")
    monkeypatch.setattr(sys, "platform", "darwin")
    fake_exe = tmp_path / "Binggo.app" / "Contents" / "MacOS" / "Binggo"
    fake_exe.parent.mkdir(parents=True)
    fake_exe.write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(fake_exe))
    assert app_bundle_root() == tmp_path / "Binggo.app"
    assert user_home() == tmp_path


def test_platform_label(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    assert platform_label() == "windows"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert platform_label() == "macos"


def test_ensure_user_dirs_seeds_examples(monkeypatch, tmp_path):
    monkeypatch.setenv("BINGGO_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("BINGGO_HOME", raising=False)
    data_paths.reset_runtime_profile_for_tests()
    monkeypatch.setattr("src.app_paths.install_root", lambda: ROOT)
    monkeypatch.setattr("src.app_paths._SEEDED", False)
    monkeypatch.setattr("src.app_paths._BOOTSTRAPPED", False)
    from src.db.engine import reset_engine_for_tests

    reset_engine_for_tests()
    ensure_user_dirs()
    profile_dir = tmp_path / "profiles" / "account-1"
    assert (profile_dir / "cookies.txt.example").exists()
    assert (profile_dir / "sources.yaml").exists()
    assert (profile_dir / "logs").is_dir()
    assert (tmp_path / "shared" / "llm.env.example").exists()
    reset_engine_for_tests()
    data_paths.reset_runtime_profile_for_tests()


def test_is_frozen_false_in_pytest():
    assert is_frozen() is False
    assert install_root() == ROOT


def test_dashboard_assert_loopback() -> None:
    from src.dashboard_server import assert_loopback_host

    assert_loopback_host("127.0.0.1")
    assert_loopback_host("localhost")
    with pytest.raises(RuntimeError, match="loopback"):
        assert_loopback_host("0.0.0.0")
