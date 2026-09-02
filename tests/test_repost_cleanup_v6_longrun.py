"""V6 长期运行模拟：连续维护周期在无新数据 / 一次 dirty / 风控暂停 / 恢复 / 多账号 下的远程请求行为。

用 fake 客户端与可控 feed 模拟维护周期，绝不 sleep；任何真实网络都会让测试失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from web.auto_config import set_cleanup_maintain_enabled
from src.participation_guard import confirm_repost
from src.db.engine import reset_engine_for_tests
from src.repost_cleanup import auto_maintain, recover_auto_maintenance
from src.repost_history import (
    get_checkpoint,
    get_repost,
    pause_maintenance_risk,
    upsert_repost_assessment,
)

UID_A = "11111"
UID_B = "22222"
DYNAMIC_A = "1000000000000000001"
REPOST_A = "2000000000000000001"


@pytest.fixture(autouse=True)
def _restore_maintain_default() -> None:
    set_cleanup_maintain_enabled(True)
    yield
    set_cleanup_maintain_enabled(False)


class _FakeClient:
    """被 patch 掉登录校验后只承担生命周期；真联网会失败。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def _patch_runtime(monkeypatch, uid: str) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(uid)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(uid)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )


def _tracking_clients():
    state = {"created": 0, "remotes": 0, "feed": 0}

    def client_factory() -> _FakeClient:
        state["created"] += 1
        return _FakeClient()

    def feed(*_args, **_kwargs):
        state["feed"] += 1
        state["remotes"] += 1
        return {"items": [], "offset": ""}

    return state, client_factory, feed


def _run_cycles(client_factory, n: int) -> list[str]:
    messages = []
    for _ in range(n):
        result = auto_maintain(client_factory=client_factory)
        messages.append(str(result.get("message") or ""))
    return messages


def test_scenario_a_all_idle_cycles_are_zero_remote(isolated_home: Path, monkeypatch) -> None:
    set_cleanup_maintain_enabled(True)
    _patch_runtime(monkeypatch, UID_A)
    state, client_factory, feed = _tracking_clients()
    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed)
    monkeypatch.setattr(
        "src.repost_cleanup.sync_repost_history",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("空闲不应触发同步")),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("空闲不应触发评估")),
    )

    messages = _run_cycles(client_factory, 12)

    assert state["created"] == 0
    assert state["remotes"] == 0
    assert all("0 远程" in message for message in messages)


def test_scenario_b_one_confirmed_then_closed(isolated_home: Path, monkeypatch) -> None:
    set_cleanup_maintain_enabled(True)
    _patch_runtime(monkeypatch, UID_A)
    state, client_factory, feed = _tracking_clients()
    upsert_repost_assessment(
        UID_A, DYNAMIC_A, assessment_level="safe", classification_source="public_classifier"
    )
    feed_item = {
        "id_str": REPOST_A,
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {"module_author": {"mid": int(UID_A), "name": "当前账号", "pub_ts": 100}},
        "orig": {
            "id_str": DYNAMIC_A,
            "modules": {"module_author": {"mid": 54321, "name": "原作者"}},
        },
    }

    def feed_with_item(*_args, **_kwargs):
        state["feed"] += 1
        state["remotes"] += 1
        return {"items": [feed_item], "offset": ""}

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed_with_item)

    messages = _run_cycles(client_factory, 5)
    assert state["created"] == 0 and state["remotes"] == 0

    confirm_repost(UID_A, DYNAMIC_A)
    assert get_checkpoint(UID_A).sync_needed is True

    messages = _run_cycles(client_factory, 1)
    assert state["remotes"] == 1  # 只触发这一次必要增量同步
    assert get_checkpoint(UID_A).sync_needed is False

    # 闭包后继续跑多个周期：不再重复扫描个人空间。
    _run_cycles(client_factory, 8)
    assert state["remotes"] == 1
    assert state["created"] == 1
    record = get_repost(UID_A, REPOST_A)
    assert record is not None
    assert record.delete_status == "active"


def test_scenario_c_risk_pause_stays_zero_remote_until_manual_recover(
    isolated_home: Path, monkeypatch
) -> None:
    set_cleanup_maintain_enabled(True)
    _patch_runtime(monkeypatch, UID_A)
    state, client_factory, feed = _tracking_clients()
    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed)
    pause_maintenance_risk(UID_A, "-352 风控暂停", at=1_700_000_000)

    for _ in range(10):
        result = auto_maintain(client_factory=client_factory)
        assert "已暂停" in str(result.get("message") or "")
    assert state["created"] == 0
    assert state["remotes"] == 0
    assert get_checkpoint(UID_A).maintenance_risk_paused is True

    recover_auto_maintenance(UID_A)
    assert get_checkpoint(UID_A).maintenance_risk_paused is False


def test_scenario_d_recover_without_dirty_does_not_go_remote(
    isolated_home: Path, monkeypatch
) -> None:
    set_cleanup_maintain_enabled(True)
    _patch_runtime(monkeypatch, UID_A)
    state, client_factory, feed = _tracking_clients()
    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed)
    pause_maintenance_risk(UID_A, "风控暂停", at=1_700_000_000)
    recover_auto_maintenance(UID_A)

    messages = _run_cycles(client_factory, 4)

    assert state["created"] == 0
    assert state["remotes"] == 0
    assert all("0 远程" in message for message in messages)


def test_scenario_e_accounts_do_not_cross_state(isolated_home: Path, monkeypatch) -> None:
    set_cleanup_maintain_enabled(True)
    confirm_repost(UID_A, DYNAMIC_A)  # account-1 dirty
    assert get_checkpoint(UID_A).sync_needed is True

    # 以 account-2 运行 10 个周期：干净账号绝不能联网，也不能消费 account-1 的 dirty。
    _patch_runtime(monkeypatch, UID_B)
    state, client_factory, feed = _tracking_clients()
    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed)
    for _ in range(10):
        result = auto_maintain(client_factory=client_factory)
        assert "0 远程" in str(result.get("message") or "")

    assert state["created"] == 0
    assert state["remotes"] == 0
    assert get_checkpoint(UID_A).sync_needed is True
    assert get_checkpoint(UID_B) is None or get_checkpoint(UID_B).sync_needed is False

    # 切回 account-1 后，只对 account-1 做一次必要同步。
    _patch_runtime(monkeypatch, UID_A)
    state2, client_factory2, feed2 = _tracking_clients()

    def feed_a(*_args, **_kwargs):
        state2["feed"] += 1
        state2["remotes"] += 1
        return {"items": [], "offset": ""}

    monkeypatch.setattr("src.repost_cleanup.fetch_space_feed_page", feed_a)
    auto_maintain(client_factory=client_factory2)
    assert state2["remotes"] >= 1
    assert get_checkpoint(UID_A).sync_needed is False
    assert get_checkpoint(UID_B) is None or get_checkpoint(UID_B).sync_needed is False


def test_dirty_mark_survives_restart(isolated_home: Path) -> None:
    confirm_repost(UID_A, DYNAMIC_A)
    reset_engine_for_tests()
    checkpoint = get_checkpoint(UID_A)
    assert checkpoint is not None
    assert checkpoint.sync_needed is True

