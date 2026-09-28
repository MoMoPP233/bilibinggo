from __future__ import annotations

import pytest

from src import participate_preflight as preflight
from src import participation as flow
from src.activity_store import activity_exists, replace_all_activities
from src.invalid_activities import invalid_activity_ids
from src.lottery_api import DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL
from src.participation_errors import ParticipationReadError


DID = "1000000000000000201"
WRAPPER = "1000000000000000202"
UID = "401"


class DetailReadClient:
    def __init__(self, *, code: int, message: str, endpoint: str = DYNAMIC_DETAIL_URL) -> None:
        self.code = code
        self.message = message
        self.endpoint = endpoint
        self.posts: list[str] = []

    def request_json(self, url, params=None, *, referer=None, retries=3):
        if url in (DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL):
            return {"code": self.code, "message": self.message}
        raise AssertionError(url)

    get_json = request_json

    def post_json(self, url, data, **kwargs):
        self.posts.append(url)
        return {"code": 0}

    def post_form(self, url, data, **kwargs):
        self.posts.append(url)
        return {"code": 0}


def _setup(monkeypatch, isolated_home):
    monkeypatch.setattr(flow, "require_login", lambda: ("csrf", int(UID)))
    replace_all_activities([
        {
            "dynamic_id": DID,
            "lottery_type": "转发抽奖",
            "activity_status": "未参加",
            "draw_status": "active",
            "lottery_time": 9_999_999_999,
            "observed_wrapper_dynamic_id": WRAPPER,
        }
    ])
    monkeypatch.setattr(
        preflight,
        "_load_activity_item",
        lambda dynamic_id: {
            "dynamic_id": dynamic_id,
            "lottery_type": "转发抽奖",
            "activity_status": "未参加",
            "draw_status": "active",
            "lottery_time": 9_999_999_999,
        },
    )


def test_confirmed_4101152_reuses_invalid_activity_tombstone(monkeypatch, isolated_home):
    _setup(monkeypatch, isolated_home)
    client = DetailReadClient(code=4101152, message="动态不可见")

    result = flow.participate_activity(
        client,
        dynamic_id=DID,
        lottery_type="转发抽奖",
        preflight=True,
    )

    payload = result.to_dict()
    assert payload["result_class"] == "invalid_activity"
    assert payload["context_snapshot"]["invalid_reason"] == "dynamic_invisible_4101152"
    assert not activity_exists(DID)
    assert DID in invalid_activity_ids()
    assert WRAPPER in invalid_activity_ids()
    assert client.posts == []


@pytest.mark.parametrize(
    ("message", "endpoint"),
    [
        ("其他错误", DYNAMIC_DETAIL_URL),
        ("动态不可见", "https://api.bilibili.com/x/other/detail"),
        ("动态不可见", "/x/other/detail"),
    ],
)
def test_4101152_mapping_rejects_unconfirmed_semantics(message: str, endpoint: str) -> None:
    error = ParticipationReadError(
        message,
        code=4101152,
        endpoint=endpoint,
        dynamic_id=DID,
    )
    assert not preflight._is_confirmed_invisible_detail_error(error)


@pytest.mark.parametrize("endpoint", [DYNAMIC_DETAIL_URL, "/x/polymer/web-dynamic/v1/detail"])
def test_4101152_mapping_accepts_confirmed_detail_endpoint(endpoint: str) -> None:
    error = ParticipationReadError(
        "动态不可见",
        code=4101152,
        endpoint=endpoint,
        dynamic_id=DID,
    )
    assert preflight._is_confirmed_invisible_detail_error(error)


def test_unconfirmed_read_error_does_not_tombstone_or_mutate(monkeypatch, isolated_home):
    _setup(monkeypatch, isolated_home)
    client = DetailReadClient(code=4101152, message="暂时不可见")

    with pytest.raises(ParticipationReadError):
        preflight._sync_live_fields(
            flow.ParticipationReadClient(client),
            {"dynamic_id": DID},
            lottery_type="转发抽奖",
        )

    assert activity_exists(DID)
    assert invalid_activity_ids() == set()
    assert client.posts == []


def test_normal_detail_response_keeps_preflight_behavior() -> None:
    class NormalClient(DetailReadClient):
        def request_json(self, url, params=None, *, referer=None, retries=3):
            if url in (DYNAMIC_DETAIL_URL, OPUS_DETAIL_URL):
                return {
                    "code": 0,
                    "data": {
                        "item": {
                            "modules": {},
                        }
                    },
                }
            raise AssertionError(url)

    item = {
        "dynamic_id": DID,
        "lottery_type": "转发抽奖",
        "lottery_time": 9_999_999_999,
    }
    preflight._sync_live_fields(
        flow.ParticipationReadClient(NormalClient(code=0, message="0")),
        item,
        lottery_type="转发抽奖",
    )
    assert item["draw_status"] == "active"
