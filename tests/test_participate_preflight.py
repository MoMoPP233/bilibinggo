from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import participation as flow
from src.activity_store import replace_all_activities
from src.participate_preflight import ensure_activity_participatable


def test_ensure_activity_participatable_rejects_joined(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = isolated_home
    dynamic_id = "1000000000000000001"
    replace_all_activities(
        [
            {
                "dynamic_id": dynamic_id,
                "lottery_type": "互动抽奖",
                "activity_status": "未参加",
                "draw_status": "active",
                "lottery_time": 9_999_999_999,
                "conditions": {},
            }
        ]
    )

    notice = {
        "participated": True,
        "lottery_time": 9_999_999_999,
        "status": 0,
    }
    client = MagicMock()
    monkeypatch.setattr(
        "src.participate_preflight.fetch_dynamic_detail",
        lambda _client, _did: {"modules": {}},
    )
    monkeypatch.setattr("src.participate_preflight.is_upower_dynamic", lambda _item: False)
    monkeypatch.setattr(
        "src.participate_preflight.fetch_notice_for_interact",
        lambda _client, _did: (notice, 0, dynamic_id),
    )
    monkeypatch.setattr("src.participate_preflight.load_participations", lambda: {})

    with pytest.raises(RuntimeError, match="已参加"):
        ensure_activity_participatable(client, dynamic_id, lottery_type_hint="互动抽奖")


def test_unknown_time_repost_preflight_remains_participatable(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = isolated_home
    dynamic_id = "1000000000000000002"
    replace_all_activities(
        [
            {
                "dynamic_id": dynamic_id,
                "lottery_type": "转发抽奖",
                "activity_status": "未参加",
                "draw_status": "active",
                "lottery_time": None,
                "conditions": {"lottery_time_inferred": True},
            }
        ]
    )
    monkeypatch.setattr(
        "src.participate_preflight.fetch_dynamic_detail",
        lambda _client, _did: {"modules": {}},
    )
    monkeypatch.setattr("src.participate_preflight.is_upower_dynamic", lambda _item: False)
    monkeypatch.setattr("src.participate_preflight.load_participations", lambda: {})

    item = ensure_activity_participatable(
        MagicMock(), dynamic_id, lottery_type_hint="转发抽奖"
    )

    assert item["activity_status"] == "未参加"
    assert item["draw_status"] == "active"
    assert item.get("lottery_time") is None


def test_unknown_time_preflight_continues_to_mock_mutation_stage(
    isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = isolated_home
    dynamic_id = "1000000000000000003"
    replace_all_activities(
        [
            {
                "dynamic_id": dynamic_id,
                "lottery_type": "转发抽奖",
                "activity_status": "未参加",
                "draw_status": "active",
                "lottery_time": None,
                "conditions": {"lottery_time_inferred": True},
            }
        ]
    )
    monkeypatch.setattr(flow, "require_login", lambda: ("csrf", 401))
    monkeypatch.setattr(
        "src.participate_preflight.fetch_dynamic_detail",
        lambda _client, _did: {"modules": {}},
    )
    monkeypatch.setattr("src.participate_preflight.is_upower_dynamic", lambda _item: False)
    reached: list[str] = []

    def fake_mutation(_client, *, dynamic_id: str, lottery_type: str, **_kwargs):
        reached.append(dynamic_id)
        return flow.ParticipateResult(
            dynamic_id=dynamic_id,
            lottery_type=lottery_type,
            status="joined",
            message="mock mutation reached",
            action_text="",
            actions=[],
            context_snapshot={},
        )

    monkeypatch.setattr(flow, "_participate_activity_unlocked", fake_mutation)

    result = flow.participate_activity(
        MagicMock(),
        dynamic_id=dynamic_id,
        lottery_type="转发抽奖",
        preflight=True,
        persist=False,
    )

    assert reached == [dynamic_id]
    assert result.message == "mock mutation reached"
