"""Binggo 国内通知系统（M2）：Server酱 / 钉钉机器人 / 企业微信机器人 / 飞书机器人。

设计约束：
- 通知 HTTP 与 BilibiliClient 完全隔离：独立 httpx.Client，trust_env=False
  （不继承系统/BINGGO 代理），无 Cookie/CSRF/Bilibili headers。
- 配置与投递记录 per-Profile（本地 JSON，atomic 写，V10 同一 per-path 锁）。
- 凭据为 secret：读接口只返回 configured/masked；日志与异常一律脱敏。
- 状态三态：sent / failed / delivery_unknown；delivery_unknown 永不自动重试。
- (event_id, channel) 唯一约束：sent / delivery_unknown / failed 再次自动 dispatch
  均 0 请求；人工“测试通知”每次生成新 event_id 允许重复发送。
- 一事件多渠道结果相互独立；单渠道异常不影响其它渠道；每渠道每事件只尝试一次。
- 只做通用事件模型（含预留 winning_candidate/winning_confirmed 等枚举），
  M2 不生产 winning_* 事件；测试通知是唯一内置 sender。

四渠道请求格式与签名纯函数轻改自参考项目（bil-1-main notify.py）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import tempfile
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.data_paths import get_atomic_path_lock, get_profile_dir

TIMEOUT_SECONDS = 10.0
SEND_RESULTS = ("sent", "failed", "delivery_unknown")

CHANNEL_SERVERCHAN = "serverchan"
CHANNEL_DINGTALK = "dingtalk"
CHANNEL_WE_COM = "wecom"
CHANNEL_FEISHU = "feishu"
ALL_CHANNELS = (CHANNEL_SERVERCHAN, CHANNEL_DINGTALK, CHANNEL_WE_COM, CHANNEL_FEISHU)
CHANNEL_LABELS = {
    CHANNEL_SERVERCHAN: "Server酱",
    CHANNEL_DINGTALK: "钉钉机器人",
    CHANNEL_WE_COM: "企业微信机器人",
    CHANNEL_FEISHU: "飞书机器人",
}

# 通用事件模型；winning_* 在 M2 只做预留枚举（M3 才接入生产逻辑）。
EVENT_IDS = (
    "winning_candidate",
    "winning_confirmed",
    "platform_risk",
    "auto_task_error",
    "auto_task_completed",
    "test",
)
DEFAULT_EVENT_ENABLED = True

_CONFIG_FILE = "notify_config.json"
_OUTBOX_FILE = "notify_outbox.json"


class NotificationConfigError(ValueError):
    """渠道配置缺失/非法。"""


@dataclass(frozen=True)
class ChannelPublic:
    channel: str
    configured: bool
    enabled: bool
    masked: str


# ---------------------------------------------------------------------------
# 纯函数（源自 bil-1-main，仅保留四个渠道所需；签名与 payload 格式保持一致）
# ---------------------------------------------------------------------------


def dingtalk_sign(secret: str, timestamp_ms: int) -> str:
    string_to_sign = f"{timestamp_ms}\n{secret}"
    return urllib.parse.quote_plus(
        base64.b64encode(
            hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).digest()
        )
    )


def feishu_sign(secret: str, timestamp: int) -> str:
    # 官方签名：key=f"{timestamp}\n{secret}"，HMAC-SHA256(空消息) 后 Base64。
    string_to_sign = f"{timestamp}\n{secret}"
    return base64.b64encode(
        hmac.new(string_to_sign.encode(), digestmod=hashlib.sha256).digest()
    ).decode()


def serverchan_request(sendkey: str, title: str, body: str) -> tuple[str, dict]:
    return (
        f"https://sctapi.ftqq.com/{sendkey}.send",
        {"params": {"title": title, "desp": body}, "json": None},
    )


def dingtalk_request(token: str, secret: str, title: str, body: str) -> tuple[str, dict]:
    params: dict[str, Any] = {"access_token": token}
    if secret:
        timestamp = str(round(time.time() * 1000))
        params["timestamp"] = timestamp
        params["sign"] = dingtalk_sign(secret, int(timestamp))
    return (
        "https://oapi.dingtalk.com/robot/send",
        {"params": params, "json": {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}},
    )


def wecom_request(key: str, title: str, body: str) -> tuple[str, dict]:
    return (
        f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={key}",
        {"params": None, "json": {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}},
    )


def feishu_request(webhook: str, secret: str, title: str, body: str) -> tuple[str, dict]:
    payload: dict[str, Any] = {"msg_type": "text", "content": {"text": f"{title}\n{body}"}}
    if secret:
        timestamp = int(time.time())
        payload["timestamp"] = timestamp
        payload["sign"] = feishu_sign(secret, timestamp)
    return webhook, {"params": None, "json": payload}


# ---------------------------------------------------------------------------
# Profile 级配置与投递记录
# ---------------------------------------------------------------------------


def _config_path() -> Path:
    return get_profile_dir() / _CONFIG_FILE


def _outbox_path() -> Path:
    return get_profile_dir() / _OUTBOX_FILE


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    with get_atomic_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _default_config() -> dict[str, Any]:
    events = {event: DEFAULT_EVENT_ENABLED for event in EVENT_IDS}
    channels = {
        channel: {"enabled": False, **{field: "" for field in _channel_fields(channel)}}
        for channel in ALL_CHANNELS
    }
    return {"version": 1, "channels": channels, "events": events}


def _channel_fields(channel: str) -> tuple[str, ...]:
    if channel == CHANNEL_SERVERCHAN:
        return ("sendkey",)
    if channel == CHANNEL_DINGTALK:
        return ("token", "secret")
    if channel == CHANNEL_WE_COM:
        return ("key",)
    if channel == CHANNEL_FEISHU:
        return ("webhook", "secret")
    raise NotificationConfigError(f"未知渠道：{channel}")


def _load_config() -> dict[str, Any]:
    stored = _read_json(_config_path())
    merged = _default_config()
    channels = stored.get("channels")
    if isinstance(channels, dict):
        for channel in ALL_CHANNELS:
            entry = channels.get(channel)
            if not isinstance(entry, dict):
                continue
            target = merged["channels"][channel]
            target["enabled"] = bool(entry.get("enabled", False))
            for field in _channel_fields(channel):
                value = entry.get(field)
                if isinstance(value, str) and value:
                    target[field] = value
    events = stored.get("events")
    if isinstance(events, dict):
        for event in EVENT_IDS:
            if event in events:
                merged["events"][event] = bool(events[event])
    return merged


def _save_config(config: dict[str, Any]) -> None:
    _write_json(_config_path(), config)


# ---------------------------------------------------------------------------
# secret 脱敏
# ---------------------------------------------------------------------------


def _mask(value: str, *, tail: int = 4) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) <= tail + 3:
        return "*" * len(text)
    head = text[:3]
    return f"{head}****{text[-tail:]}"


def _masked_credential(channel: str, entry: dict[str, Any]) -> str:
    if channel == CHANNEL_SERVERCHAN:
        return _mask(entry.get("sendkey") or "")
    if channel == CHANNEL_DINGTALK:
        value = entry.get("token") or ""
        return _mask(value) if value else ""
    if channel == CHANNEL_WE_COM:
        return _mask(entry.get("key") or "")
    if channel == CHANNEL_FEISHU:
        webhook = str(entry.get("webhook") or "")
        if not webhook:
            return ""
        return _mask(webhook)
    return ""


def _configured(channel: str, entry: dict[str, Any]) -> bool:
    return any(bool(str(entry.get(field) or "").strip()) for field in _channel_fields(channel))


def public_channel_view(channel: str, entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "channel": channel,
        "label": CHANNEL_LABELS[channel],
        "configured": _configured(channel, entry),
        "enabled": bool(entry.get("enabled")),
        "masked": _masked_credential(channel, entry),
    }


def get_public_config() -> dict[str, Any]:
    """GET API 视图：只返回 configured/enabled/masked，绝不返回原始 secret。"""
    config = _load_config()
    channels = [
        public_channel_view(channel, config["channels"][channel]) for channel in ALL_CHANNELS
    ]
    return {
        "channels": channels,
        "events": {event: bool(config["events"][event]) for event in EVENT_IDS},
    }


def save_channel_config(channel: str, values: dict[str, Any]) -> dict[str, Any]:
    if channel not in ALL_CHANNELS:
        raise NotificationConfigError(f"未知渠道：{channel}")
    config = _load_config()
    entry = config["channels"][channel]
    accepted = False
    for field in _channel_fields(channel):
        if field in values:
            entry[field] = str(values[field] or "").strip()
            accepted = True
    if not accepted:
        raise NotificationConfigError("没有可保存的凭据字段")
    _save_config(config)
    return public_channel_view(channel, entry)


def set_channel_enabled(channel: str, enabled: bool) -> dict[str, Any]:
    if channel not in ALL_CHANNELS:
        raise NotificationConfigError(f"未知渠道：{channel}")
    config = _load_config()
    config["channels"][channel]["enabled"] = bool(enabled)
    _save_config(config)
    return public_channel_view(channel, config["channels"][channel])


def set_event_enabled(event: str, enabled: bool) -> None:
    if event not in EVENT_IDS:
        raise NotificationConfigError(f"未知通知事件：{event}")
    config = _load_config()
    config["events"][event] = bool(enabled)
    _save_config(config)


# ---------------------------------------------------------------------------
# 投递记录（outbox）与去重
# ---------------------------------------------------------------------------


def _load_outbox() -> list[dict[str, Any]]:
    stored = _read_json(_outbox_path())
    records = stored.get("records")
    return records if isinstance(records, list) else []


def _save_outbox(records: list[dict[str, Any]]) -> None:
    _write_json(_outbox_path(), {"version": 1, "records": records[-200:]})


def _existing_status(event_id: str, channel: str) -> str | None:
    for record in _load_outbox():
        if record.get("event_id") == event_id and record.get("channel") == channel:
            return str(record.get("status") or "")
    return None


def _append_record(event_id: str, channel: str, status: str, summary: str) -> None:
    records = _load_outbox()
    records = [r for r in records if not (r.get("event_id") == event_id and r.get("channel") == channel)]
    records.append(
        {
            "event_id": event_id,
            "channel": channel,
            "status": status,
            "summary": summary,
            "sent_at": int(time.time()),
        }
    )
    _save_outbox(records)


def recent_records(limit: int = 30) -> list[dict[str, Any]]:
    records = _load_outbox()
    return records[-limit:][::-1]


# ---------------------------------------------------------------------------
# 通知 HTTP（与 Bilibili HTTP 完全隔离）
# ---------------------------------------------------------------------------


def create_notify_client() -> httpx.Client:
    # trust_env=False：绝不继承系统代理 / BINGGO_PROXY；无 Cookie 与 B 站头。
    return httpx.Client(
        trust_env=False,
        timeout=TIMEOUT_SECONDS,
        follow_redirects=False,
        headers={"Content-Type": "application/json", "User-Agent": "Binggo-notify/1.0"},
    )


def _classify_response(channel: str, status_code: int, body: Any) -> str:
    """把渠道响应归类为 sent / failed / delivery_unknown。"""
    if status_code >= 500:
        return "delivery_unknown"
    if status_code >= 400:
        return "failed"
    if channel == CHANNEL_FEISHU:
        try:
            if isinstance(body, dict):
                code = body.get("code")
                if code is not None:
                    return "sent" if code == 0 else "failed"
        except (TypeError, ValueError):
            return "delivery_unknown"
        return "sent"
    try:
        if isinstance(body, dict):
            if "errcode" in body and body.get("errcode") != 0:
                return "failed"
            if "code" in body and body.get("code") != 0:
                return "failed"
    except (TypeError, ValueError):
        return "delivery_unknown"
    return "sent"


def _sanitize_exception(exc: BaseException) -> str:
    # 异常可能内嵌完整 URL/query（含 key/token）：一律不落原始文本。
    return f"{type(exc).__name__}"


def _provider_send(channel: str, entry: dict[str, Any], title: str, body: str) -> str:
    """单渠道单次尝试；返回 sent/failed/delivery_unknown（绝不自动 retry）。"""
    try:
        if channel == CHANNEL_SERVERCHAN:
            sendkey = str(entry.get("sendkey") or "").strip()
            if not sendkey:
                raise NotificationConfigError("Server酱 SendKey 未配置")
            url, options = serverchan_request(sendkey, title, body)
        elif channel == CHANNEL_DINGTALK:
            token = str(entry.get("token") or "").strip()
            if not token:
                raise NotificationConfigError("钉钉 Token 未配置")
            url, options = dingtalk_request(token, str(entry.get("secret") or ""), title, body)
        elif channel == CHANNEL_WE_COM:
            key = str(entry.get("key") or "").strip()
            if not key:
                raise NotificationConfigError("企业微信机器人 Key 未配置")
            url, options = wecom_request(key, title, body)
        elif channel == CHANNEL_FEISHU:
            webhook = str(entry.get("webhook") or "").strip()
            if not webhook:
                raise NotificationConfigError("飞书 Webhook 未配置")
            url, options = feishu_request(webhook, str(entry.get("secret") or ""), title, body)
        else:  # pragma: no cover
            raise NotificationConfigError(f"未知渠道：{channel}")
    except NotificationConfigError:
        # 配置缺失属于可明确判定失败（请求未发出）。
        return "failed"

    try:
        with create_notify_client() as client:
            if options.get("params") is not None:
                response = client.post(url, params=options["params"], json=options["json"])
            else:
                response = client.post(url, json=options["json"])
        try:
            body_payload = response.json()
        except (ValueError, AttributeError):
            body_payload = None
        return _classify_response(channel, int(response.status_code), body_payload)
    except httpx.TimeoutException:
        return "delivery_unknown"  # 可能已送达：永不自动重试
    except httpx.HTTPError:
        return "delivery_unknown"  # 连接中断：无法确认是否送达
    except Exception:  # noqa: BLE001 - 本地异常也不得外泄原始信息
        return "delivery_unknown"


# ---------------------------------------------------------------------------
# 分发入口（service）
# ---------------------------------------------------------------------------


class NotificationService:
    """通用通知服务：事件 → 各启用渠道（每渠道一次，结果独立落 outbox）。"""

    def dispatch(
        self,
        event_id: str,
        title: str,
        body: str = "",
        *,
        channels: tuple[str, ...] | list[str] | None = None,
        force: bool = False,
    ) -> dict[str, dict[str, str]]:
        if event_id not in EVENT_IDS and not force:
            raise NotificationConfigError(f"未知通知事件：{event_id}")
        config = _load_config()
        if not config["events"].get(event_id, True):
            return {}
        results: dict[str, dict[str, str]] = {}
        targets = list(channels) if channels else [
            channel for channel in ALL_CHANNELS if config["channels"][channel]["enabled"]
        ]
        summary = body.strip()[:80] if body else title.strip()[:80]
        for channel in targets:
            entry = config["channels"][channel]
            if not entry.get("enabled"):
                results[channel] = {"status": "skipped", "summary": "渠道未启用"}
                continue
            existing = _existing_status(event_id, channel)
            if existing in ("sent", "delivery_unknown", "failed"):
                # sent/unknown 重启后绝不重发；failed 也不后台自动重试
                results[channel] = {"status": existing, "summary": "已存在投递记录，不重复发送"}
                continue
            status = _provider_send(channel, entry, title, body)
            _append_record(event_id, channel, status, summary)
            results[channel] = {"status": status, "summary": summary}
        return results

    def send_test(self, channel: str) -> dict[str, str]:
        """人工测试：每次都生成新的 test event_id，因此允许再次发送。"""
        if channel not in ALL_CHANNELS:
            raise NotificationConfigError(f"未知渠道：{channel}")
        config = _load_config()
        entry = config["channels"][channel]
        if not _configured(channel, entry):
            raise NotificationConfigError("渠道尚未配置，无法发送测试通知")
        event_id = f"test:{int(time.time() * 1000)}:{os.urandom(3).hex()}"
        # 测试只发指定渠道；不写业务 outbox（人工动作非业务事件）。
        status = _provider_send(channel, entry, "Binggo 测试通知", "如果你收到这条消息，说明通知渠道配置正常。")
        return {"event_id": event_id, "channel": channel, "status": status}


service = NotificationService()
