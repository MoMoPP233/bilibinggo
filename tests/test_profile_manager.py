from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src import data_paths, profile_manager
from src.db.engine import db_path, reset_engine_for_tests


@pytest.fixture
def profile_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "binggo-data"
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(root))
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    data_paths.reset_runtime_profile_for_tests()
    reset_engine_for_tests()
    yield root
    reset_engine_for_tests()
    data_paths.reset_runtime_profile_for_tests()


def _write_database_marker(value: str) -> Path:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE profile_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO profile_marker (value) VALUES (?)", (value,))
    return path


def _read_database_markers(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT value FROM profile_marker ORDER BY rowid").fetchall()
    return [str(row[0]) for row in rows]


def _restart_profile_runtime() -> None:
    reset_engine_for_tests()
    data_paths.reset_runtime_profile_for_tests()


def test_account_1_and_account_2_have_private_data_and_shared_llm(
    profile_data_root: Path,
) -> None:
    assert profile_manager.list_profiles() == [
        {"profile_id": "account-1", "mid": "", "nickname": ""}
    ]
    assert profile_manager.create_profile() == {
        "profile_id": "account-2",
        "mid": "",
        "nickname": "",
    }

    account_1_database = data_paths.get_database_path("account-1")
    account_2_database = data_paths.get_database_path("account-2")
    account_1_cookie = data_paths.get_cookie_path("account-1")
    account_2_cookie = data_paths.get_cookie_path("account-2")
    shared_llm = data_paths.get_llm_env_path()

    assert account_1_database == profile_data_root / "profiles" / "account-1" / "binggo.db"
    assert account_2_database == profile_data_root / "profiles" / "account-2" / "binggo.db"
    assert account_1_database != account_2_database
    assert account_1_cookie != account_2_cookie
    assert account_1_cookie.parent == account_1_database.parent
    assert account_2_cookie.parent == account_2_database.parent
    assert shared_llm == profile_data_root / "shared" / "llm.env"
    assert not shared_llm.is_relative_to(account_1_database.parent)
    assert not shared_llm.is_relative_to(account_2_database.parent)

    account_1_cookie.write_text("SESSDATA=account-1\n", encoding="utf-8")
    account_2_cookie.write_text("SESSDATA=account-2\n", encoding="utf-8")
    shared_llm.write_text("OPENAI_API_KEY=shared\n", encoding="utf-8")
    assert account_1_cookie.read_text(encoding="utf-8") == "SESSDATA=account-1\n"
    assert account_2_cookie.read_text(encoding="utf-8") == "SESSDATA=account-2\n"
    assert data_paths.get_llm_env_path().read_text(encoding="utf-8") == "OPENAI_API_KEY=shared\n"


def test_active_profile_persists_but_database_switch_waits_for_restart_and_switches_back(
    profile_data_root: Path,
) -> None:
    profile_manager.list_profiles()
    profile_manager.create_profile()
    assert data_paths.get_runtime_profile_id() == "account-1"

    account_1_database = _write_database_marker("only-account-1")
    account_1_cookie = data_paths.get_cookie_path()
    account_1_cookie.write_text("SESSDATA=only-account-1\n", encoding="utf-8")
    selected = profile_manager.set_active_profile("account-2")
    assert selected["profile_id"] == "account-2"
    assert json.loads(
        (profile_data_root / "active_profile.json").read_text(encoding="utf-8")
    ) == {"profile_id": "account-2"}
    assert data_paths.get_selected_profile_id() == "account-2"
    assert data_paths.get_runtime_profile_id() == "account-1"
    assert db_path() == account_1_database

    _restart_profile_runtime()
    assert data_paths.get_runtime_profile_id() == "account-2"
    account_2_database = _write_database_marker("only-account-2")
    account_2_cookie = data_paths.get_cookie_path()
    account_2_cookie.write_text("SESSDATA=only-account-2\n", encoding="utf-8")
    assert account_2_database != account_1_database
    assert account_2_cookie != account_1_cookie
    assert _read_database_markers(account_1_database) == ["only-account-1"]
    assert _read_database_markers(account_2_database) == ["only-account-2"]
    assert account_1_cookie.read_text(encoding="utf-8") == "SESSDATA=only-account-1\n"
    assert account_2_cookie.read_text(encoding="utf-8") == "SESSDATA=only-account-2\n"

    profile_manager.set_active_profile("account-1")
    assert data_paths.get_selected_profile_id() == "account-1"
    assert data_paths.get_runtime_profile_id() == "account-2"
    assert db_path() == account_2_database

    _restart_profile_runtime()
    assert data_paths.get_runtime_profile_id() == "account-1"
    assert db_path() == account_1_database
    assert _read_database_markers(db_path()) == ["only-account-1"]
    assert _read_database_markers(account_2_database) == ["only-account-2"]
    assert data_paths.get_cookie_path() == account_1_cookie
    assert account_1_cookie.read_text(encoding="utf-8") == "SESSDATA=only-account-1\n"
    assert account_2_cookie.read_text(encoding="utf-8") == "SESSDATA=only-account-2\n"


def test_profile_metadata_and_delete_guards(profile_data_root: Path) -> None:
    profile_manager.list_profiles()
    profile_manager.create_profile()
    assert profile_manager.update_profile_metadata(
        "account-2", mid=" 42 ", nickname=" 第二账号 "
    ) == {"profile_id": "account-2", "mid": "42", "nickname": "第二账号"}
    assert [item["profile_id"] for item in profile_manager.list_profiles()] == [
        "account-1",
        "account-2",
    ]

    with pytest.raises(ValueError, match="当前正在使用"):
        profile_manager.delete_profile("account-1")

    profile_manager.set_active_profile("account-2")
    with pytest.raises(ValueError, match="下次启动"):
        profile_manager.delete_profile("account-2")

    profile_manager.set_active_profile("account-1")
    profile_manager.delete_profile("account-2")
    assert not (profile_data_root / "profiles" / "account-2").exists()
    assert [item["profile_id"] for item in profile_manager.list_profiles()] == ["account-1"]
