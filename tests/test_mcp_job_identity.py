"""MCP：等待精确 job_id，同 action 的先后 Job 不串结果（全 fake，无网络）。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MCP_ROOT = ROOT / "mcp"
if str(MCP_ROOT) not in sys.path:
    sys.path.insert(0, str(MCP_ROOT))

from binggo_mcp import jobs as mcp_jobs  # noqa: E402


class _Job:
    def __init__(self, job_id: int, action: str, state: str, message: str = ""):
        self.data = {
            "id": job_id,
            "action": action,
            "state": state,
            "message": message,
            "source": "ui",
            "progress_step": 0,
            "progress_total": 0,
        }

    def as_terminal(self):
        return _Job(self.data["id"], self.data["action"], "success", "done")


class _FakeClient:
    """按 job_id 提供状态的 fake：/api/jobs/current 显示别的任务也不影响按 id 等待。"""

    def __init__(self, by_id: dict[int, list], current: list, started: _Job | None = None):
        self.by_id = {job_id: list(seq) for job_id, seq in by_id.items()}
        self.current = list(current)
        self.started = started
        self.calls: list[str] = []

    async def get_json(self, path: str):
        self.calls.append(path)
        if path == "/api/jobs/current":
            if not self.current:
                return {"id": None, "state": "idle", "action": "", "message": ""}
            return self.current.pop(0).data
        if path.startswith("/api/jobs/"):
            job_id = int(path.rsplit("/", 1)[1])
            seq = self.by_id[job_id]
            return seq[0].data if len(seq) == 1 else seq.pop(0).data
        raise AssertionError(f"unexpected path {path}")

    async def post_json(self, path: str, *, json_body: dict | None = None):
        if path == "/api/jobs":
            if self.started is not None:
                return {"ok": True, "job": self.started.data}
            return {"ok": True, "job": self.current[0].data}
        raise AssertionError(f"unexpected post {path}")


def test_mcp_wait_waits_exact_job_id_not_other_job(monkeypatch) -> None:
    """Job A(id=10, refresh) 与 Job B(id=11, refresh) 同 action：
    等待 A 时 B 先结束，必须仍等到 A 自己的终态。"""
    client = _FakeClient(
        by_id={
            10: [
                _Job(10, "refresh_status", "running", "A working"),
                _Job(10, "refresh_status", "success", "A done"),
            ],
            # B 也在跑；如果按 action/当前槽位等待会被它骗到
            11: [_Job(11, "refresh_status", "success", "B done")],
        },
        current=[
            _Job(11, "refresh_status", "running", "B running"),
            _Job(11, "refresh_status", "success", "B done"),
        ],
    )

    async def run() -> dict:
        result = await mcp_jobs.wait_job_terminal(
            client, expect_job_id=10, expect_action="refresh_status", timeout_sec=5
        )
        return result

    final = asyncio.run(run())
    assert final["id"] == 10
    assert final["message"] == "A done"
    assert final["state"] == "success"


def test_mcp_same_action_two_jobs_do_not_cross_results(monkeypatch) -> None:
    """两个相同 action 的 Job 先后结束：等待各自的 id 都拿到各自结果。"""
    client = _FakeClient(
        by_id={
            10: [
                _Job(10, "refresh_source", "running", "A source 1"),
                _Job(10, "refresh_source", "success", "A source done"),
            ],
            11: [
                _Job(11, "refresh_source", "running", "B source 2"),
                _Job(11, "refresh_source", "success", "B source done"),
            ],
        },
        current=[
            _Job(10, "refresh_source", "running", "A"),
            _Job(10, "refresh_source", "success", "A done"),
            _Job(11, "refresh_source", "running", "B"),
            _Job(11, "refresh_source", "success", "B done"),
        ],
    )

    async def run() -> tuple[dict, dict]:
        first = await mcp_jobs.wait_job_terminal(client, expect_job_id=10, timeout_sec=5)
        second = await mcp_jobs.wait_job_terminal(client, expect_job_id=11, timeout_sec=5)
        return first, second

    first, second = asyncio.run(run())
    assert first["id"] == 10 and first["message"] == "A source done"
    assert second["id"] == 11 and second["message"] == "B source done"


def test_mcp_run_job_to_terminal_binds_started_job_id(monkeypatch) -> None:
    client = _FakeClient(
        by_id={
            20: [
                _Job(20, "refresh_watch", "running", "watch working"),
                _Job(20, "refresh_watch", "success", "watch done"),
            ],
        },
        current=[
            _Job(20, "refresh_watch", "running", "watch started"),
            _Job(20, "refresh_watch", "success", "watch done"),
        ],
        started=_Job(20, "refresh_watch", "running", "watch started"),
    )

    async def run() -> dict:
        return await mcp_jobs.run_job_to_terminal(client, "refresh_watch", timeout_sec=5)

    result = asyncio.run(run())
    assert result["job"]["id"] == 20
    assert result["job"]["message"] == "watch done"
