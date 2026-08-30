from __future__ import annotations

import httpx
import pytest

from src.pipeline.refresh_all_pipeline import run_new_links_pipeline
from src.pipeline.classify_step import ClassifyOutcome


DYNAMIC_ID = "1224962472871460885"
DYNAMIC_URL = f"https://www.bilibili.com/opus/{DYNAMIC_ID}"
GOOD_DYNAMIC_ID = "1224962472871460886"
GOOD_DYNAMIC_URL = f"https://www.bilibili.com/opus/{GOOD_DYNAMIC_ID}"


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _run_with_classify_error(monkeypatch, error: Exception):
    def raise_classify_error(client, dynamic_id):
        raise error

    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.known_activity_ids",
        lambda: set(),
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.load_participations",
        lambda: {},
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.BilibiliClient",
        _Client,
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.classify_new_link",
        raise_classify_error,
    )
    return run_new_links_pipeline([DYNAMIC_URL], workers=1)


@pytest.mark.parametrize("error", [
    httpx.HTTPStatusError(
        "missing",
        request=httpx.Request("GET", DYNAMIC_URL),
        response=httpx.Response(
            404,
            request=httpx.Request("GET", DYNAMIC_URL),
        ),
    ),
    RuntimeError("404 Client Error: Not Found"),
])
def test_classify_not_found_skips_only_bad_dynamic(monkeypatch, error) -> None:
    result = _run_with_classify_error(monkeypatch, error)

    assert result.ok is True
    assert result.new_link_count == 1
    assert result.classified_count == 0
    assert result.skipped_count == 1
    assert result.skip_reasons == {"链接失效": 1}


def test_classify_unreadable_content_skips_only_bad_dynamic(monkeypatch) -> None:
    result = _run_with_classify_error(
        monkeypatch,
        RuntimeError(f"无法获取动态正文: {DYNAMIC_ID}"),
    )

    assert result.ok is True
    assert result.new_link_count == 1
    assert result.classified_count == 0
    assert result.skipped_count == 1
    assert result.skip_reasons == {"正文不可读取": 1}


def test_classify_unknown_error_is_not_swallowed(monkeypatch) -> None:
    with pytest.raises(RuntimeError, match="未知分类异常"):
        _run_with_classify_error(monkeypatch, RuntimeError("未知分类异常"))


@pytest.mark.parametrize(
    ("bad_error", "expected_reason"),
    [
        (RuntimeError("404 Client Error: Not Found"), "链接失效"),
        (RuntimeError(f"无法获取动态正文: {DYNAMIC_ID}"), "正文不可读取"),
    ],
)
def test_bad_dynamic_does_not_stop_later_good_dynamic(
    monkeypatch,
    bad_error: RuntimeError,
    expected_reason: str,
) -> None:
    """bad 必须只跳过自身；若 continue 被误改为 break，本测试应失败。"""
    classified_ids: list[str] = []
    persisted_rows: list[dict] = []

    def classify_one(client, dynamic_id):
        classified_ids.append(dynamic_id)
        if dynamic_id == DYNAMIC_ID:
            raise bad_error
        return ClassifyOutcome(
            dynamic_id=dynamic_id,
            lottery_type="互动抽奖",
            skipped=False,
        )

    class GoodActivity:
        dynamic_id = GOOD_DYNAMIC_ID
        skipped = False

        def to_dict(self) -> dict:
            return {"dynamic_id": self.dynamic_id}

    def persist(rows: list[dict]) -> int:
        persisted_rows.extend(rows)
        return len(rows)

    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.known_activity_ids",
        lambda: set(),
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.load_participations",
        lambda: {},
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.BilibiliClient",
        _Client,
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.classify_new_link",
        classify_one,
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.enrich_activity",
        lambda *args, **kwargs: GoodActivity(),
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.apply_initial_status",
        lambda row: {**row, "status_classified": True},
    )
    monkeypatch.setattr(
        "src.pipeline.refresh_all_pipeline.append_activities",
        persist,
    )

    result = run_new_links_pipeline(
        [DYNAMIC_URL, GOOD_DYNAMIC_URL],
        workers=1,
    )

    assert classified_ids == [DYNAMIC_ID, GOOD_DYNAMIC_ID]
    assert persisted_rows == [
        {"dynamic_id": GOOD_DYNAMIC_ID, "status_classified": True}
    ]
    assert result.ok is True
    assert result.new_link_count == 2
    assert result.classified_count == 1
    assert result.skipped_count == 1
    assert result.enriched_count == 1
    assert result.persisted_count == 1
    assert result.skip_reasons == {expected_reason: 1}
