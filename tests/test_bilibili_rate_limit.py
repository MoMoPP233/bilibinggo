from __future__ import annotations

import threading
import time

import pytest

from src.bilibili_rate_limit import (
    DEFAULT_BILI_RPS,
    TokenBucketRateLimiter,
    configure_bilibili_rate_limit,
    get_bilibili_rate_limiter,
    reset_bilibili_rate_limit,
)


@pytest.fixture(autouse=True)
def _reset_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BILI_RPS", raising=False)
    reset_bilibili_rate_limit()
    yield
    reset_bilibili_rate_limit()


def test_token_bucket_disabled_when_rps_zero() -> None:
    limiter = TokenBucketRateLimiter(0)
    start = time.monotonic()
    for _ in range(20):
        limiter.acquire()
    assert time.monotonic() - start < 0.05


def test_token_bucket_limits_burst() -> None:
    limiter = TokenBucketRateLimiter(4.0, burst=1.0)
    start = time.monotonic()
    limiter.acquire()
    limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.2


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def test_effective_rps_and_source_follow_env_or_default(monkeypatch) -> None:
    monkeypatch.setenv("BILI_RPS", "0.5")
    reset_bilibili_rate_limit()
    limiter = get_bilibili_rate_limiter()
    assert limiter.rps == 0.5
    assert limiter.source == "env"

    monkeypatch.delenv("BILI_RPS", raising=False)
    reset_bilibili_rate_limit()
    limiter = get_bilibili_rate_limiter()
    assert limiter.rps == DEFAULT_BILI_RPS
    assert limiter.source == "default"


def test_token_wait_uses_fake_clock_and_accumulates(monkeypatch) -> None:
    import src.bilibili_rate_limit as rate_limit

    clock = _FakeClock()
    monkeypatch.setattr(rate_limit.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(rate_limit.time, "sleep", clock.sleep)
    limiter = TokenBucketRateLimiter(0.5, source="env")

    assert limiter.acquire() == (False, 0.0)
    waited, wait_ms = limiter.acquire()
    assert waited is True
    assert wait_ms == pytest.approx(2_000)
    waited, wait_ms = limiter.acquire()
    assert waited is True
    assert wait_ms == pytest.approx(2_000)


def test_global_limiter_shared_across_threads() -> None:
    configure_bilibili_rate_limit(rps=5.0)
    from src.bilibili_rate_limit import get_bilibili_rate_limiter

    limiter = get_bilibili_rate_limiter()
    assert limiter.rps == 5.0

    hits: list[float] = []
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire()
        with lock:
            hits.append(time.monotonic())

    start = time.monotonic()
    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert time.monotonic() - start >= 0.3
    assert len(hits) == 4


def test_bilibili_client_http_methods_acquire_slots(monkeypatch) -> None:
    from src.bilibili_client import BilibiliClient

    calls = {"n": 0}

    def fake_acquire() -> None:
        calls["n"] += 1

    monkeypatch.setattr("src.bilibili_client.acquire_bilibili_request_slot", fake_acquire)

    class _Resp:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 0, "data": {"wbi_img": {"img_url": "a.png", "sub_url": "b.png"}}}

        @property
        def text(self) -> str:
            return "ok"

    client = BilibiliClient(warmup=False)
    client._client.get = lambda *args, **kwargs: _Resp()  # type: ignore[method-assign]
    client._client.post = lambda *args, **kwargs: _Resp()  # type: ignore[method-assign]

    client.get_json("https://api.bilibili.com/x/web-interface/nav")
    client.get_text("https://www.bilibili.com/opus/1")
    client.post_form("https://api.bilibili.com/x/dynamic/feed/dyn/thumb", {"id": "1"})
    client.close()

    assert calls["n"] == 3


def test_bilibili_client_warmup_budget_exhaustion_is_handled(monkeypatch) -> None:
    from src.bilibili_client import BilibiliClient
    from src.bilibili_request_budget import RequestBudgetExhausted, RequestBudgetSnapshot

    snapshot = RequestBudgetSnapshot(
        window_started_at=1,
        window_expires_at=2,
        used_requests=120,
        budget_limit=120,
        window_seconds=21_600,
        observed_at=1,
    )
    monkeypatch.setattr(
        "src.bilibili_client.reserve_bilibili_http_attempt",
        lambda _url: (_ for _ in ()).throw(RequestBudgetExhausted(snapshot)),
    )

    client = BilibiliClient(warmup=True)
    client.close()


def test_post_form_forwards_query_params_without_changing_retry_defaults(monkeypatch) -> None:
    from src.bilibili_client import BilibiliClient

    captured: dict = {}

    class _Resp:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"code": 0}

    client = BilibiliClient(warmup=False)

    def fake_post(url, *, data=None, json=None, params=None, headers=None):
        captured.update(url=url, data=data, params=params, headers=headers)
        return _Resp()

    monkeypatch.setattr(client, "_http_post", fake_post)
    payload = client.post_form(
        "https://api.bilibili.com/x/dynamic/feed/operate/remove",
        {"dyn_id_str": "1234567890123456789"},
        params={"platform": "web", "csrf": "token"},
        retries=0,
        raise_on_code=False,
    )
    client.close()

    assert payload == {"code": 0}
    assert captured["data"] == {"dyn_id_str": "1234567890123456789"}
    assert captured["params"] == {"platform": "web", "csrf": "token"}


def test_default_bilibili_rps_is_three() -> None:
    from src.bilibili_rate_limit import DEFAULT_BILI_RPS, get_bilibili_rate_limiter

    assert DEFAULT_BILI_RPS == 3.0
    assert get_bilibili_rate_limiter().rps == 3.0
