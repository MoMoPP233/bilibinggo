from __future__ import annotations

from src.lottery_enricher import EnrichedActivity, PrizeTier, _apply_default_lottery_time
from src.lottery_time import is_lottery_time_inferred


def test_apply_default_lottery_time_on_missing() -> None:
    activity = EnrichedActivity(
        dynamic_id="1000000000000000001",
        source_url="https://www.bilibili.com/opus/1000000000000000001",
        lottery_type="转发抽奖",
        enriched_at=1_000_000,
        business_id="1000000000000000001",
        business_type=0,
        draw_status="active",
        lottery_time=None,
        prizes=[PrizeTier(tier="first", winner_count=1, description="奖品")],
        participants=0,
        conditions={},
        winners=None,
        platform_participated=None,
        repost_fetched=True,
    )
    updated = _apply_default_lottery_time(activity)
    assert updated.lottery_time is None
    assert updated.conditions.get("lottery_time_inferred") is True
    assert is_lottery_time_inferred(updated.to_dict()) is True
    assert "lottery_time_text" not in updated.conditions
