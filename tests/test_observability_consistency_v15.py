"""Observability consistency V15：

1. account_service NAV -799 展示为“临时限频”，不冒充正式平台风控；
   -352/-509/429 展示与正式语义一致；全程 0 cooldown/pause 写入。
2. DS9 last_scan_at 纯观察字段持久化（无变化话题也落盘），重启后可读；
   不影响 checkpoint/fp/dedup/是否扫描。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from src.platform_risk import is_platform_risk_code


# ---------- account_service ----------


def _nav_payload(code: int, message: str = "", is_login: bool = False) -> dict:
    return {"code": code, "message": message, "data": {"isLogin": is_login}}


def _run_account_fetch(code: int) -> dict:
    from web import account_service as svc

    class _FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def request_json(
            self,
            url: str,
            params: dict | None = None,
            *,
            referer: str | None = None,
            retries: int = 0,
        ) -> dict:
            if "nav/stat" in url:
                return {"code": 0, "data": {}}
            return _nav_payload(code)

    with patch.object(svc, "BilibiliClient", _FakeClient), patch.object(
        svc, "has_login_cookie", return_value=True
    ):
        return svc._fetch_remote_profile()


def test_account_minus_799_shows_temporary_restriction_not_platform_risk() -> None:
    profile = _run_account_fetch(-799)
    assert "临时限频" in profile["message"]
    assert "-799" in profile["message"]
    assert "平台风控" not in profile["message"]
    assert "风控" not in profile["message"]


def test_account_formal_codes_still_shown_as_platform_risk() -> None:
    for code in (-352, -509, 429):
        profile = _run_account_fetch(code)
        text = profile["message"]
        assert ("平台风控" in text) or ("平台" in text and "限流" in text), (code, text)


def test_account_risk_path_writes_no_global_cooldown(monkeypatch) -> None:
    from web import account_service as svc

    recorded: list = []
    monkeypatch.setattr(
        "web.auto_remote_state.record_auto_remote_risk", lambda **kwargs: recorded.append(kwargs)
    )
    for code in (-352, -509, 429, -799):
        _run_account_fetch(code)
    assert recorded == []


def test_account_risk_path_writes_no_profile_pause(monkeypatch) -> None:
    from web import account_service as svc

    called = {"pause": False}
    monkeypatch.setattr(
        "src.repost_cleanup.pause_maintenance_risk",
        lambda *a, **k: called.update(pause=True),
    )
    for code in (-352, -509, 429, -799):
        _run_account_fetch(code)
    assert called["pause"] is False


@pytest.mark.parametrize(
    ("code", "expected"),
    [(-352, True), (-509, True), (429, True), (-799, False)],
)
def test_formal_helper_unchanged(code, expected) -> None:
    assert is_platform_risk_code(code) is expected


# ---------- DS9 last_scan_at ----------


@pytest.fixture
def ds9_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import src.sources.ds9_topics as ds9

    state_file = tmp_path / "ds9_topics.json"
    monkeypatch.setattr(ds9, "_state_path", lambda: state_file)
    return ds9, state_file


def _seed_state(ds9, state_file: Path, records: list[dict]) -> None:
    state = {"topics": records}
    ds9._write_state(state)


def test_ds9_commit_meta_persists_last_scan_across_reload(ds9_state) -> None:
    ds9, state_file = ds9_state
    records = [{"topic_id": "1261901", "last_scan_at": None, "fp": None}]
    _seed_state(ds9, state_file, records)

    ds9._commit_topic_meta_now([{"topic_id": "1261901", "last_scan_at": 123456}], now=123456)

    fresh = ds9._topic_records(ds9._read_state())
    assert fresh[0]["last_scan_at"] == 123456


def test_ds9_legacy_state_without_field_reads_compatibly(ds9_state) -> None:
    ds9, state_file = ds9_state
    _seed_state(ds9, state_file, [{"topic_id": "1261901", "fp": None}])

    fresh = ds9._topic_records(ds9._read_state())
    assert fresh[0]["topic_id"] == "1261901"
    assert fresh[0].get("last_scan_at") is None  # 旧 state：显示未知，下次扫描补齐


def test_ds9_meta_commit_never_touches_fp_or_incomplete(ds9_state) -> None:
    ds9, state_file = ds9_state
    records = [{"topic_id": "1261901", "fp": "fp-old", "incomplete": ["x"], "last_scan_at": None}]
    _seed_state(ds9, state_file, records)

    ds9._commit_topic_meta_now([{"topic_id": "1261901", "last_scan_at": 9}], now=9)

    fresh = ds9._topic_records(ds9._read_state())[0]
    assert fresh["fp"] == "fp-old"
    assert fresh["incomplete"] == ["x"]


def test_ds9_check_update_nochange_persists_last_scan(ds9_state, monkeypatch) -> None:
    ds9, state_file = ds9_state
    records = [
        {
            "topic_id": "1261901",
            "name": "抽奖！！！",
            "kind": "built-in",
            "enabled": True,
            "fp": "fp-same",
            "incomplete": [],
            "last_success_at": None,
            "last_scan_at": None,
            "last_found": 0,
            "last_error": "",
        }
    ]
    _seed_state(ds9, state_file, records)

    # 固定 records 视角（避免默认话题补齐影响本轮断言），落盘仍走真实 state 文件
    monkeypatch.setattr(ds9, "_topic_records", lambda state: list(records))

    client = MagicMock()
    monkeypatch.setattr(ds9, "_fetch_topic_page", lambda client, topic_id: ([], "", False))
    monkeypatch.setattr(ds9, "normalize_topic_entries", lambda entries: ([], [], []))
    monkeypatch.setattr(ds9, "fingerprint_of_topic", lambda outers: "fp-same")
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: client)

    result = ds9.check_update()
    assert result.updated is False
    # 直接读文件验证观察字段已持久化（绕过 _topic_records 的默认值补齐）
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert any(
        str(t.get("topic_id")) == "1261901" and t.get("last_scan_at") is not None
        for t in stored.get("topics") or []
    )


def test_ds9_check_update_partial_failure_still_records_last_scan(ds9_state, monkeypatch) -> None:
    ds9, state_file = ds9_state
    records = [
        {
            "topic_id": "1261901",
            "name": "抽奖！！！",
            "kind": "built-in",
            "enabled": True,
            "fp": None,
            "incomplete": [],
            "last_success_at": None,
            "last_scan_at": None,
            "last_found": 0,
            "last_error": "",
        }
    ]
    _seed_state(ds9, state_file, records)

    monkeypatch.setattr(
        ds9, "_fetch_topic_page", lambda client, topic_id: (_ for _ in ()).throw(RuntimeError("网络超时"))
    )
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: MagicMock())

    result = ds9.check_update()
    assert result.ds9_summary["partial"] is True

    fresh = ds9._topic_records(ds9._read_state())
    assert fresh[0]["last_scan_at"] is not None
    assert fresh[0]["fp"] is None  # checkpoint 未推进


def test_ds9_risk_abort_keeps_formal_semantics(ds9_state, monkeypatch) -> None:
    ds9, state_file = ds9_state
    records = [
        {
            "topic_id": "1261901",
            "name": "抽奖！！！",
            "kind": "built-in",
            "enabled": True,
            "fp": None,
            "incomplete": [],
            "last_success_at": None,
            "last_scan_at": None,
            "last_found": 0,
            "last_error": "",
        }
    ]
    _seed_state(ds9, state_file, records)

    def risk_fetch(client, topic_id):
        raise RuntimeError("API error -352: 风控校验失败")

    monkeypatch.setattr(ds9, "_fetch_topic_page", risk_fetch)
    monkeypatch.setattr(ds9, "BilibiliClient", lambda: MagicMock())

    with pytest.raises(RuntimeError, match="API error -352"):
        ds9.check_update()
    # 未推进、未伪装成功
    fresh = ds9._topic_records(ds9._read_state())
    assert fresh[0]["fp"] is None
