from __future__ import annotations

import time

from web import activity_service


def _activity(
    dynamic_id: str,
    lottery_time: int | None,
    *,
    status: str = "未参加",
    draw_status: str = "active",
    inferred: bool = False,
) -> dict:
    conditions = {"lottery_time_inferred": True} if inferred else {}
    return {
        "dynamic_id": dynamic_id,
        "lottery_type": "互动抽奖",
        "draw_status": draw_status,
        "activity_status": status,
        "can_participate": status == "未参加" and draw_status == "active",
        "status_classified": True,
        "lottery_time": lottery_time,
        "conditions": conditions,
        "prizes": [{"description": dynamic_id, "winner_count": 1}],
    }


def _patch_activity_sources(monkeypatch, activities: list[dict]) -> None:
    monkeypatch.setattr(
        activity_service,
        "_load_activities_payload",
        lambda: {"activities": activities},
    )
    monkeypatch.setattr(activity_service, "_load_participation_actions", lambda: {})
    monkeypatch.setattr(activity_service, "load_participations", lambda: {})
    monkeypatch.setattr(activity_service, "_load_blocked_activity_ids", lambda: set())
    monkeypatch.setattr(activity_service, "invalid_activity_ids", lambda: set())


def test_default_sort_prioritizes_reliable_draw_time_and_unknown_last(monkeypatch) -> None:
    now = int(time.time())
    activities = [
        _activity("2000000000000000001", now + 7 * 86400),  # next week
        _activity("2000000000000000002", now + 86400),  # tomorrow
        _activity("2000000000000000003", now + 3600),  # tonight
        _activity("2000000000000000004", None, inferred=True),
        _activity("2000000000000000005", now - 3600, status="未参加"),  # ended by time
        _activity("2000000000000000006", now + 1800, status="已参加"),
    ]
    _patch_activity_sources(monkeypatch, activities)

    rows = activity_service._filtered_activity_rows()

    assert [row["dynamic_id"] for row in rows] == [
        "2000000000000000003",
        "2000000000000000002",
        "2000000000000000001",
        "2000000000000000004",
        "2000000000000000006",
        "2000000000000000005",
    ]
    assert rows[3]["lottery_time"] is None


def test_same_draw_time_keeps_dynamic_id_tiebreaker(monkeypatch) -> None:
    draw_time = int(time.time()) + 3600
    activities = [
        _activity("2000000000000000002", draw_time),
        _activity("2000000000000000001", draw_time),
    ]
    _patch_activity_sources(monkeypatch, activities)

    rows = activity_service._filtered_activity_rows()

    assert [row["dynamic_id"] for row in rows] == [
        "2000000000000000001",
        "2000000000000000002",
    ]


def test_historical_inferred_date_is_unknown_for_api_and_selector(monkeypatch) -> None:
    now = int(time.time())
    activities = [
        _activity("2000000000000000001", now + 3600, inferred=False),
        _activity("2000000000000000002", now + 7200, inferred=True),
    ]
    activities[1]["conditions"]["lottery_time_text"] = "2027-03-13 23:29"
    _patch_activity_sources(monkeypatch, activities)

    rows = activity_service._filtered_activity_rows()

    assert rows[1]["lottery_time"] is None
    assert [row["dynamic_id"] for row in activity_service._pick_triple_from_rows(rows)] == [
        "2000000000000000001",
        "2000000000000000002",
    ]


def test_real_2027_date_is_not_treated_as_unknown() -> None:
    from datetime import datetime

    from src.lottery_time import BEIJING_TZ, reliable_lottery_time_unix

    stamp = int(datetime(2027, 3, 13, tzinfo=BEIJING_TZ).timestamp())
    item = _activity("2000000000000000001", stamp)

    assert reliable_lottery_time_unix(item) == stamp


def test_invalid_official_notice_time_becomes_unknown() -> None:
    from src.lottery_enricher import _apply_default_lottery_time, _build_from_notice

    activity = _build_from_notice(
        dynamic_id="2000000000000000001",
        lottery_type="互动抽奖",
        notice={
            "status": 0,
            "lottery_time": "not-a-timestamp",
            "first_prize": 1,
            "first_prize_cmt": "奖品",
            "participants": 0,
        },
        business_type=1,
        business_id="1",
        from_cache=False,
    )

    updated = _apply_default_lottery_time(activity)
    assert updated.lottery_time is None
    assert updated.conditions["lottery_time_inferred"] is True


def test_heat_sort_remains_repost_count_order(monkeypatch) -> None:
    now = int(time.time())
    first = _activity("2000000000000000001", now + 86400)
    second = _activity("2000000000000000002", now + 3600)
    first["repost_count"] = 100
    second["repost_count"] = 10
    first["repost_fetched"] = second["repost_fetched"] = True
    _patch_activity_sources(monkeypatch, [first, second])

    rows = activity_service._filtered_activity_rows(sort="heat", order="desc")

    assert [row["dynamic_id"] for row in rows] == [
        "2000000000000000001",
        "2000000000000000002",
    ]


def test_selector_guards_still_win_over_earlier_draw_time(monkeypatch) -> None:
    monkeypatch.setattr(activity_service, "is_comment_breaker_active", lambda: False)
    now = int(time.time())
    rows = [
        {
            **_activity("2000000000000000001", now + 3600),
            "last_participation": {"context_snapshot": {"result_class": "pending"}},
        },
        {
            **_activity("2000000000000000002", now + 7200),
            "last_participation": {"context_snapshot": {"result_class": "unknown"}},
        },
        _activity("2000000000000000003", now + 86400),
        _activity("2000000000000000004", now + 2 * 86400),
        _activity("2000000000000000005", None, inferred=True),
    ]

    assert [row["dynamic_id"] for row in activity_service._pick_triple_from_rows(rows)] == [
        "2000000000000000003",
        "2000000000000000004",
        "2000000000000000005",
    ]
