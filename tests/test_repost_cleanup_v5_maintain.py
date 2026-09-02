from __future__ import annotations

import pytest

from src.participation_guard import (
    confirm_repost,
    mark_repost_suspected,
    mark_repost_unknown,
    record_pending,
)
from src.repost_cleanup import auto_maintain
from src.repost_cleanup import maintenance_risk_paused, recover_auto_maintenance
from src.db.engine import reset_engine_for_tests
from src.repost_history import (
    RepostImportRecord,
    claim_delete_pending,
    get_checkpoint,
    mark_delete_result,
    mark_sync_needed,
    pause_maintenance_risk,
    upsert_repost_records,
)

UID = "12345"
DYNAMIC_ID = "1000000000000000001"
REPOST_ID = "2000000000000000001"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class _TrackingClient:
    def __init__(self) -> None:
        self.closed = False

    def __enter__(self):
        assert self.closed is False
        return self

    def __exit__(self, *args):
        self.closed = True
        return None


class _NavClient(_TrackingClient):
    def __init__(self, response) -> None:
        super().__init__()
        self.response = response
        self.requests = 0

    def request_json(self, url, *, retries=3):
        assert retries == 0
        self.requests += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _login(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._require_verified_login",
        lambda client: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )


def _runtime_only(monkeypatch) -> None:
    monkeypatch.setattr(
        "src.repost_cleanup.require_login",
        lambda: ("csrf-token", int(UID)),
    )
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: "profile-1")
    monkeypatch.setattr(
        "src.repost_cleanup.db_path",
        lambda: type("_P", (), {"resolve": lambda self: "db-1"})(),
    )


def test_confirm_repost_marks_sync_needed_but_other_states_do_not(isolated_home) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    assert get_checkpoint(UID).sync_needed is True

    other = "1000000000000000002"
    record_pending(UID, other)
    mark_repost_unknown(UID, other)
    mark_repost_suspected(UID, "1000000000000000003")
    # confirmed 已标记；pending/unknown/suspected 不改变该标记
    assert get_checkpoint(UID).sync_needed is True


def test_auto_maintain_no_new_data_is_zero_remote(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    monkeypatch.setattr(
        "src.repost_cleanup.sync_repost_history",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应触发同步")),
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应触发评估")),
    )

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["synced"] is False
    assert result["evaluated_originals"] == 0
    assert "0 远程" in result["message"]


def test_auto_maintain_syncs_and_clears_sync_needed(isolated_home, monkeypatch) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100),
            RepostImportRecord(
                repost_dynamic_id="2000000000000000002",
                original_dynamic_id="1000000000000000002",
                reposted_at=101,
            ),
        ],
        seen_at=100,
    )
    monkeypatch.setattr(
        "src.repost_cleanup._assess_original",
        lambda *args, **kwargs: CandidateAssessmentSafe(),
    )
    monkeypatch.setattr(
        "src.repost_cleanup.fetch_space_feed_page",
        lambda *args, **kwargs: {"items": [], "offset": ""},
    )

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert get_checkpoint(UID).sync_needed is False
    assert result["synced"] is True


def test_auto_maintain_sync_and_assessment_use_separate_live_clients(
    isolated_home, monkeypatch,
) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    _login(monkeypatch)
    feed_item = {
        "id_str": REPOST_ID,
        "type": "DYNAMIC_TYPE_FORWARD",
        "modules": {
            "module_author": {"mid": int(UID), "name": "当前账号", "pub_ts": 100}
        },
        "orig": {
            "id_str": DYNAMIC_ID,
            "modules": {"module_author": {"mid": 54321, "name": "原作者"}},
        },
    }
    monkeypatch.setattr(
        "src.repost_cleanup.fetch_space_feed_page",
        lambda *args, **kwargs: {"items": [feed_item], "offset": ""},
    )
    clients: list[_TrackingClient] = []

    def client_factory() -> _TrackingClient:
        client = _TrackingClient()
        clients.append(client)
        return client

    def assess(client, *args, **kwargs):
        assert len(clients) == 2
        assert clients[0].closed is True
        assert client is clients[1]
        assert client.closed is False
        return CandidateAssessmentSafe()

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)

    result = auto_maintain(client_factory=client_factory)

    assert result["synced"] is True
    assert result["evaluated_originals"] == 1
    assert len(clients) == 2
    assert all(client.closed for client in clients)


def test_auto_maintain_sync_failure_keeps_sync_needed(isolated_home, monkeypatch) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    _login(monkeypatch)

    def fail_sync(*, on_progress=None, cancel_check=None, client_factory=None):
        raise RuntimeError("网络请求失败: ConnectError")

    monkeypatch.setattr("src.repost_cleanup.sync_repost_history", fail_sync)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["synced"] is False
    assert get_checkpoint(UID).sync_needed is True
    assert "保持待同步" in result["message"]


def test_auto_assessment_uses_conservative_budget(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    monkeypatch.setattr("src.repost_cleanup.AUTO_ASSESSMENT_BUDGET", 2)
    originals = [f"100000000000000000{n}" for n in range(1, 5)]
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(
                repost_dynamic_id=f"200000000000000000{n}",
                original_dynamic_id=original,
                reposted_at=100 + n,
            )
            for n, original in enumerate(originals, 1)
        ],
        seen_at=100,
    )
    calls = {"n": 0}

    def assess(*args, **kwargs):
        calls["n"] += 1
        return CandidateAssessmentSafe()

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert calls["n"] == 2
    assert result["evaluated_originals"] == 2


def test_auto_maintain_rate_limit_stops(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=100,
    )

    def assess(*args, **kwargs):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["rate_limited"] is True
    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"


def test_auto_maintain_never_deletes(isolated_home, monkeypatch) -> None:
    from src.repost_cleanup import delete_reposts

    _login(monkeypatch)
    monkeypatch.setattr(
        delete_reposts.__module__ + ".delete_reposts",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("自动维护不得调用删除")),
    )
    result = auto_maintain(client_factory=lambda: _FakeClient())
    assert "delete" not in str(result).lower() or "deleted" not in str(result).lower()
    assert result.get("requested_count") is None


def test_deleted_tombstone_not_resurrected_by_resync(isolated_home) -> None:
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=100,
    )
    claim_delete_pending(UID, REPOST_ID, requested_at=200)
    mark_delete_result(UID, REPOST_ID, status="deleted", deleted_at=201, updated_at=201)

    # 增量 feed 再次看到同一 repost：不得恢复为 active。
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=300,
    )

    from src.repost_history import get_repost

    record = get_repost(UID, REPOST_ID)
    assert record.delete_status == "deleted"
    assert record.deleted_at == 201


def test_sync_needed_is_profile_scoped(isolated_home) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    assert get_checkpoint(UID).sync_needed is True
    assert get_checkpoint("54321") is None
    assert get_checkpoint("54321") is None or get_checkpoint("54321").sync_needed is False


def test_auto_maintain_binds_runtime_profile(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [
            RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100),
            RepostImportRecord(
                repost_dynamic_id="2000000000000000002",
                original_dynamic_id="1000000000000000002",
                reposted_at=101,
            ),
        ],
        seen_at=100,
    )
    state = {"profile": "profile-1"}
    monkeypatch.setattr("src.repost_cleanup.get_runtime_profile_id", lambda: state["profile"])

    def assess(*args, **kwargs):
        state["profile"] = "profile-2"
        return CandidateAssessmentSafe()

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)
    result = auto_maintain(client_factory=lambda: _FakeClient())
    assert "Profile 已变化" in result["message"]


def test_risk_code_persists_maintenance_pause(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=100,
    )

    def assess(*args, **kwargs):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"
    checkpoint = get_checkpoint(UID)
    assert checkpoint.maintenance_risk_paused is True
    assert "风控" in (checkpoint.maintenance_risk_reason or "")


def test_http_429_persists_maintenance_pause(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(repost_dynamic_id=REPOST_ID, original_dynamic_id=DYNAMIC_ID, reposted_at=100)],
        seen_at=100,
    )

    def assess(*args, **kwargs):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr("src.repost_cleanup._assess_original", assess)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"
    assert get_checkpoint(UID).maintenance_risk_paused is True


@pytest.mark.parametrize("code", [-352, -509])
def test_initial_nav_risk_code_persists_pause(
    isolated_home, monkeypatch, code,
) -> None:
    _runtime_only(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(REPOST_ID, DYNAMIC_ID, 100)],
        seen_at=100,
    )
    client = _NavClient({"code": code, "message": "risk-control"})

    result = auto_maintain(client_factory=lambda: client)

    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"
    assert client.requests == 1
    assert client.closed is True
    checkpoint = get_checkpoint(UID)
    assert checkpoint.maintenance_risk_paused is True
    assert str(code) in (checkpoint.maintenance_risk_reason or "")


def test_initial_nav_http_429_persists_pause(isolated_home, monkeypatch) -> None:
    _runtime_only(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(REPOST_ID, DYNAMIC_ID, 100)],
        seen_at=100,
    )
    client = _NavClient(RuntimeError("HTTP 429 Too Many Requests"))

    result = auto_maintain(client_factory=lambda: client)

    assert result["rate_limited"] is True
    assert get_checkpoint(UID).maintenance_risk_paused is True
    assert client.requests == 1
    assert client.closed is True


def test_initial_nav_ordinary_business_error_does_not_pause(
    isolated_home, monkeypatch,
) -> None:
    _runtime_only(monkeypatch)
    upsert_repost_records(
        UID,
        [RepostImportRecord(REPOST_ID, DYNAMIC_ID, 100)],
        seen_at=100,
    )
    client = _NavClient({"code": -101, "message": "账号未登录"})

    with pytest.raises(RuntimeError, match=r"NAV API error -101"):
        auto_maintain(client_factory=lambda: client)

    checkpoint = get_checkpoint(UID)
    assert checkpoint is None or checkpoint.maintenance_risk_paused is False
    assert client.requests == 1
    assert client.closed is True


def test_ordinary_network_error_does_not_pause(isolated_home, monkeypatch) -> None:
    confirm_repost(UID, DYNAMIC_ID)
    _login(monkeypatch)

    def fail_sync(*, on_progress=None, cancel_check=None, client_factory=None):
        raise RuntimeError("网络请求失败: ConnectError")

    monkeypatch.setattr("src.repost_cleanup.sync_repost_history", fail_sync)

    result = auto_maintain(client_factory=lambda: _FakeClient())

    assert get_checkpoint(UID).maintenance_risk_paused is False
    assert get_checkpoint(UID).sync_needed is True
    assert "保持待同步" in result["message"]


def test_paused_auto_maintain_is_zero_remote(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    pause_maintenance_risk(UID, "风控暂停", at=100)

    def forbidden_client():
        raise AssertionError("风控暂停中不得创建客户端联网")

    result = auto_maintain(client_factory=forbidden_client)

    assert result["message"] == "自动维护因平台限制已暂停，请稍后手动恢复。"
    assert maintenance_risk_paused(UID) is True


def test_risk_pause_survives_restart(isolated_home) -> None:
    pause_maintenance_risk(UID, "风控暂停", at=100)
    reset_engine_for_tests()

    checkpoint = get_checkpoint(UID)
    assert checkpoint.maintenance_risk_paused is True
    assert checkpoint.maintenance_risk_paused_at == 100


def test_risk_pause_is_profile_scoped(isolated_home) -> None:
    pause_maintenance_risk(UID, "风控暂停", at=100)
    assert maintenance_risk_paused(UID) is True
    assert maintenance_risk_paused("54321") is False


def test_manual_recover_is_local_and_does_not_run_maintenance(isolated_home, monkeypatch) -> None:
    _login(monkeypatch)
    pause_maintenance_risk(UID, "风控暂停", at=100)

    recover_auto_maintenance(UID)

    assert get_checkpoint(UID).maintenance_risk_paused is False

    def forbidden_client():
        raise AssertionError("恢复后不立即执行维护，不应联网")

    result = auto_maintain(client_factory=forbidden_client)
    assert "0 远程" in result["message"]


class CandidateAssessmentSafe:
    level = "safe"
    reason = "ok"
    reason_code = "safe_official_notice"
    lottery_type = "互动抽奖"
    lottery_time = 1_800_000_000 - 10 * 86400
    lottery_time_reliable = True
    eligible_after = None
    classification_source = "public_classifier"
    summary = ""
    remote_checked_at = None
