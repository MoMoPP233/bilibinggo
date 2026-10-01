from __future__ import annotations

from datetime import datetime, timedelta, timezone


CN_TZ = timezone(timedelta(hours=8))


def daytime_cn_clock() -> datetime:
    """Stable daytime wall clock for scheduler tests that are not about night mode."""
    return datetime(2026, 10, 1, 10, 0, tzinfo=CN_TZ)
