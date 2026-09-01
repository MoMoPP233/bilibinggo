from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src import data_paths, profile_manager
from src.db import schema
from src.db.engine import db_path, reset_engine_for_tests
from src.repost_history import (
    RepostImportRecord,
    claim_delete_pending,
    get_checkpoint,
    get_repost,
    list_active_reposts,
    list_repost_history,
    mark_delete_result,
    reconcile_last_seen,
    save_checkpoint,
    upsert_repost_records,
)

UID = "12345"
REPOST_ID = "2000000000000000001"
ORIGINAL_ID = "1000000000000000001"


def _import_record(
    repost_id: str = REPOST_ID,
    original_id: str = ORIGINAL_ID,
    reposted_at: int = 100,
    *,
    source: str = "history_import",
) -> RepostImportRecord:
    return RepostImportRecord(
        repost_dynamic_id=repost_id,
        original_dynamic_id=original_id,
        reposted_at=reposted_at,
        original_author_uid="98765",
        original_author_name="原作者",
        source=source,
    )


def test_upsert_roundtrip_is_uid_scoped_and_idempotent(isolated_home) -> None:
    assert upsert_repost_records(UID, [_import_record()], seen_at=200) == 1
    assert upsert_repost_records(UID, [_import_record()], seen_at=300) == 0
    assert upsert_repost_records("54321", [_import_record()], seen_at=250) == 1

    record = get_repost(UID, REPOST_ID)
    assert record is not None
    assert record.original_dynamic_id == ORIGINAL_ID
    assert record.reposted_at == 100
    assert record.original_author_uid == "98765"
    assert record.original_author_name == "原作者"
    assert record.source == "history_import"
    assert record.delete_status == "active"
    assert record.last_seen_at == record.updated_at == 300
    assert get_repost("54321", REPOST_ID).last_seen_at == 250


def test_upsert_preserves_binggo_source_and_delete_state(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record(source="binggo")], seen_at=100)
    assert claim_delete_pending(UID, REPOST_ID, requested_at=110)
    assert mark_delete_result(
        UID,
        REPOST_ID,
        status="unknown",
        error="response lost",
        updated_at=120,
    )

    upsert_repost_records(UID, [_import_record()], seen_at=130)
    record = get_repost(UID, REPOST_ID)
    assert record.source == "binggo"
    assert record.delete_status == "unknown"
    assert record.last_error == "response lost"
    assert record.last_seen_at == record.updated_at == 130


@pytest.mark.parametrize(
    "changed",
    [
        _import_record(original_id="1000000000000000002"),
        _import_record(reposted_at=101),
    ],
)
def test_upsert_rejects_changed_repost_identity(isolated_home, changed) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=200)
    with pytest.raises(RuntimeError, match="发生冲突"):
        upsert_repost_records(UID, [changed], seen_at=300)
    assert get_repost(UID, REPOST_ID).original_dynamic_id == ORIGINAL_ID


def test_batch_duplicate_and_cross_uid_records_are_rejected(isolated_home) -> None:
    with pytest.raises(ValueError, match="重复"):
        upsert_repost_records(UID, [_import_record(), _import_record()])
    with pytest.raises(ValueError, match="当前 UID"):
        upsert_repost_records(
            UID,
            [
                {
                    "uid": "54321",
                    "repost_dynamic_id": REPOST_ID,
                    "original_dynamic_id": ORIGINAL_ID,
                    "reposted_at": 100,
                }
            ],
        )
    assert list_repost_history(UID)[1] == 0


def test_listing_pagination_status_and_active_filter(isolated_home) -> None:
    records = [
        _import_record(str(2000 + number), str(1000 + number), 100 + number)
        for number in range(4)
    ]
    upsert_repost_records(UID, records, seen_at=500)
    assert claim_delete_pending(UID, "2000", requested_at=600)
    assert mark_delete_result(UID, "2000", status="deleted", updated_at=601)

    page, total = list_repost_history(UID, page=1, page_size=2)
    assert total == 4
    assert [record.repost_dynamic_id for record in page] == ["2003", "2002"]
    deleted, deleted_total = list_repost_history(UID, status="deleted")
    assert deleted_total == 1 and deleted[0].repost_dynamic_id == "2000"
    assert {record.repost_dynamic_id for record in list_active_reposts(UID)} == {
        "2001",
        "2002",
        "2003",
    }


def test_checkpoint_roundtrip_and_restart_persistence(isolated_home) -> None:
    saved = save_checkpoint(
        UID,
        head_dynamic_id="3000000000000000001",
        head_published_at=1000,
        full_scan_completed=True,
        last_synced_at=1100,
    )
    assert saved == get_checkpoint(UID)
    reset_engine_for_tests()
    assert get_checkpoint(UID) == saved

    empty = save_checkpoint(
        "54321",
        head_dynamic_id=None,
        head_published_at=None,
        full_scan_completed=True,
        last_synced_at=1200,
    )
    assert empty.head_dynamic_id is None and empty.full_scan_completed is True
    with pytest.raises(ValueError, match="同时"):
        save_checkpoint(
            UID,
            head_dynamic_id="1",
            head_published_at=None,
            full_scan_completed=False,
        )


def test_delete_pending_claim_is_atomic_and_fail_closed(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=100)

    def claim(_):
        return claim_delete_pending(UID, REPOST_ID, requested_at=200)

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(claim, range(8)))
    assert results.count(True) == 1
    assert get_repost(UID, REPOST_ID).delete_status == "delete_pending"
    assert claim_delete_pending(UID, REPOST_ID, requested_at=201) is False

    assert mark_delete_result(
        UID,
        REPOST_ID,
        status="unknown",
        error="connection interrupted",
        updated_at=202,
    )
    assert claim_delete_pending(UID, REPOST_ID, requested_at=203) is False
    record = get_repost(UID, REPOST_ID)
    assert record.delete_status == "unknown"
    assert record.last_error == "connection interrupted"


def test_explicit_failure_can_be_manually_reclaimed_but_deleted_cannot(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=100)
    assert claim_delete_pending(UID, REPOST_ID, requested_at=200)
    assert mark_delete_result(
        UID,
        REPOST_ID,
        status="delete_failed",
        error="code=-404",
        updated_at=201,
    )
    assert claim_delete_pending(UID, REPOST_ID, requested_at=300)
    assert mark_delete_result(
        UID,
        REPOST_ID,
        status="deleted",
        deleted_at=301,
        updated_at=301,
    )
    record = get_repost(UID, REPOST_ID)
    assert record.delete_status == "deleted"
    assert record.deleted_at == 301
    assert record.last_error is None
    assert claim_delete_pending(UID, REPOST_ID, requested_at=400) is False


def test_delete_result_requires_persisted_pending(isolated_home) -> None:
    upsert_repost_records(UID, [_import_record()], seen_at=100)
    assert mark_delete_result(UID, REPOST_ID, status="deleted", updated_at=200) is False
    assert get_repost(UID, REPOST_ID).delete_status == "active"
    with pytest.raises(ValueError, match="不能记录"):
        mark_delete_result(
            UID,
            REPOST_ID,
            status="unknown",
            deleted_at=200,
            updated_at=200,
        )


def test_reconcile_last_seen_never_infers_deletion_or_clears_unknown(isolated_home) -> None:
    upsert_repost_records(
        UID,
        [_import_record(), _import_record("2000000000000000002", "1000000000000000002")],
        seen_at=100,
    )
    assert claim_delete_pending(UID, REPOST_ID, requested_at=200)
    assert mark_delete_result(UID, REPOST_ID, status="unknown", updated_at=201)
    assert reconcile_last_seen(UID, [REPOST_ID], seen_at=300) == 1
    assert get_repost(UID, REPOST_ID).delete_status == "unknown"
    assert get_repost(UID, REPOST_ID).last_seen_at == 300
    other = get_repost(UID, "2000000000000000002")
    assert other.delete_status == "active" and other.last_seen_at == 100


@pytest.fixture
def isolated_profile_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "profile-data"
    monkeypatch.setenv(data_paths.DATA_ROOT_ENV, str(root))
    monkeypatch.setenv(data_paths.LEGACY_DATA_ROOT_ENV, str(root))
    monkeypatch.delenv(data_paths.LEGACY_HOME_ENV, raising=False)
    monkeypatch.delenv("BINGGO_PORTABLE", raising=False)
    monkeypatch.setenv("BINGGO_DATA_ROOT_LOCATOR", str(tmp_path / "locator.json"))
    reset_engine_for_tests()
    data_paths.reset_runtime_profile_for_tests()
    try:
        yield root
    finally:
        reset_engine_for_tests()
        data_paths.reset_runtime_profile_for_tests()


def test_repost_history_uses_only_runtime_profile_database(isolated_profile_root) -> None:
    assert data_paths.initialize_data_layout() == "account-1"
    assert profile_manager.create_profile()["profile_id"] == "account-2"

    def restart_into(profile_id: str) -> None:
        profile_manager.set_active_profile(profile_id)
        reset_engine_for_tests()
        data_paths.reset_runtime_profile_for_tests()
        assert data_paths.get_runtime_profile_id() == profile_id
        assert db_path().resolve().is_relative_to(isolated_profile_root.resolve())
        schema.init_db()

    restart_into("account-1")
    upsert_repost_records(UID, [_import_record()], seen_at=100)
    account_1_db = db_path()

    restart_into("account-2")
    account_2_db = db_path()
    assert account_1_db != account_2_db
    assert list_repost_history(UID)[1] == 0
    upsert_repost_records(
        UID,
        [_import_record("2000000000000000002", "1000000000000000002")],
        seen_at=200,
    )

    restart_into("account-1")
    assert get_repost(UID, REPOST_ID) is not None
    assert get_repost(UID, "2000000000000000002") is None
