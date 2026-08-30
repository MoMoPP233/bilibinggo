from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from src import data_paths


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Give profile/path tests a clean data root and runtime snapshot."""
    root = tmp_path / "binggo-data"
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(root))
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    data_paths.reset_runtime_profile_for_tests()
    yield root
    data_paths.reset_runtime_profile_for_tests()


def _create_marker_database(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE markers (value TEXT NOT NULL)")
        connection.execute("INSERT INTO markers (value) VALUES (?)", (value,))


def _marker_values(path: Path) -> list[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute("SELECT value FROM markers ORDER BY rowid").fetchall()
    return [str(row[0]) for row in rows]


def test_fresh_layout_uses_account_1_and_profile_scoped_paths(data_root: Path) -> None:
    assert data_paths.initialize_data_layout() == "account-1"

    account_dir = data_root / "profiles" / "account-1"
    assert data_paths.get_data_root() == data_root.resolve()
    assert data_paths.get_profiles_dir() == data_root / "profiles"
    assert data_paths.get_shared_dir() == data_root / "shared"
    assert data_paths.get_active_profile_path() == data_root / "active_profile.json"
    assert data_paths.get_profile_dir() == account_dir
    assert data_paths.get_database_path() == account_dir / "binggo.db"
    assert data_paths.get_cookie_path() == account_dir / "cookies.txt"
    assert data_paths.get_llm_env_path() == data_root / "shared" / "llm.env"
    assert json.loads(data_paths.get_active_profile_path().read_text(encoding="utf-8")) == {
        "profile_id": "account-1"
    }


def test_runtime_profile_and_data_root_are_frozen_for_process(
    data_root: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data_paths.initialize_data_layout()
    assert data_paths.get_runtime_profile_id() == "account-1"

    account_2 = data_root / "profiles" / "account-2"
    account_2.mkdir()
    (account_2 / "profile.json").write_text(
        json.dumps({"profile_id": "account-2", "mid": "", "nickname": ""}),
        encoding="utf-8",
    )
    data_paths.get_active_profile_path().write_text(
        json.dumps({"profile_id": "account-2"}), encoding="utf-8"
    )

    assert data_paths.get_selected_profile_id() == "account-2"
    assert data_paths.get_runtime_profile_id() == "account-1"
    assert data_paths.get_database_path() == data_root / "profiles" / "account-1" / "binggo.db"

    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(tmp_path / "another-root"))
    with pytest.raises(RuntimeError, match="运行期间发生变化"):
        data_paths.get_runtime_profile_id()


def test_uncreatable_chosen_root_does_not_persist_locator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    locator = tmp_path / "locator.json"
    parent_file = tmp_path / "not-a-directory"
    parent_file.write_text("block mkdir", encoding="utf-8")
    selected = parent_file / "binggo-data"
    monkeypatch.delenv(data_paths.DATA_ROOT_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(locator))

    with pytest.raises(data_paths.DataRootNotConfiguredError, match="不可用或不可写"):
        data_paths.ensure_data_root_selected(chooser=lambda: selected)

    assert not locator.exists()
    assert data_paths.DATA_ROOT_ENV not in os.environ


def test_legacy_database_and_cookie_migrate_once_without_removing_sources(
    data_root: Path,
) -> None:
    legacy_database = data_root / "data" / "binggo.db"
    legacy_cookie = data_root / "config" / "cookies.txt"
    legacy_llm = data_root / "config" / "llm.env"
    _create_marker_database(legacy_database, "legacy-original")
    legacy_cookie.parent.mkdir(parents=True, exist_ok=True)
    legacy_cookie.write_text(
        "SESSDATA=legacy-session; DedeUserID=24680; bili_jct=legacy-csrf\n",
        encoding="utf-8",
    )
    legacy_llm.write_text("OPENAI_API_KEY=legacy-key\n", encoding="utf-8")

    assert data_paths.initialize_data_layout() == "default"

    migrated_database = data_root / "profiles" / "default" / "binggo.db"
    migrated_cookie = data_root / "profiles" / "default" / "cookies.txt"
    migrated_llm = data_root / "shared" / "llm.env"
    assert _marker_values(migrated_database) == ["legacy-original"]
    assert migrated_cookie.read_bytes() == legacy_cookie.read_bytes()
    assert migrated_llm.read_bytes() == legacy_llm.read_bytes()
    assert json.loads(
        (data_root / "profiles" / "default" / "profile.json").read_text(encoding="utf-8")
    ) == {"profile_id": "default", "mid": "24680", "nickname": ""}
    assert data_paths.get_selected_profile_id() == "default"
    assert legacy_database.is_file()
    assert legacy_cookie.is_file()
    assert legacy_llm.is_file()

    # A later startup must not re-copy changed legacy data over live Profile data.
    connection = sqlite3.connect(migrated_database)
    try:
        connection.execute("INSERT INTO markers (value) VALUES ('profile-only')")
        connection.commit()
    finally:
        connection.close()
    connection = sqlite3.connect(legacy_database)
    try:
        connection.execute("INSERT INTO markers (value) VALUES ('legacy-later')")
        connection.commit()
    finally:
        connection.close()
    migrated_cookie.write_text("SESSDATA=profile-kept\n", encoding="utf-8")
    legacy_cookie.write_text("SESSDATA=legacy-changed\n", encoding="utf-8")
    migrated_llm.write_text("OPENAI_API_KEY=profile-kept\n", encoding="utf-8")
    legacy_llm.write_text("OPENAI_API_KEY=legacy-changed\n", encoding="utf-8")

    assert data_paths.initialize_data_layout() == "default"
    assert _marker_values(migrated_database) == ["legacy-original", "profile-only"]
    assert _marker_values(legacy_database) == ["legacy-original", "legacy-later"]
    assert migrated_cookie.read_text(encoding="utf-8") == "SESSDATA=profile-kept\n"
    assert legacy_cookie.read_text(encoding="utf-8") == "SESSDATA=legacy-changed\n"
    assert migrated_llm.read_text(encoding="utf-8") == "OPENAI_API_KEY=profile-kept\n"
    assert legacy_llm.read_text(encoding="utf-8") == "OPENAI_API_KEY=legacy-changed\n"

    # 删除当前 Profile 数据代表用户主动退出登录/重置；旧文件只保留作备份，
    # 后续启动不得再次把 Cookie、数据库或共享密钥复活。
    migrated_cookie.unlink()
    migrated_llm.unlink()
    assert data_paths.initialize_data_layout() == "default"
    assert not migrated_cookie.exists()
    assert not migrated_llm.exists()


def test_existing_profile_is_never_backfilled_from_retained_legacy_files(
    data_root: Path,
) -> None:
    legacy_database = data_root / "data" / "binggo.db"
    legacy_cookie = data_root / "config" / "cookies.txt"
    legacy_llm = data_root / "config" / "llm.env"
    _create_marker_database(legacy_database, "must-not-return")
    legacy_cookie.parent.mkdir(parents=True, exist_ok=True)
    legacy_cookie.write_text("SESSDATA=must-not-return\n", encoding="utf-8")
    legacy_llm.write_text("OPENAI_API_KEY=must-not-return\n", encoding="utf-8")
    profile = data_root / "profiles" / "default"
    profile.mkdir(parents=True)
    (profile / "profile.json").write_text(
        json.dumps({"profile_id": "default", "mid": "", "nickname": ""}),
        encoding="utf-8",
    )

    assert data_paths.initialize_data_layout() == "default"
    assert not (profile / "binggo.db").exists()
    assert not (profile / "cookies.txt").exists()
    assert not (data_root / "shared" / "llm.env").exists()


def test_legacy_database_and_cookie_never_mix_across_roots(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_other = tmp_path / "other-legacy-root"
    database = data_root / "data" / "binggo.db"
    other_cookie = legacy_other / "data" / "cookies.txt"
    _create_marker_database(database, "database-account-a")
    other_cookie.parent.mkdir(parents=True)
    other_cookie.write_text(
        "SESSDATA=account-b; DedeUserID=22222\n", encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(legacy_other))

    assert data_paths.initialize_data_layout() == "default"

    migrated = data_root / "profiles" / "default"
    assert _marker_values(migrated / "binggo.db") == ["database-account-a"]
    assert not (migrated / "cookies.txt").exists()
    assert json.loads((migrated / "profile.json").read_text(encoding="utf-8"))["mid"] == ""
    assert other_cookie.read_text(encoding="utf-8").startswith("SESSDATA=account-b")


def test_complete_legacy_account_root_wins_over_earlier_partial_root(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    partial_cookie = data_root / "config" / "cookies.txt"
    partial_cookie.parent.mkdir(parents=True)
    partial_cookie.write_text("SESSDATA=stale-partial\n", encoding="utf-8")
    complete_root = tmp_path / "complete-legacy-root"
    complete_database = complete_root / "data" / "binggo.db"
    complete_cookie = complete_root / "config" / "cookies.txt"
    _create_marker_database(complete_database, "complete-account")
    complete_cookie.parent.mkdir(parents=True)
    complete_cookie.write_text(
        "SESSDATA=complete; DedeUserID=33333\n", encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(complete_root))

    assert data_paths.initialize_data_layout() == "default"
    migrated = data_root / "profiles" / "default"
    assert _marker_values(migrated / "binggo.db") == ["complete-account"]
    assert (migrated / "cookies.txt").read_bytes() == complete_cookie.read_bytes()
    assert json.loads((migrated / "profile.json").read_text(encoding="utf-8"))["mid"] == "33333"


def test_corrupt_legacy_database_never_commits_partial_profile_and_can_retry(
    data_root: Path,
) -> None:
    legacy_database = data_root / "data" / "binggo.db"
    legacy_database.parent.mkdir(parents=True)
    legacy_database.write_bytes(b"not-a-sqlite-database")

    with pytest.raises(RuntimeError, match="无法安全迁移"):
        data_paths.initialize_data_layout()

    profiles = data_root / "profiles"
    assert not (profiles / "default").exists()
    assert list(profiles.glob(".default.migrating-*")) == []
    assert not (data_root / "active_profile.json").exists()
    assert legacy_database.read_bytes() == b"not-a-sqlite-database"

    legacy_database.unlink()
    _create_marker_database(legacy_database, "repaired-legacy")
    assert data_paths.initialize_data_layout() == "default"
    assert _marker_values(profiles / "default" / "binggo.db") == [
        "repaired-legacy"
    ]


def test_any_source_cli_can_discover_legacy_checkout_root(
    data_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checkout = tmp_path / "legacy-checkout"
    fake_module = checkout / "src" / "data_paths.py"
    fake_module.parent.mkdir(parents=True)
    legacy_database = checkout / "data" / "binggo.db"
    _create_marker_database(legacy_database, "source-cli-legacy")
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    monkeypatch.setattr(data_paths, "__file__", str(fake_module))
    monkeypatch.setattr(data_paths, "_is_frozen", lambda: False)

    # 模拟任意 CLI 先向 db.engine 请求数据库路径；路径入口必须先迁移，
    # 不能让 CLI 创建一个空 default DB 后永久遮蔽旧库。
    database_path = data_paths.get_database_path()
    assert database_path == data_root / "profiles" / "default" / "binggo.db"
    assert _marker_values(database_path) == ["source-cli-legacy"]


def test_failed_legacy_copy_leaves_no_profile_and_can_retry(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    legacy_database = data_root / "data" / "binggo.db"
    legacy_cookie = data_root / "config" / "cookies.txt"
    _create_marker_database(legacy_database, "legacy-retry")
    legacy_cookie.parent.mkdir(parents=True, exist_ok=True)
    legacy_cookie.write_text("SESSDATA=retry; DedeUserID=13579\n", encoding="utf-8")
    real_copy = data_paths._copy_sqlite_database

    def fail_after_partial_copy(_source: Path, destination: Path) -> None:
        destination.write_bytes(b"partial")
        raise OSError("simulated copy failure")

    monkeypatch.setattr(data_paths, "_copy_sqlite_database", fail_after_partial_copy)
    with pytest.raises(OSError, match="simulated copy failure"):
        data_paths.initialize_data_layout()

    profiles = data_root / "profiles"
    assert not (profiles / "default").exists()
    assert list(profiles.glob(".default.migrating-*")) == []
    assert not (data_root / "active_profile.json").exists()
    assert legacy_database.is_file()
    assert legacy_cookie.is_file()

    monkeypatch.setattr(data_paths, "_copy_sqlite_database", real_copy)
    assert data_paths.initialize_data_layout() == "default"
    assert _marker_values(profiles / "default" / "binggo.db") == ["legacy-retry"]
    assert (profiles / "default" / "cookies.txt").read_bytes() == legacy_cookie.read_bytes()
