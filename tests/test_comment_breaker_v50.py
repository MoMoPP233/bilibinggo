from __future__ import annotations

from unittest.mock import Mock

import pytest

from src import lottery_actions as actions
from src import participation as flow
from src.comment_breaker import (
    CommentLimitActive,
    clear_comment_breaker,
    is_comment_breaker_active,
    load_comment_breaker,
    trigger_comment_limit,
)
from src.participation_errors import ParticipationReadError


@pytest.fixture(autouse=True)
def _local_login(monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("csrf", 42))


def _comment_kwargs() -> dict:
    return {
        "rid": "123",
        "comment_type": 17,
        "message": "test",
        "csrf": "csrf",
        "referer": "https://www.bilibili.com/opus/123",
        "uid": 42,
    }


def test_12014_persists_profile_local_breaker_and_blocks_next_comment(isolated_home) -> None:
    client = Mock()
    client.post_form.side_effect = [
        {"code": 12014, "message": "评论频率达到上限"},
        {"code": 0},
    ]

    first = actions.comment_dynamic(client, **_comment_kwargs())
    assert first.ok is False
    assert first.detail.startswith("code=12014")
    assert is_comment_breaker_active() is True
    persisted = load_comment_breaker()
    assert persisted["active"] is True
    assert persisted["code"] == 12014

    with pytest.raises(CommentLimitActive):
        actions.comment_dynamic(client, **_comment_kwargs())
    assert client.post_form.call_count == 1

    clear_comment_breaker()
    assert is_comment_breaker_active() is False
    assert actions.comment_dynamic(client, **_comment_kwargs()).ok is True
    assert client.post_form.call_count == 2


def test_breaker_persists_across_module_reads_and_is_profile_local(isolated_home, monkeypatch) -> None:
    trigger_comment_limit(message="fixture")
    assert load_comment_breaker()["active"] is True

    import src.comment_breaker as breaker

    other_profile = isolated_home / "profiles" / "account-2"
    other_profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(breaker, "get_profile_dir", lambda: other_profile)
    assert breaker.is_comment_breaker_active() is False


def test_active_comment_breaker_returns_deferred_without_remote_reads(isolated_home, monkeypatch) -> None:
    trigger_comment_limit()
    monkeypatch.setattr(flow, "require_login", lambda: ("csrf", 177466156))
    client = Mock()

    result = flow.participate_five_action_lottery(
        client,
        dynamic_id="1000000000000000001",
        lottery_type="互动抽奖",
        persist=True,
    )

    assert result.status == "deferred"
    assert result.to_dict()["result_class"] == "deferred_comment_limit"
    client.get_json.assert_not_called()
    client.request_json.assert_not_called()
    client.post_form.assert_not_called()


def test_participation_read_error_keeps_structured_diagnostics() -> None:
    raw = Mock()
    raw.request_json.return_value = {"code": 4101152, "message": "动态不存在"}
    reader = actions.ParticipationReadClient(raw)

    with pytest.raises(ParticipationReadError) as caught:
        reader.get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail",
            {"id": "123456789"},
        )

    error = caught.value
    assert error.code == 4101152
    assert error.message.find("动态不存在") >= 0
    assert error.endpoint.endswith("/x/polymer/web-dynamic/v1/detail")
    assert error.dynamic_id == "123456789"
