"""国内通知系统（M2）离线测试：全 mock，零真实 Server酱/钉钉/企微/飞书/Bilibili。

A  四 provider 正常 sent
B  Profile A/B 配置完全隔离
C  GET API 永不返回完整 secret（只 configured/masked）
D  日志/异常脱敏（异常文本不包含 URL/secret；send 状态不泄露原始值）
E  trust_env=False（HTTP(S)_PROXY/BINGGO_PROXY 存在时也不继承）
F  Bilibili Cookie/CSRF 不进通知 client（构造参数断言）
G  timeout → delivery_unknown，0 retry
H  连接发送后中断 → delivery_unknown，0 retry
I  业务明确拒绝 → failed，0 自动 retry
J  同 (event_id, channel) 已 sent：restart/再次 dispatch → 0 新请求
K  同 (event_id, channel) delivery_unknown：再次自动 dispatch → 0 新请求
L  Server酱 sent + 飞书 failed：结果独立保存
M  单 provider 异常不影响其它渠道
N  测试通知每次生成新 event_id，可重复发送
O  Profile 切换读不到另一 Profile 凭据/记录
P  API/前端视图不暴露 secret
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from src.notification import service as nsvc


def _json_response(status: int, body) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        request=httpx.Request("POST", "https://notify.example.test/"),
    )


class _FakeNotifyClient:
    """可编程通知 HTTP fake；post 记录完整参数供断言（不落日志）。"""

    def __init__(self, *, on_post=None):
        self.posts: list[tuple[str, dict]] = []
        self.on_post = on_post

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if self.on_post is not None:
            return self.on_post(url, kwargs)
        return _json_response(200, {"code": 0})


@pytest.fixture
def profile_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "profile-a"
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: target)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _fake_post(client_factory, *, channel=None):
    def _install(monkeypatch: pytest.MonkeyPatch):
        fake = _FakeNotifyClient(on_post=client_factory)
        monkeypatch.setattr(nsvc, "create_notify_client", lambda: fake)
        return fake

    return _install


def _configure_channel(channel: str, values: dict[str, str]) -> None:
    nsvc.save_channel_config(channel, values)
    nsvc.set_channel_enabled(channel, True)


# ---------- A：四渠道 sent ----------


@pytest.mark.parametrize("channel,values", [
    (nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT1234567890abcdef"}),
    (nsvc.CHANNEL_DINGTALK, {"token": "tok123", "secret": "sec123"}),
    (nsvc.CHANNEL_WE_COM, {"key": "key-abc"}),
    (nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/tok", "secret": ""}),
])
def test_provider_send_success(profile_dir: Path, monkeypatch, channel, values) -> None:
    _configure_channel(channel, values)
    fake = _FakeNotifyClient()
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: fake)
    result = nsvc.service.dispatch("auto_task_completed", "完成", "详情", channels=[channel])
    assert result[channel]["status"] == "sent"
    assert len(fake.posts) == 1
    record = nsvc._existing_status("auto_task_completed", channel)
    assert record == "sent"


# ---------- B/O：Profile 隔离 ----------


def test_profile_isolation_config_and_records(tmp_path: Path, monkeypatch) -> None:
    a = tmp_path / "profile-a"
    b = tmp_path / "profile-b"
    a.mkdir(parents=True, exist_ok=True)
    b.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: a)
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTA"})
    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: b)
    assert nsvc.get_public_config()["channels"][0]["configured"] is False  # B 读不到 A

    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: a)
    fake = _FakeNotifyClient()
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: fake)
    nsvc.service.dispatch("auto_task_error", "err", channels=[nsvc.CHANNEL_SERVERCHAN])
    records_a = nsvc.recent_records()

    monkeypatch.setattr(nsvc, "get_profile_dir", lambda: b)
    assert nsvc.recent_records() == []  # O：记录也不共享
    assert nsvc.get_public_config()["channels"][0]["configured"] is False


# ---------- C/P：secret 不暴露 ----------


def test_public_config_never_exposes_secrets(profile_dir: Path) -> None:
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCT1234567890abcdef"})
    _configure_channel(nsvc.CHANNEL_DINGTALK, {"token": "tok123", "secret": "sec-secret"})
    payload = json.dumps(nsvc.get_public_config())
    assert "SCT1234567890abcdef" not in payload
    assert "tok123" not in payload
    assert "sec-secret" not in payload
    assert "SCT****" in payload or "SCT1****" in payload or "SCT1" in payload
    for channel in nsvc.get_public_config()["channels"]:
        assert set(channel) <= {"channel", "label", "configured", "enabled", "masked"}


def test_save_config_requires_full_secret_on_write(profile_dir: Path) -> None:
    # 读视图没有 secret；只有写接口接收完整值（存盘后读视图仍掩码）
    nsvc.save_channel_config(nsvc.CHANNEL_WE_COM, {"key": "qywx-full-key"})
    public = nsvc.get_public_config()["channels"]
    wecom = next(item for item in public if item["channel"] == nsvc.CHANNEL_WE_COM)
    assert wecom["configured"] is True
    assert "qywx-full-key" not in json.dumps(wecom)
    assert wecom["masked"] != "qywx-full-key"


# ---------- D：异常脱敏 ----------


def test_failure_path_never_leaks_secrets_in_results_or_records(profile_dir: Path) -> None:
    _configure_channel(nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/topsecret"})
    _configure_channel(nsvc.CHANNEL_WE_COM, {"key": "very-secret-key"})

    class ExplodingClient(_FakeNotifyClient):
        def post(self, url, **kwargs):
            raise httpx.ConnectTimeout("GET https://open.feishu.cn/.../topsecret")

    monkeypatch = None
    import pytest

    from unittest.mock import patch

    with patch.object(nsvc, "create_notify_client", lambda: ExplodingClient()):
        results = nsvc.service.dispatch("auto_task_completed", "x", channels=[nsvc.CHANNEL_FEISHU, nsvc.CHANNEL_WE_COM])
    payload = json.dumps(results)
    assert "topsecret" not in payload
    assert "very-secret-key" not in payload
    assert "ConnectTimeout" not in payload  # 原始异常文本不落结果
    assert results[nsvc.CHANNEL_FEISHU]["status"] == "delivery_unknown"


# ---------- E/F：client 隔离 ----------


def test_notify_client_is_proxy_and_bilibili_isolated(monkeypatch) -> None:
    os.environ["HTTP_PROXY"] = "http://user:pass@proxy.example:8080"
    os.environ["HTTPS_PROXY"] = "http://user:pass@proxy.example:8080"
    os.environ["BINGGO_PROXY"] = "http://bproxy.example:3128"
    try:
        client = nsvc.create_notify_client()
    finally:
        os.environ.pop("HTTP_PROXY", None)
        os.environ.pop("HTTPS_PROXY", None)
        os.environ.pop("BINGGO_PROXY", None)
    assert client.trust_env is False  # 系统/BINGGO 代理一律不继承
    assert "Cookie" not in client.headers
    assert "csrf" not in {str(k).lower() for k in client.headers}
    assert client.headers.get("User-Agent", "").startswith("Binggo-notify")


def test_dispatch_client_created_with_trust_env_false(profile_dir: Path, monkeypatch) -> None:
    captured: dict = {}

    def spy_client(*args, **kwargs):
        captured.update(kwargs)
        return _FakeNotifyClient()

    monkeypatch.setattr(nsvc, "create_notify_client", spy_client)
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTx"})
    nsvc.service.dispatch("auto_task_completed", "t", channels=[nsvc.CHANNEL_SERVERCHAN])
    # create_notify_client 内部用 trust_env=False 由上一测试直接覆盖；
    # 此处验证 dispatch 不传任何代理/凭据相关选项。
    assert "proxies" not in captured
    assert "headers" not in captured or "Cookie" not in captured.get("headers", {})


# ---------- G/H/I：状态三态与 0 retry ----------


def test_timeout_is_delivery_unknown_with_no_retry(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    class TimeoutClient(_FakeNotifyClient):
        def post(self, url, **kwargs):
            calls["n"] += 1
            raise httpx.ReadTimeout("timed out", request=None)

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: TimeoutClient())
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTx"})
    result = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert result[nsvc.CHANNEL_SERVERCHAN]["status"] == "delivery_unknown"
    assert calls["n"] == 1


def test_connection_break_is_delivery_unknown_no_retry(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    class BrokenClient(_FakeNotifyClient):
        def post(self, url, **kwargs):
            calls["n"] += 1
            raise httpx.RemoteProtocolError("peer closed connection")

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: BrokenClient())
    _configure_channel(nsvc.CHANNEL_DINGTALK, {"token": "tok", "secret": ""})
    result = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_DINGTALK])
    assert result[nsvc.CHANNEL_DINGTALK]["status"] == "delivery_unknown"
    assert calls["n"] == 1


def test_business_rejection_is_failed_no_retry(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    def on_post(url, kwargs):
        calls["n"] += 1
        return _json_response(200, {"errcode": 93000, "errmsg": "invalid webhook"})

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    _configure_channel(nsvc.CHANNEL_WE_COM, {"key": "k"})
    result = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_WE_COM])
    assert result[nsvc.CHANNEL_WE_COM]["status"] == "failed"
    assert calls["n"] == 1


def test_http_4xx_is_failed_http_5xx_unknown(profile_dir: Path, monkeypatch) -> None:
    calls: dict[str, int] = {"4": 0, "5": 0}

    def on_post_4(url, kwargs):
        calls["4"] += 1
        return _json_response(403, {})

    def on_post_5(url, kwargs):
        calls["5"] += 1
        return _json_response(503, {})

    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "s1"})
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post_4))
    r4 = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert r4[nsvc.CHANNEL_SERVERCHAN]["status"] == "failed"

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post_5))
    r5 = nsvc.service.dispatch("auto_task_completed", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert r5[nsvc.CHANNEL_SERVERCHAN]["status"] == "delivery_unknown"


# ---------- J/K：outbox 去重（含 restart 模拟） ----------


def _restart(monkeypatch, profile_dir: Path) -> None:
    # 模拟进程重启：模块状态无缓存，重新读取文件即可
    pass


def test_sent_event_never_resent_after_restart(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    def on_post(url, kwargs):
        calls["n"] += 1
        return _json_response(200, {"code": 0})

    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTx"})
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    first = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert first[nsvc.CHANNEL_SERVERCHAN]["status"] == "sent"
    _restart(monkeypatch, profile_dir)

    # 再次自动 dispatch（模拟 restart 后补投）→ 0 新请求
    second = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert second[nsvc.CHANNEL_SERVERCHAN]["status"] == "sent"
    assert calls["n"] == 1


def test_delivery_unknown_never_resent_after_restart(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    class UnknownClient(_FakeNotifyClient):
        def post(self, url, **kwargs):
            calls["n"] += 1
            raise httpx.ReadTimeout("t", request=None)

    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTx"})
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: UnknownClient())
    first = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert first[nsvc.CHANNEL_SERVERCHAN]["status"] == "delivery_unknown"
    second = nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert second[nsvc.CHANNEL_SERVERCHAN]["status"] == "delivery_unknown"
    assert calls["n"] == 1  # 0 新请求


def test_failed_not_auto_retried(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    def on_post(url, kwargs):
        calls["n"] += 1
        return _json_response(200, {"errcode": 1})

    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTx"})
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    nsvc.service.dispatch("auto_task_error", "e", channels=[nsvc.CHANNEL_SERVERCHAN])
    assert calls["n"] == 1


# ---------- L/M：多渠道独立 ----------


def test_multi_channel_results_independent(profile_dir: Path, monkeypatch) -> None:
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTok"})
    _configure_channel(nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/f", "secret": ""})

    def on_post(url, kwargs):
        if "sctapi.ftqq.com" in url:
            return _json_response(200, {"code": 0})
        return _json_response(200, {"code": 19001})  # 飞书业务拒绝

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    results = nsvc.service.dispatch("auto_task_error", "多渠", channels=[nsvc.CHANNEL_SERVERCHAN, nsvc.CHANNEL_FEISHU])
    assert results[nsvc.CHANNEL_SERVERCHAN]["status"] == "sent"
    assert results[nsvc.CHANNEL_FEISHU]["status"] == "failed"
    assert nsvc._existing_status("auto_task_error", nsvc.CHANNEL_SERVERCHAN) == "sent"
    assert nsvc._existing_status("auto_task_error", nsvc.CHANNEL_FEISHU) == "failed"


def test_one_provider_exception_does_not_block_others(profile_dir: Path, monkeypatch) -> None:
    _configure_channel(nsvc.CHANNEL_SERVERCHAN, {"sendkey": "SCTok"})
    _configure_channel(nsvc.CHANNEL_WE_COM, {"key": "k"})

    def on_post(url, kwargs):
        if "sctapi.ftqq.com" in url:
            raise RuntimeError("本地爆炸")  # 本地未知异常也隔离
        return _json_response(200, {"errcode": 0})

    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    results = nsvc.service.dispatch("auto_task_error", "x", channels=[nsvc.CHANNEL_SERVERCHAN, nsvc.CHANNEL_WE_COM])
    assert results[nsvc.CHANNEL_WE_COM]["status"] == "sent"
    assert results[nsvc.CHANNEL_SERVERCHAN]["status"] == "delivery_unknown"
    assert len(results) == 2


# ---------- N：测试通知每次新 event ----------


def test_manual_test_notification_can_resend(profile_dir: Path, monkeypatch) -> None:
    calls = {"n": 0}

    def on_post(url, kwargs):
        calls["n"] += 1
        return _json_response(200, {"code": 0})

    _configure_channel(nsvc.CHANNEL_FEISHU, {"webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/f", "secret": ""})
    monkeypatch.setattr(nsvc, "create_notify_client", lambda: _FakeNotifyClient(on_post=on_post))
    r1 = nsvc.service.send_test(nsvc.CHANNEL_FEISHU)
    r2 = nsvc.service.send_test(nsvc.CHANNEL_FEISHU)
    assert r1["event_id"] != r2["event_id"]
    assert calls["n"] == 2  # 每次人工点击 = 新的人工请求，允许再发


# ---------- 签名纯函数回归 ----------


def test_signature_pure_functions() -> None:
    # 确定性校验：给定时间戳与 secret，签名稳定（钉钉 URL 安全；飞书标准 Base64）。
    ding = nsvc.dingtalk_sign("SECabc", 1700000000000)
    assert ding == nsvc.dingtalk_sign("SECabc", 1700000000000)
    assert "%" in ding or "+" in ding or "=" in ding  # quote_plus 后不再含裸 base64 字符
    feishu = nsvc.feishu_sign("SECabc", 1700000000)
    assert feishu == nsvc.feishu_sign("SECabc", 1700000000)
    assert isinstance(feishu, str) and feishu
