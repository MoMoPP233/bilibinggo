from __future__ import annotations

from src import data_paths
from src.secrets_inventory import (
    SECRET_FILENAMES,
    is_redact_secret_key,
    is_sanitize_secret_key,
    redact_secret_key_re,
    sanitize_secret_key_re,
    secret_filenames_csv,
    secret_file_paths,
)


def test_secret_filenames() -> None:
    assert SECRET_FILENAMES == frozenset({"cookies.txt", "llm.env"})
    assert "cookies.txt" in secret_filenames_csv()


def test_secrets_inventory_paths_follow_profile_and_shared_root(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    # 显式固化布局，避免共享会话目录里偶发的 legacy 文件把 Profile 命名
    # 从 account-1 漂移到 default（legacy 迁移分支）——测试结果与其它测试留下的
    # 任何文件无关，只取决于本测试自己创建的 pointer + profile.json。
    profiles_dir = tmp_path / "profiles" / "account-1"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "profile.json").write_text(
        '{"profile_id": "account-1", "mid": "", "nickname": ""}', encoding="utf-8"
    )
    (tmp_path / "active_profile.json").write_text(
        '{"profile_id": "account-1"}', encoding="utf-8"
    )
    (tmp_path / "shared").mkdir(exist_ok=True)
    data_paths.reset_runtime_profile_for_tests()
    try:
        paths = secret_file_paths()
        assert paths[0] == tmp_path / "profiles" / "account-1" / "cookies.txt"
        assert paths[1] == tmp_path / "shared" / "llm.env"
    finally:
        data_paths.reset_runtime_profile_for_tests()


def test_redact_vs_sanitize_matching() -> None:
    assert is_redact_secret_key("api_key")
    assert is_redact_secret_key("API-KEY")
    assert not is_redact_secret_key("my_cookie")
    assert is_sanitize_secret_key("my_cookie")
    assert redact_secret_key_re().match("cookie")
    assert sanitize_secret_key_re().search("x_token_y")
