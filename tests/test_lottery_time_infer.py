from __future__ import annotations

from src.lottery_time import resolve_effective_lottery_time_unix


def test_resolve_effective_lottery_time_does_not_infer_missing_value() -> None:
    item = {"dynamic_id": "1", "enriched_at": 1_600_000_000, "conditions": {}}
    assert resolve_effective_lottery_time_unix(item, persist=True) is None
    assert "lottery_time" not in item
