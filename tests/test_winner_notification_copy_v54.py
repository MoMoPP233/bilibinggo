"""V54 winner notification copy tests (offline provider payloads only)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from src.notification import service as nsvc
from src.winner_scan import service as winmod


TITLE = "Binggo中奖提醒｜炉石东少"
BODY = "恭喜你中奖了，请提供收件信息"


class _CaptureNotification:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def dispatch(self, event_id, title, body="", *, channels=None, force=False, event_type=None):
        self.calls.append({"event_id": event_id, "title": title, "body": body, "event_type": event_type})
        return {}


def _record(*, name: str = "炉石东少", sender_id: int = 204408558, summary: str | None = BODY) -> dict:
    return {
        "key": "dm:204408558:1",
        "source_type": "dm",
        "source_time": 1,
        "sender": {"id": sender_id, "name": name},
        "summary": summary,
        "source_url": "",
        "notification_event_id": "winning_candidate:dm:204408558:1",
    }


def test_winner_formatter_uses_sender_and_summary() -> None:
    capture = _CaptureNotification()
    winmod.WinnerScanService(notification=capture)._dispatch_new(
        {_record()["key"]: _record()}, [_record()["key"]], notify=True
    )

    assert capture.calls == [
        {
            "event_id": "winning_candidate:dm:204408558:1",
            "title": TITLE,
            "body": BODY,
            "event_type": "winning_candidate",
        }
    ]
    assert "[3/5]" not in capture.calls[0]["title"]
    assert "疑似中奖线索" not in capture.calls[0]["title"]


def test_winner_formatter_keeps_existing_fallbacks() -> None:
    record = _record(name="", sender_id=204408558, summary=None)
    title, body = winmod._format_winner_notification(record)

    assert title == "Binggo中奖提醒｜204408558"
    assert "摘要：" in body
    assert body


@pytest.mark.parametrize(
    "channel,request_factory",
    [
        (nsvc.CHANNEL_SERVERCHAN, lambda: nsvc.serverchan_request("SCT-test", TITLE, BODY)),
        (nsvc.CHANNEL_DINGTALK, lambda: nsvc.dingtalk_request("token", "", TITLE, BODY)),
        (nsvc.CHANNEL_WE_COM, lambda: nsvc.wecom_request("key", TITLE, BODY)),
        (nsvc.CHANNEL_FEISHU, lambda: nsvc.feishu_request("https://example.test/hook", "", TITLE, BODY)),
    ],
)
def test_all_provider_payloads_show_winner_copy(channel, request_factory) -> None:
    _url, request = request_factory()
    rendered = json.dumps(request, ensure_ascii=False)

    assert TITLE in rendered, channel
    assert BODY in rendered, channel
    assert "[3/5]" not in rendered
    assert "疑似中奖线索" not in rendered


def test_send_test_keeps_test_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: tmp_path)
    nsvc.save_channel_config(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT-test"})
    nsvc.set_channel_enabled(nsvc.CHANNEL_SERVERCHAN, True)
    posts: list[dict] = []

    class _Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, _url, **kwargs):
            posts.append(kwargs)
            return httpx.Response(200, json={"code": 0})

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _Client())
    nsvc.service.send_test(nsvc.CHANNEL_SERVERCHAN)

    rendered = json.dumps(posts[0], ensure_ascii=False)
    assert "Binggo 测试通知" in rendered
    assert "Binggo中奖提醒" not in rendered
