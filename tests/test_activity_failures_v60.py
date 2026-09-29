from __future__ import annotations

from src.activity_failures import (
    delete_activity_failure,
    list_activity_failures,
    record_activity_failure,
    resolve_activity_failure,
)
from src.activity_store import append_activities, load_activities


def test_activity_failure_is_profile_local_deduplicated_and_deletable(isolated_home) -> None:
    dynamic_id = "1000000000000000001"
    record_activity_failure(
        dynamic_id,
        source_url=f"https://t.bilibili.com/{dynamic_id}",
        reason="链接失效",
        failed_at=100,
    )
    record_activity_failure(
        dynamic_id,
        source_url=f"https://www.bilibili.com/opus/{dynamic_id}",
        reason="正文不可读取",
        failed_at=200,
    )

    rows = list_activity_failures()
    assert len(rows) == 1
    assert rows[0]["dynamic_id"] == dynamic_id
    assert rows[0]["reason"] == "正文不可读取"
    assert rows[0]["failed_at"] == 200

    append_activities(
        [
            {
                "dynamic_id": dynamic_id,
                "source_url": f"https://www.bilibili.com/opus/{dynamic_id}",
                "lottery_type": "互动抽奖",
            }
        ]
    )
    assert load_activities()[0]["dynamic_id"] == dynamic_id
    assert resolve_activity_failure(dynamic_id) is True
    assert list_activity_failures() == []

    record_activity_failure(dynamic_id, reason="链接失效", failed_at=300)
    assert delete_activity_failure(dynamic_id) is True
    assert list_activity_failures() == []
    assert load_activities()[0]["dynamic_id"] == dynamic_id
