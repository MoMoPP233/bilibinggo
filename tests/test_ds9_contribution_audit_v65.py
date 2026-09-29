from __future__ import annotations

from src.ds9_contribution_audit import (
    RETENTION_SECONDS,
    list_page_contributions,
    list_unique_valid,
    record_round,
)


X = "100000000000000001"


def _page(topic: str, page: int, dynamic_id: str = X) -> dict:
    return {
        "source_id": "DS-9",
        "topic_id": topic,
        "topic_name": topic,
        "page_index": page,
        "discovered_ids": [dynamic_id],
        "discovered_count": 1,
        "valid_lottery_count": 1,
        "ds9_unique_valid_count": 1,
    }


def _obs(source: str, topic: str, page: int, dynamic_id: str = X) -> dict:
    return {
        "source_id": source,
        "topic_id": topic,
        "topic_name": topic,
        "page_index": page,
        "dynamic_id": dynamic_id,
        "canonical_dynamic_id": dynamic_id,
        "outcome": "valid_lottery",
        "valid_lottery": True,
    }


def test_cross_source_order_is_not_unique(isolated_home) -> None:
    record_round(observations=[_obs("DS-9", "1261901", 1), _obs("DS-3", "", 0)], page_stats=[_page("1261901", 1)], now=100)
    assert list_unique_valid(since=0) == []

    # Reversing observation order produces the same overlap result.
    from sqlmodel import delete
    from src.db.models import Ds9ContributionCandidateRow, Ds9ContributionPageRow
    from src.db.session import session_scope

    with session_scope() as session:
        session.exec(delete(Ds9ContributionCandidateRow))
        session.exec(delete(Ds9ContributionPageRow))
    record_round(observations=[_obs("DS-3", "", 0), _obs("DS-9", "1261901", 1)], page_stats=[_page("1261901", 1)], now=100)
    assert list_unique_valid(since=0) == []


def test_cross_topic_canonical_duplicate_is_one_record(isolated_home) -> None:
    record_round(
        observations=[_obs("DS-9", "1261901", 1), _obs("DS-9", "47801", 2)],
        page_stats=[_page("1261901", 1), _page("47801", 2)],
        now=100,
    )
    assert len(list_unique_valid(since=0)) == 1
    assert len(list_page_contributions(since=0)) == 2


def test_later_other_source_removes_current_unique(isolated_home) -> None:
    record_round(observations=[_obs("DS-9", "1261901", 2)], page_stats=[_page("1261901", 2)], now=100)
    assert len(list_unique_valid(since=0)) == 1
    record_round(observations=[_obs("refresh_watch", "", 0)], page_stats=[], now=101)
    assert list_unique_valid(since=0) == []


def test_retention_is_bounded(isolated_home) -> None:
    record_round(observations=[_obs("DS-9", "1261901", 1)], page_stats=[_page("1261901", 1)], now=100)
    record_round(observations=[_obs("DS-9", "1261901", 1)], page_stats=[_page("1261901", 1)], now=RETENTION_SECONDS + 101)
    assert len(list_page_contributions(since=0)) == 1
