from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine

from scripts import participate as cli
from src.db.models import ActivityRow, ParticipationRow, SchemaMeta, SourceCheckpointRow
from web import actions, activity_service


FIRST_ID = "1220000000000000001"
SECOND_ID = "1220000000000000002"


def _skip(dynamic_id=FIRST_ID, *, reason="repost_unknown") -> dict:
    return {
        "dynamic_id": dynamic_id,
        "lottery_type": "转发抽奖",
        "status": "skipped",
        "actions": [],
        "message": "转发状态待确认，已跳过以避免重复操作",
        "skipped": True,
        "skip_reason": reason,
        "context_snapshot": {"dedup_reason": reason},
    }


def _joined(dynamic_id=SECOND_ID) -> dict:
    return {
        "dynamic_id": dynamic_id,
        "lottery_type": "转发抽奖",
        "status": "joined",
        "message": "参与成功",
        "actions": [{"action": name, "ok": True} for name in ("like", "follow", "favorite", "repost", "comment")],
    }


@pytest.mark.parametrize("reason", sorted(actions.PARTICIPATION_DEDUP_SKIP_REASONS))
def test_single_dedup_skip_has_no_early_client_or_joined_write(monkeypatch, reason) -> None:
    def fail_client(*args, **kwargs):
        raise AssertionError("Web 不应在本地 guard 前创建客户端")

    calls = []

    def participate(client=None, **kwargs):
        assert client is None
        assert kwargs["preflight"] is True
        calls.append(kwargs["dynamic_id"])
        payload = _skip(reason=reason)
        return SimpleNamespace(to_dict=lambda: payload)

    monkeypatch.setattr(actions, "BilibiliClient", fail_client)
    monkeypatch.setattr(actions, "participate_activity", participate)
    monkeypatch.setattr(actions, "lookup_lottery_type", lambda dynamic_id: "转发抽奖")
    monkeypatch.setattr(actions, "resolve_participate_lottery_type", lambda *args, **kwargs: "转发抽奖")
    marked = []
    monkeypatch.setattr("src.fetch_activity_info.mark_enriched_joined", marked.append)
    monkeypatch.setattr(actions, "refresh_local_activity_statuses", lambda: pytest.fail("skip不应刷新参与状态"))

    payload = actions.run_action("participate", {"dynamic_id": FIRST_ID})

    assert payload["ok"] is True
    assert payload["result"]["skipped"] is True
    assert payload["result"]["skip_reason"] == reason
    assert calls == [FIRST_ID]
    assert marked == []


@pytest.mark.parametrize("reason", [None, "activity_ended", "unrecognized_reason"])
def test_ordinary_business_skip_is_still_an_error(reason) -> None:
    payload = _skip(reason=reason)
    with pytest.raises(RuntimeError):
        actions._normalize_participate_payload(payload)


@pytest.mark.parametrize("all_skipped", [False, True])
def test_triple_skip_does_not_cancel_other_target_or_count_as_joined(monkeypatch, all_skipped) -> None:
    targets = [{"dynamic_id": dynamic_id, "lottery_type": "转发抽奖"} for dynamic_id in (FIRST_ID, SECOND_ID)]
    barrier = threading.Barrier(2)
    called = []

    def execute(dynamic_id, on_step, **kwargs):
        assert kwargs.get("client") is None
        called.append(dynamic_id)
        barrier.wait(timeout=5)
        return _skip(dynamic_id) if all_skipped or dynamic_id == FIRST_ID else _joined(dynamic_id)

    monkeypatch.setattr(actions, "pick_triple_participate_targets", lambda **kwargs: targets)
    monkeypatch.setattr(actions, "resolve_participate_lottery_type", lambda *args, **kwargs: "转发抽奖")
    monkeypatch.setattr(actions, "_execute_participate", execute)
    monkeypatch.setattr(actions, "BilibiliClient", lambda *args, **kwargs: pytest.fail("不应提前创建客户端"))
    monkeypatch.setattr(actions, "refresh_local_activity_statuses", lambda: None)
    marked = []
    monkeypatch.setattr("src.fetch_activity_info.mark_enriched_joined", marked.append)
    cancel = threading.Event()

    payload = actions.run_action("participate_triple", {"from_auto": True}, cancel_event=cancel)

    assert set(called) == {FIRST_ID, SECOND_ID}
    assert cancel.is_set() is False
    assert payload["ok"] is True
    assert payload["result"]["joined"] == (0 if all_skipped else 1)
    assert payload["result"]["skipped_count"] == (2 if all_skipped else 1)
    assert payload["result"]["failed"] == 0
    assert payload["result"]["skipped"] is all_skipped
    assert payload["result"]["from_auto"] is True
    # The stubbed guarded service owns all real participation writes; Web must
    # not write joined state again after that service releases its gate.
    assert marked == []


def test_blocked_targets_remain_visible_with_history_but_are_not_selected(monkeypatch) -> None:
    items = [
        {"dynamic_id": dynamic_id, "lottery_type": "转发抽奖", "activity_status": "未参加", "status_classified": True, "draw_status": "active"}
        for dynamic_id in (FIRST_ID, SECOND_ID)
    ]
    history = {"status": "failed", "message": "原始历史记录", "recorded_at": 10, "actions": []}
    monkeypatch.setattr(activity_service, "_load_activities_payload", lambda: {"activities": items})
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {FIRST_ID: history})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    monkeypatch.setattr(activity_service, "participation_uid", lambda: "123")
    seen_uids = []

    def blocked(uid):
        seen_uids.append(uid)
        return {FIRST_ID}

    monkeypatch.setattr(activity_service, "load_blocked_guard_ids", blocked)
    rows = activity_service._filtered_activity_rows(status="未参加")
    blocked_row = next(row for row in rows if row["dynamic_id"] == FIRST_ID)

    assert seen_uids == ["123"]
    assert blocked_row["participation_blocked"] is True
    assert blocked_row["can_participate"] is False
    assert "待确认" in blocked_row["skip_reason"]
    assert blocked_row["activity_status"] == "未参加"
    assert blocked_row["last_participation"] == history
    assert [row["dynamic_id"] for row in activity_service._pick_triple_from_rows(rows)] == [SECOND_ID]


def test_completed_dedup_skip_reason_survives_another_target_failure(monkeypatch) -> None:
    skip_message = "上次转发结果未知，禁止自动重发，需人工确认"
    skip_reported = threading.Event()
    events = []
    targets = [
        {"dynamic_id": FIRST_ID, "lottery_type": "转发抽奖", "activity_title": "需确认目标"},
        {"dynamic_id": SECOND_ID, "lottery_type": "转发抽奖", "activity_title": "失败目标"},
    ]

    def execute(dynamic_id, on_step, **kwargs):
        if dynamic_id == FIRST_ID:
            return {**_skip(dynamic_id), "message": skip_message}
        assert skip_reported.wait(timeout=5), "等待第一个目标完成幂等跳过"
        raise RuntimeError("真正的参与失败")

    def on_progress(**event):
        events.append(event)
        if skip_message in str(event.get("log_append") or ""):
            skip_reported.set()

    monkeypatch.setattr(actions, "pick_triple_participate_targets", lambda **kwargs: targets)
    monkeypatch.setattr(actions, "resolve_participate_lottery_type", lambda *args, **kwargs: "转发抽奖")
    monkeypatch.setattr(actions, "_execute_participate", execute)
    cancel = threading.Event()

    with pytest.raises(RuntimeError, match="真正的参与失败"):
        actions.run_action("participate_triple", {}, on_progress=on_progress, cancel_event=cancel)

    assert cancel.is_set()
    assert skip_reported.is_set()
    assert f"需确认目标: {skip_message}" in events[-1]["message"]
    assert "需确认目标: 已取消" not in events[-1]["message"]
    assert "需确认目标: 已停止" not in events[-1]["message"]


@pytest.mark.parametrize("reason, exit_code", [("already_joined", 0), ("repost_unknown", 0), ("activity_ended", 1)])
def test_cli_uses_guarded_service_before_client_creation(monkeypatch, capsys, reason, exit_code) -> None:
    monkeypatch.setattr(cli.sys, "argv", ["participate.py", FIRST_ID])
    monkeypatch.setattr(cli, "ensure_user_dirs", lambda: None)
    monkeypatch.setattr(cli, "_lookup_lottery_type", lambda dynamic_id: "转发抽奖")
    calls = []

    def participate(*args, **kwargs):
        assert args == ()
        assert kwargs["preflight"] is True
        calls.append(kwargs["dynamic_id"])
        return SimpleNamespace(status="skipped", to_dict=lambda: _skip(reason=reason))

    monkeypatch.setattr(cli, "participate_activity", participate)
    assert cli.main() == exit_code
    assert calls == [FIRST_ID]
    assert reason in capsys.readouterr().out


@pytest.mark.parametrize("version, exit_code", [(2, 0), (7, 1)])
def test_standalone_cli_checks_runtime_profile_schema_before_client(tmp_path, version, exit_code) -> None:
    # A fresh interpreter must bootstrap without the Dashboard, using the real
    # runtime Profile path rather than isolated_home's compatibility DB shim.
    data_root = tmp_path / "standalone-cli"
    profile_dir = data_root / "profiles" / "account-2"
    profile_dir.mkdir(parents=True)
    (data_root / "active_profile.json").write_text('{"profile_id":"account-2"}', encoding="utf-8")
    cookie = profile_dir / "cookies.txt"
    cookie.write_text("DedeUserID=123; bili_jct=offline-test-only;", encoding="utf-8")
    cookie_before = cookie.read_bytes()
    database = profile_dir / "binggo.db"
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        SQLModel.metadata.create_all(engine)
        with Session(engine) as session:
            session.add(SchemaMeta(id=1, version=version))
            session.add(ActivityRow(dynamic_id=FIRST_ID, lottery_type="转发抽奖", updated_at=123))
            session.add(ParticipationRow(
                uid="123", dynamic_id=FIRST_ID, user_status="已参加", updated_at=123, source="participate",
            ))
            session.add(SourceCheckpointRow(source_id="DS-test", container_url="unchanged"))
            session.commit()
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE participation_guard")
    finally:
        engine.dispose()

    harness = """
import json
import runpy
import sys
from src.bilibili_client import BilibiliClient

clients = []
def forbidden_client(*args, **kwargs):
    clients.append(True)
    raise AssertionError("standalone CLI must not create a client in this test")

BilibiliClient.__init__ = forbidden_client
script = sys.argv.pop(1)
sys.argv[0] = script
try:
    runpy.run_path(script, run_name="__main__")
finally:
    print(json.dumps({"client_count": len(clients)}), file=sys.stderr)
"""
    env = os.environ.copy()
    env.update({
        "BINGGO_DATA_ROOT": str(data_root),
        "BINGGO_LEGACY_DATA_ROOT": str(data_root),
        "BINGGO_DATA_ROOT_LOCATOR": str(tmp_path / "locator.json"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for name in ("BINGGO_HOME", "BINGGO_PORTABLE", "BILI_COOKIE"):
        env.pop(name, None)
    repo_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "-c", harness, str(repo_root / "scripts" / "participate.py"), FIRST_ID],
        cwd=repo_root, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
    )

    assert completed.returncode == exit_code, completed.stdout + completed.stderr
    stderr_lines = completed.stderr.strip().splitlines()
    assert json.loads(stderr_lines[-1]) == {"client_count": 0}
    if version == 2:
        assert json.loads(completed.stdout)["skip_reason"] == "already_joined"
    else:
        assert "schema_version=7" in json.loads(stderr_lines[0])["error"]
        assert completed.stdout == ""
    assert cookie.read_bytes() == cookie_before
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT version FROM schema_meta WHERE id=1").fetchone() == (6 if version == 2 else 7,)
        assert conn.execute("SELECT uid,dynamic_id,user_status,updated_at,source FROM participations").fetchone() == (
            "123", FIRST_ID, "已参加", 123, "participate",
        )
        assert conn.execute("SELECT container_url FROM source_checkpoints").fetchone() == ("unchanged",)
        if version == 2:
            assert conn.execute("SELECT COUNT(*) FROM participation_guard").fetchone() == (0,)
        else:
            assert conn.execute("SELECT name FROM sqlite_master WHERE name='participation_guard'").fetchone() is None
