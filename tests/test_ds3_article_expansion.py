from __future__ import annotations

import logging
from dataclasses import replace
from unittest.mock import MagicMock

import pytest

from src.pipeline import refresh_all_pipeline as pipeline
from src.pipeline.classify_step import ClassifyOutcome
from src.sources import ds3_gongjuren as ds3
from src.sources.common import CheckResult, opus_link


CONTAINER = "1220000000000000001"
OTHER_CONTAINER = "1220000000000000002"
HISTORICAL_CONTAINER = "1220000000000000003"
LEAF_A = "1220000000000000011"
LEAF_B = "1220000000000000012"
LEAF_C = "1220000000000000013"


def _rich(dynamic_id: str, *, url: str | None = None, kind: str = "OPUS") -> dict:
    return {
        "type": "TEXT_NODE_TYPE_RICH",
        "rich": {
            "type": f"RICH_TEXT_NODE_TYPE_{kind}",
            "rid": dynamic_id if kind == "OPUS" else "",
            "jump_url": url or opus_link(dynamic_id),
        },
    }


def _word(text: str) -> dict:
    return {"type": "TEXT_NODE_TYPE_WORD", "word": {"words": text}}


def _article(*nodes: dict, uid: str | None = None, modules_dict: bool = False) -> dict:
    content = {"paragraphs": [{"para_type": 1, "text": {"nodes": list(nodes)}}]}
    modules = {"module_content": content}
    return {
        "type": 1,
        "basic": {"comment_type": 12, "uid": uid or str(ds3.MID)},
        "modules": modules if modules_dict else [modules],
    }


def _dynamic() -> dict:
    item = _article(_rich(LEAF_B))
    item["type"] = 0
    item["basic"]["comment_type"] = 17
    return item


def _mock_items(monkeypatch, items: dict[str, dict | Exception | None]) -> list[str]:
    fetched: list[str] = []

    def fetch(client, dynamic_id):
        fetched.append(dynamic_id)
        item = items.get(dynamic_id, _dynamic())
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(ds3, "fetch_opus_detail_item_strict", fetch)
    return fetched


@pytest.mark.parametrize("modules_dict", [False, True])
def test_container_expands_three_leaf_links(monkeypatch, modules_dict) -> None:
    _mock_items(
        monkeypatch,
        {CONTAINER: _article(*map(_rich, (LEAF_A, LEAF_B, LEAF_C)), modules_dict=modules_dict)},
    )

    links, hints = ds3.expand_container_links(
        object(), [opus_link(CONTAINER)], {CONTAINER: "转发抽奖"}
    )

    assert links == list(map(opus_link, (LEAF_A, LEAF_B, LEAF_C)))
    assert opus_link(CONTAINER) not in links
    assert CONTAINER not in hints


def test_duplicate_id_in_multiple_link_forms_is_processed_once(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(
                _rich(LEAF_A),
                _rich(LEAF_A, url=f"https://t.bilibili.com/{LEAF_A}", kind="WEB"),
                _word(f"同一活动 https://www.bilibili.com/opus/{LEAF_A}"),
                _rich(LEAF_B),
            )
        },
    )

    links, _ = ds3.expand_container_links(
        object(), [opus_link(CONTAINER), f"https://t.bilibili.com/{LEAF_A}"]
    )

    assert links == [opus_link(LEAF_A), opus_link(LEAF_B)]
    # Only outer-cv candidates are checked; the same ID encountered inside the
    # collection must not cause another detail request.
    assert fetched == [CONTAINER, LEAF_A]


def test_mutual_root_containers_do_not_loop_or_emit_containers(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(_rich(OTHER_CONTAINER), _rich(LEAF_A), _rich(CONTAINER)),
            OTHER_CONTAINER: _article(_rich(CONTAINER), _rich(LEAF_B)),
        },
    )

    links, _ = ds3.expand_container_links(
        object(), [opus_link(CONTAINER), opus_link(OTHER_CONTAINER)]
    )

    assert set(links) == {opus_link(LEAF_A), opus_link(LEAF_B)}
    assert len(links) == 2
    assert fetched.count(CONTAINER) == 1
    assert fetched.count(OTHER_CONTAINER) == 1


def test_body_opus_link_without_container_metadata_is_a_leaf(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(_rich(HISTORICAL_CONTAINER), _rich(LEAF_A)),
            HISTORICAL_CONTAINER: _article(_rich(LEAF_B)),
        },
    )
    links, _ = ds3.expand_container_links(object(), [opus_link(CONTAINER)])

    # OPUS indicates a resource type, not a collection. Do not fetch the child
    # merely to discover whether it happens to be another historical article.
    assert links == [opus_link(HISTORICAL_CONTAINER), opus_link(LEAF_A)]
    assert fetched == [CONTAINER]


@pytest.mark.parametrize("modules", [[{"module_content": {"paragraphs": []}}], []])
def test_empty_or_unreadable_container_never_falls_back(monkeypatch, caplog, modules) -> None:
    item = _article()
    item["modules"] = modules
    _mock_items(monkeypatch, {CONTAINER: item})

    with caplog.at_level(logging.WARNING):
        links, hints = ds3.expand_container_links(
            object(), [opus_link(CONTAINER), opus_link(LEAF_A)], {CONTAINER: "转发抽奖"}
        )

    assert links == [opus_link(LEAF_A)]
    assert CONTAINER not in hints
    assert opus_link(CONTAINER) in caplog.text
    assert any("跳过" in record.getMessage() or "skip" in record.getMessage() for record in caplog.records)


def test_known_read_failure_skips_container_and_continues(monkeypatch, caplog) -> None:
    from src.lottery_api import OpusReadError

    _mock_items(monkeypatch, {CONTAINER: OpusReadError("正文已删除")})
    with caplog.at_level(logging.WARNING):
        links, _ = ds3.expand_container_links(
            object(), [opus_link(CONTAINER), opus_link(LEAF_A)]
        )

    assert links == [opus_link(LEAF_A)]
    assert opus_link(CONTAINER) in caplog.text
    assert "正文已删除" in caplog.text


def test_unknown_error_is_not_swallowed(monkeypatch) -> None:
    _mock_items(monkeypatch, {CONTAINER: RuntimeError("未知系统异常")})
    with pytest.raises(RuntimeError, match="未知系统异常"):
        ds3.expand_container_links(object(), [opus_link(CONTAINER), opus_link(LEAF_A)])


def test_malformed_container_structure_is_not_swallowed(monkeypatch) -> None:
    item = _article()
    item["modules"] = "unexpected server schema"
    _mock_items(monkeypatch, {CONTAINER: item})
    with pytest.raises(TypeError):
        ds3.expand_container_links(object(), [opus_link(CONTAINER)])


@pytest.mark.parametrize("item_type", [None, True, False, 0.0, 1.0, "1", 2])
def test_unknown_opus_type_is_not_assumed_to_be_an_ordinary_dynamic(monkeypatch, item_type) -> None:
    item = _article(_rich(LEAF_A))
    item["type"] = item_type
    _mock_items(monkeypatch, {CONTAINER: item})
    with pytest.raises(ValueError):
        ds3.expand_container_links(object(), [opus_link(CONTAINER)])


def test_real_module_layout_skips_null_fields_and_combines_content(monkeypatch) -> None:
    item = _article(_rich(LEAF_A))
    item["modules"] = [
        {"module_author": {"mid": ds3.MID}, "module_content": None},
        item["modules"][0],
        _article(_rich(LEAF_B))["modules"][0],
    ]
    _mock_items(monkeypatch, {CONTAINER: item})
    links, _ = ds3.expand_container_links(object(), [opus_link(CONTAINER)])
    assert links == [opus_link(LEAF_A), opus_link(LEAF_B)]


def test_web_node_never_uses_unrelated_rid_as_a_dynamic_id(monkeypatch) -> None:
    video = _rich(LEAF_A, kind="WEB", url="https://www.bilibili.com/video/BV1uw41137jH")
    video["rich"]["rid"] = LEAF_A
    dynamic = _rich(LEAF_A, kind="WEB", url=f"https://t.bilibili.com/{LEAF_B}")
    dynamic["rich"]["rid"] = LEAF_A
    fetched = _mock_items(monkeypatch, {CONTAINER: _article(video, dynamic)})
    links, _ = ds3.expand_container_links(object(), [opus_link(CONTAINER)])
    assert links == [opus_link(LEAF_B)]
    assert LEAF_A not in fetched


def test_direct_web_dynamic_leaves_do_not_add_opus_requests(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {CONTAINER: _article(*(
            _rich(dynamic_id, kind="WEB", url=f"https://t.bilibili.com/{dynamic_id}")
            for dynamic_id in (LEAF_A, LEAF_B, LEAF_C)
        ))},
    )
    links, _ = ds3.expand_container_links(object(), [opus_link(CONTAINER)])
    assert links == list(map(opus_link, (LEAF_A, LEAF_B, LEAF_C)))
    assert fetched == [CONTAINER]


def test_fifty_five_opus_leaves_require_only_one_container_detail_request(monkeypatch) -> None:
    leaf_ids = [str(1220000000000010000 + index) for index in range(55)]
    fetched = _mock_items(
        monkeypatch, {CONTAINER: _article(*map(_rich, leaf_ids))}
    )
    progress: list[tuple[int, int, str]] = []

    links, _ = ds3.expand_container_links(
        object(), [opus_link(CONTAINER)],
        on_progress=lambda done, total, message: progress.append((done, total, message)),
    )

    assert links == list(map(opus_link, leaf_ids))
    assert fetched == [CONTAINER]
    assert any("55" in message and "候选" in message for _, _, message in progress)


def test_multiple_outer_containers_are_each_expanded_once(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(_rich(LEAF_A), _rich(LEAF_B)),
            OTHER_CONTAINER: _article(_rich(LEAF_B), _rich(LEAF_C)),
        },
    )

    links, _ = ds3.expand_container_links(
        object(), [opus_link(CONTAINER), opus_link(OTHER_CONTAINER)]
    )

    assert links == list(map(opus_link, (LEAF_A, LEAF_B, LEAF_C)))
    assert fetched == [CONTAINER, OTHER_CONTAINER]


def test_risk_control_stops_detail_requests_and_preserves_successful_leaves(monkeypatch, caplog) -> None:
    from src.lottery_api import OpusRiskControlError

    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(
                _rich(LEAF_A), _rich(LEAF_B),
                _rich(OTHER_CONTAINER), _rich(HISTORICAL_CONTAINER),
            ),
            OTHER_CONTAINER: OpusRiskControlError("opus/detail API error -352: 风控"),
            HISTORICAL_CONTAINER: _article(_rich(LEAF_C)),
        },
    )
    progress: list[tuple[int, int, str]] = []
    with caplog.at_level(logging.WARNING):
        links, hints = ds3.expand_container_links(
            object(),
            list(map(opus_link, (CONTAINER, OTHER_CONTAINER, HISTORICAL_CONTAINER, LEAF_C))),
            {CONTAINER: "转发抽奖", LEAF_C: "互动抽奖"},
            direct_dynamic_ids={LEAF_C},
            on_progress=lambda done, total, message: progress.append((done, total, message)),
        )

    assert fetched == [CONTAINER, OTHER_CONTAINER]
    assert links == list(map(opus_link, (LEAF_A, LEAF_B, LEAF_C)))
    # Failed/unprobed roots must not leak back through links in an earlier body.
    for container_id in (CONTAINER, OTHER_CONTAINER, HISTORICAL_CONTAINER):
        assert opus_link(container_id) not in links
        assert container_id not in hints
    assert hints[LEAF_C] == "互动抽奖"
    assert opus_link(OTHER_CONTAINER) in caplog.text
    assert opus_link(HISTORICAL_CONTAINER) in caplog.text
    assert "-352" in caplog.text
    assert any("部分合集读取失败" in message for _, _, message in progress)


@pytest.mark.parametrize("read_failure", [False, True])
def test_body_opus_node_does_not_upgrade_leaf_alias_to_container(monkeypatch, read_failure) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(
                _rich(HISTORICAL_CONTAINER, kind="WEB", url=f"https://t.bilibili.com/{HISTORICAL_CONTAINER}"),
                _rich(LEAF_A),
            ),
            OTHER_CONTAINER: _article(_rich(HISTORICAL_CONTAINER)),
            HISTORICAL_CONTAINER: (
                ds3.OpusReadError("正文已删除") if read_failure else _article(_rich(LEAF_B))
            ),
        },
    )
    links, hints = ds3.expand_container_links(
        object(), [opus_link(CONTAINER), opus_link(OTHER_CONTAINER)], {CONTAINER: "转发抽奖"}
    )
    assert links == [opus_link(HISTORICAL_CONTAINER), opus_link(LEAF_A)]
    assert hints[HISTORICAL_CONTAINER] == "转发抽奖"
    assert fetched == [CONTAINER, OTHER_CONTAINER]


def test_body_opus_link_does_not_override_explicit_root_dynamic_alias(monkeypatch) -> None:
    fetched = _mock_items(
        monkeypatch,
        {
            CONTAINER: _article(_rich(LEAF_A)),
            OTHER_CONTAINER: _article(_rich(CONTAINER)),
        },
    )
    links, _ = ds3.expand_container_links(
        object(), [opus_link(CONTAINER), opus_link(OTHER_CONTAINER)],
        direct_dynamic_ids={CONTAINER},
    )
    assert links == [opus_link(CONTAINER)]
    assert fetched == [OTHER_CONTAINER]


@pytest.mark.parametrize("item", [_dynamic(), None, _article(_rich(LEAF_B), uid="987654321")])
def test_real_dynamic_or_other_author_article_keeps_original_path(monkeypatch, item) -> None:
    _mock_items(monkeypatch, {LEAF_A: item})
    links, hints = ds3.expand_container_links(
        object(), [opus_link(LEAF_A)], {LEAF_A: "互动抽奖"}
    )
    assert links == [opus_link(LEAF_A)]
    assert hints == {LEAF_A: "互动抽奖"}


def _source_client(monkeypatch) -> MagicMock:
    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.get_latest_article.return_value = {
        "id": 999001,
        "title": "最新外层专栏标题",
        "publish_time": 1700000000,
    }
    client.get_article_detail.return_value = {
        "title": "外层详情标题",
        "publish_time": 1700000010,
        "opus": {
            "content": {
                "paragraphs": [
                    {"text": {"nodes": [{"node_type": 4, "link": {"biz_id": CONTAINER}}]}}
                ]
            }
        },
    }
    monkeypatch.setattr(ds3, "BilibiliClient", lambda: client)
    return client


def test_check_update_preserves_outer_cv_metadata(monkeypatch) -> None:
    client = _source_client(monkeypatch)
    previous = "https://www.bilibili.com/read/cv888001"
    monkeypatch.setattr(ds3, "get_last_container", lambda source_id: previous)
    _mock_items(monkeypatch, {CONTAINER: _article(_rich(LEAF_A))})

    result = ds3.check_update()

    assert result.source_id == "DS-3"
    assert result.updated is True
    assert result.container_url == "https://www.bilibili.com/read/cv999001"
    assert result.container_id == "999001"
    assert result.title == "外层详情标题"
    assert result.published_at == 1700000010
    assert result.previous_container_url == previous
    assert result.activity_links == [opus_link(LEAF_A)]
    client.get_latest_article.assert_called_once_with(ds3.MID)
    client.get_article_detail.assert_called_once_with(999001)


def test_check_update_emits_ordered_progress_during_source_check(monkeypatch) -> None:
    client = _source_client(monkeypatch)
    monkeypatch.setattr(ds3, "get_last_container", lambda source_id: None)
    progress: list[tuple[int, int, str]] = []

    def fetch(client_arg, dynamic_id):
        assert client_arg is client
        assert dynamic_id == CONTAINER
        # Progress must already be visible while the network call is running,
        # not buffered until source check has completed.
        assert any("最新专栏" in message for _, _, message in progress)
        assert any("分析" in message and "合集" in message for _, _, message in progress)
        assert any("检查候选" in message for _, _, message in progress)
        return _article(_rich(LEAF_A), _rich(LEAF_B), _rich(LEAF_C))

    monkeypatch.setattr(ds3, "fetch_opus_detail_item_strict", fetch)
    result = ds3.check_update(
        on_progress=lambda done, total, message: progress.append((done, total, message))
    )

    assert result.activity_links == list(map(opus_link, (LEAF_A, LEAF_B, LEAF_C)))
    messages = [message for _, _, message in progress]
    latest_index = next(index for index, message in enumerate(messages) if "最新专栏" in message)
    analyze_index = next(index for index, message in enumerate(messages) if "分析" in message and "合集" in message)
    expand_index = next(index for index, message in enumerate(messages) if "展开" in message)
    candidates_index = next(index for index, message in enumerate(messages) if "3" in message and "候选" in message)
    organize_index = next(index for index, message in enumerate(messages) if "整理" in message)
    assert latest_index < analyze_index < expand_index < candidates_index <= organize_index
    assert all(isinstance(done, int) and isinstance(total, int) for done, total, _ in progress)


def test_check_update_preserves_direct_dynamic_path_without_extra_opus_lookup(monkeypatch) -> None:
    client = _source_client(monkeypatch)
    client.get_article_detail.return_value["opus"]["content"]["paragraphs"] = [
        {"text": {"nodes": [{"node_type": 4, "link": {
            "link_type": 16, "link": f"https://t.bilibili.com/{LEAF_A}",
        }}]}}
    ]
    monkeypatch.setattr(ds3, "get_last_container", lambda source_id: None)
    probe = MagicMock(side_effect=AssertionError("直接动态不应额外查询 Opus"))
    monkeypatch.setattr(ds3, "fetch_opus_detail_item_strict", probe)
    result = ds3.check_update()
    assert result.activity_links == [opus_link(LEAF_A)]
    probe.assert_not_called()


@pytest.mark.parametrize("marker", ["上期传送门", "本期完"])
@pytest.mark.parametrize("history_first", [False, True])
def test_excluded_history_opus_alias_does_not_trigger_detail_for_direct_dynamic(
    monkeypatch, marker, history_first
) -> None:
    client = _source_client(monkeypatch)
    current_paragraph = {"text": {"nodes": [{"node_type": 4, "link": {
        "link_type": 16, "link": f"https://t.bilibili.com/{LEAF_A}",
    }}]}}
    historical_paragraph = {"text": {"nodes": [
        {"node_type": 1, "word": {"words": marker}},
        {"node_type": 4, "link": {
            "biz_id": LEAF_A, "link_type": 39, "link": opus_link(LEAF_A),
        }},
    ]}}
    paragraphs = [historical_paragraph, current_paragraph] if history_first else [
        current_paragraph, historical_paragraph,
    ]
    client.get_article_detail.return_value["opus"]["content"]["paragraphs"] = paragraphs
    monkeypatch.setattr(ds3, "get_last_container", lambda source_id: None)
    probe = MagicMock(side_effect=AssertionError("已排除历史段落不得把直接动态升级为 Opus 候选"))
    monkeypatch.setattr(ds3, "fetch_opus_detail_item_strict", probe)

    result = ds3.check_update()

    assert result.activity_links == [opus_link(LEAF_A)]
    probe.assert_not_called()


def test_unchanged_outer_cv_reuses_snapshot_without_expansion(monkeypatch) -> None:
    client = _source_client(monkeypatch)
    monkeypatch.setattr(ds3, "get_last_container", lambda source_id: ds3.article_url(999001))
    monkeypatch.setattr(
        ds3,
        "load_previous_output",
        lambda path: {"activity_links": [opus_link(LEAF_A)], "link_hints": {LEAF_A: "预约抽奖"}},
    )
    expand = MagicMock(side_effect=AssertionError("unchanged 专栏不应再次展开"))
    monkeypatch.setattr(ds3, "expand_container_links", expand)

    result = ds3.check_update()

    assert result.updated is False
    assert result.activity_links == [opus_link(LEAF_A)]
    assert result.link_hints == {LEAF_A: "预约抽奖"}
    client.get_article_detail.assert_not_called()
    expand.assert_not_called()


def test_leaf_ids_use_existing_pipeline_classification_and_database_dedup(monkeypatch) -> None:
    _mock_items(monkeypatch, {CONTAINER: _article(*map(_rich, (LEAF_A, LEAF_B, LEAF_C)))})
    links, hints = ds3.expand_container_links(object(), [opus_link(CONTAINER)])
    source = CheckResult(
        source_id="DS-3", updated=True, container_url=ds3.article_url(999001),
        container_id="999001", title="专栏", published_at=1,
        previous_container_url=None, activity_links=links, checked_at=2, link_hints=hints,
    )
    classified: list[str] = []
    persisted: list[dict] = []

    def classify(client, dynamic_id):
        classified.append(dynamic_id)
        if dynamic_id in (LEAF_B, OTHER_CONTAINER):
            return ClassifyOutcome(dynamic_id, "非抽奖活动", True, "非抽奖活动")
        return ClassifyOutcome(dynamic_id, "互动抽奖", False)

    class Activity:
        dynamic_id = LEAF_A
        skipped = False

        def to_dict(self):
            return {"dynamic_id": self.dynamic_id, "lottery_type": "互动抽奖"}

    client = MagicMock()
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    monkeypatch.setattr(pipeline, "BilibiliClient", lambda: client)
    monkeypatch.setattr(pipeline, "known_activity_ids", lambda: {LEAF_C})
    monkeypatch.setattr(pipeline, "load_participations", lambda: {})
    monkeypatch.setattr(pipeline, "classify_new_link", classify)
    monkeypatch.setattr(pipeline, "enrich_activity", lambda *args, **kwargs: Activity())
    monkeypatch.setattr(pipeline, "apply_initial_status", lambda row: row)

    def persist(rows):
        persisted.extend(rows)
        return len(rows)

    monkeypatch.setattr(pipeline, "append_activities", persist)
    other_source = replace(
        source,
        source_id="DS-2",
        activity_links=[opus_link(OTHER_CONTAINER), f"https://t.bilibili.com/{LEAF_A}"],
    )
    result = pipeline.run_refresh_all_pipeline([source, other_source], workers=1)

    assert classified == [LEAF_A, LEAF_B, OTHER_CONTAINER]
    assert persisted == [{"dynamic_id": LEAF_A, "lottery_type": "互动抽奖"}]
    assert result.new_link_count == 3
    assert result.persisted_count == 1
    assert result.skip_reasons == {"非抽奖活动": 2}
