from __future__ import annotations

from unittest.mock import Mock

import httpx
import pytest

from src import lottery_actions as actions
from src.bilibili_client import BilibiliClient
from src.comment_confirmation import confirm_comment, is_comment_confirmed


DYNAMIC_ID = "1251573729479098372"
REFERER = f"https://www.bilibili.com/opus/{DYNAMIC_ID}"


@pytest.fixture(autouse=True)
def _local_login(monkeypatch):
    monkeypatch.setattr(actions, "require_login", lambda: ("csrf", 42))


def _context(*, commented: bool = False) -> actions.DynamicContext:
    return actions.DynamicContext(
        DYNAMIC_ID, 123, REFERER, DYNAMIC_ID, 17,
        True, True, True, True, True, commented,
    )


def _comment_kwargs() -> dict:
    return {
        "rid": DYNAMIC_ID,
        "comment_type": 17,
        "message": "new random text",
        "csrf": "csrf",
        "referer": REFERER,
        "dynamic_id": DYNAMIC_ID,
        "uid": 42,
    }


def test_successful_comment_is_durable_and_retry_skips_even_with_new_text(isolated_home, monkeypatch) -> None:
    monkeypatch.setattr(actions, "require_login", lambda: ("csrf", 42))
    monkeypatch.setattr(actions.time, "sleep", lambda _: None)
    client = Mock()
    client.post_form.return_value = {"code": 0}

    first, _ = actions.execute_full_participation(
        client, dynamic_id=DYNAMIC_ID, context=_context(), action_text="first random text"
    )
    assert first[-1].ok is True
    assert is_comment_confirmed(DYNAMIC_ID, uid=42) is True
    assert client.post_form.call_count == 1

    second, _ = actions.execute_full_participation(
        client, dynamic_id=DYNAMIC_ID, context=_context(), action_text="different random text"
    )
    assert second[-1] == actions.ActionResult("comment", True, "已评论，跳过")
    assert client.post_form.call_count == 1


def test_confirmation_is_profile_local_and_survives_module_reload(isolated_home, monkeypatch) -> None:
    confirm_comment(DYNAMIC_ID, uid=42)
    assert is_comment_confirmed(DYNAMIC_ID, uid=42) is True

    import importlib
    import src.comment_confirmation as confirmation

    importlib.reload(confirmation)
    assert confirmation.is_comment_confirmed(DYNAMIC_ID, uid=42) is True

    other_profile = isolated_home / "profiles" / "account-2"
    other_profile.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(confirmation, "get_profile_dir", lambda: other_profile)
    assert confirmation.is_comment_confirmed(DYNAMIC_ID, uid=42) is False


@pytest.mark.parametrize("code", [12078, 12014])
def test_comment_rejection_does_not_confirm(isolated_home, code: int) -> None:
    client = Mock()
    client.post_form.return_value = {"code": code, "message": "rejected"}

    result = actions.comment_dynamic(client, **_comment_kwargs())

    assert result.ok is False
    assert is_comment_confirmed(DYNAMIC_ID, uid=42) is False


def test_comment_transport_failure_is_not_retried(isolated_home, monkeypatch) -> None:
    client = BilibiliClient(warmup=False)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(client, "_http_post", fail_once)
    with pytest.raises(RuntimeError, match="网络请求失败"):
        actions.comment_dynamic(client, **_comment_kwargs())
    assert calls == 1
    assert is_comment_confirmed(DYNAMIC_ID, uid=42) is False
    client.close()
