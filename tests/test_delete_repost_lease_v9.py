"""delete_expired_reposts WriterLease（V9）：手动删除转发必经最外层 lease。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import data_paths
from src.profile_writer_lease import profile_writer_lease


@pytest.fixture
def proot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    base = tmp_path / "data-root"
    profiles = base / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    account = profiles / "account-1"
    account.mkdir()
    (account / "profile.json").write_text(
        json.dumps({"profile_id": "account-1", "mid": "88001", "nickname": "old"}),
        encoding="utf-8",
    )
    (base / "active_profile.json").write_text(
        json.dumps({"profile_id": "account-1"}), encoding="utf-8"
    )
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(base))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv(data_paths.LEGACY_DATA_ROOT_ENV, raising=False)
    data_paths.reset_runtime_profile_for_tests()
    assert data_paths.get_runtime_profile_id() == "account-1"
    yield base
    data_paths.reset_runtime_profile_for_tests()


def test_delete_expired_reposts_is_in_writer_lease_actions() -> None:
    import web.actions as actions

    assert "delete_expired_reposts" in actions.WRITER_LEASE_ACTIONS
    # 与其它写型 action 一样经 run_action 外层统一获取（仅一次，无嵌套）
    for writer in ("participate", "refresh_all", "cleanup_maintenance_cycle"):
        assert writer in actions.WRITER_LEASE_ACTIONS


def test_run_action_delete_blocks_when_profile_busy(proot: Path) -> None:
    import web.actions as actions

    with profile_writer_lease(owner="test:busy-writer"):
        with pytest.raises(RuntimeError, match="profile_writer_busy"):
            actions.run_action(
                "delete_expired_reposts",
                {"repost_dynamic_ids": ["100000000000000001"]},
            )
