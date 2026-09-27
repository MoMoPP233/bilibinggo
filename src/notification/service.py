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
from typing import Any, Callable

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


class NotificationLocalError(RuntimeError):
    """本地投递记录（outbox claim / terminal）无法持久化的硬失败。

    语义：候选已可靠落库但通知侧 durable state 未建立/未完成——
    不允许发送 HTTP，或已发出的渠道结果无法安全落盘；Source checkpoint 不得推进。
    """


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


def _mutate_outbox(mutator: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]) -> None:
    """outbox read-modify-write：整段处于 per-path 共享锁内（与原子写同一把锁）。"""
    with get_atomic_path_lock(_outbox_path()):
        records = _load_outbox()
        records = mutator(records)
        _save_outbox(records)


def _claim_records(
    event_id: str, channels: list[str], *, event_type: str | None = None
) -> None:
    """HTTP 之前为“本次全部待发渠道”做一次原子 batch claim。

    所有渠道的 dispatching 记录在同一个 per-path 共享锁内一次性写入并只落盘一次：
    - 成功 → 全部 channel 都有 durable dispatching；
    - 失败 → 抛 NotificationLocalError 且一个 dispatching 都不落盘（0 HTTP）。
    绝不存在“Server酱 有、飞书 无”的半批 claim。
    """

    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [
            r
            for r in records
            if not (r.get("event_id") == event_id and r.get("channel") in channels)
        ]
        now = int(time.time())
        for channel in channels:
            record: dict[str, Any] = {
                "event_id": event_id,
                "channel": channel,
                "status": "dispatching",
                "summary": "",
                "sent_at": now,
            }
            if event_type is not None:
                record["event_type"] = event_type
            kept.append(record)
        return kept

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001 - 本地 IO 失败归为硬失败
        raise NotificationLocalError("无法建立通知投递记录（batch claim 写入失败）") from exc


def _resolve_stale_claim(event_id: str, channel: str) -> None:
    """把遗留 dispatching claim 持久化为 delivery_unknown（fail-closed，绝不自动重发）。"""

    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        changed = False
        for record in records:
            if record.get("event_id") == event_id and record.get("channel") == channel:
                if record.get("status") == "dispatching":
                    record["status"] = "delivery_unknown"
                    record["summary"] = "结果未确认（进程中断），不再自动重试"
                    changed = True
        if changed:
            return records
        record: dict[str, Any] = {
            "event_id": event_id,
            "channel": channel,
            "status": "delivery_unknown",
            "summary": "结果未确认（进程中断），不再自动重试",
            "sent_at": int(time.time()),
        }
        records.append(record)
        return records

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001
        raise NotificationLocalError("无法落盘 delivery_unknown（进程中断记录）") from exc


def _finalize_record(event_id: str, channel: str, status: str, summary: str) -> None:
    """HTTP 结果落盘（sent/failed/delivery_unknown）；失败时保留 dispatching claim。"""

    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for record in records:
            if record.get("event_id") == event_id and record.get("channel") == channel:
                record["status"] = status
                record["summary"] = summary
                record["sent_at"] = int(time.time())
                return records
        records.append(
            {
                "event_id": event_id,
                "channel": channel,
                "status": status,
                "summary": summary,
                "sent_at": int(time.time()),
            }
        )
        return records

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001 - 结果无法持久化：claim 保持 dispatching
        raise NotificationLocalError("无法落盘通知投递结果") from exc


def _append_record(
    event_id: str, channel: str, status: str, summary: str, *, event_type: str | None = None
) -> None:
    # 兼容旧路径：直接写终态记录（无 claim 语义的调用方保留）。
    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        records = [
            r for r in records if not (r.get("event_id") == event_id and r.get("channel") == channel)
        ]
        record: dict[str, Any] = {
            "event_id": event_id,
            "channel": channel,
            "status": status,
            "summary": summary,
            "sent_at": int(time.time()),
        }
        if event_type is not None:
            record["event_type"] = event_type
        records.append(record)
        return records

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001
        raise NotificationLocalError("无法写入通知投递记录") from exc


def recent_records(limit: int = 30) -> list[dict[str, Any]]:
    records = [record for record in _load_outbox() if not record.get("deleted")]
    return records[-limit:][::-1]


def delete_record(event_id: str, channel: str) -> None:
    """Hide one recent-record row while preserving outbox deduplication state."""
    target_event = str(event_id or "").strip()
    target_channel = str(channel or "").strip()
    if not target_event or target_channel not in ALL_CHANNELS:
        raise ValueError("通知记录标识无效")
    matched = False

    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal matched
        # 正常 outbox 对 (event_id, channel) 唯一；从后往前只标记一条，
        # 即使旧数据存在重复，也不会扩大删除范围。
        for record in reversed(records):
            if (
                not record.get("deleted")
                and record.get("event_id") == target_event
                and record.get("channel") == target_channel
            ):
                record["deleted"] = True
                record["deleted_at"] = int(time.time())
                matched = True
                break
        return records

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001 - local durable failure
        raise NotificationLocalError("无法删除通知记录") from exc
    if not matched:
        raise KeyError((target_event, target_channel))


def recover_stale_dispatches_on_startup() -> int:
    """进程启动时把上一进程崩溃遗留的 dispatching 安全转为 delivery_unknown。

    前提：本进程刚启动、尚无任何在途 dispatch，outbox 中所有 dispatching 必然来自
    已退出/崩溃的旧进程 → 0 HTTP、0 resend。返回被转换的记录条数。
    """
    if not any(record.get("status") == "dispatching" for record in _load_outbox()):
        return 0
    converted = 0

    def mutate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        nonlocal converted
        for record in records:
            if record.get("status") == "dispatching":
                record["status"] = "delivery_unknown"
                record["summary"] = "结果未确认（进程中断），不再自动重试"
                converted += 1
        return records

    try:
        _mutate_outbox(mutate)
    except Exception as exc:  # noqa: BLE001
        raise NotificationLocalError("无法落盘启动期 delivery_unknown 收敛") from exc
    return converted


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
        event_type: str | None = None,
    ) -> dict[str, dict[str, str]]:
        """event_id 是本次投递实例的唯一稳定标识（同一消息重复扫描得到相同 event_id）。

        event_type 用于事件开关查询与注册校验：不传时退化为 event_id 本身
        （M2 既有调用语义不变）；传入时 event_id 允许是实例级标识
        （如 winning_candidate:mention:123），保证不同消息不同 event_id。
        """
        event_type = event_type or event_id
        if event_type not in EVENT_IDS and not force:
            raise NotificationConfigError(f"未知通知事件：{event_type}")
        config = _load_config()
        if not config["events"].get(event_type, True):
            return {}
        results: dict[str, dict[str, str]] = {}
        targets = list(channels) if channels else [
            channel for channel in ALL_CHANNELS if config["channels"][channel]["enabled"]
        ]
        summary = body.strip()[:80] if body else title.strip()[:80]

        # 阶段 0：梳理每个渠道的既有状态；终端态直接跳过，遗留 dispatching
        # （进程在 HTTP 前后死亡留下的 claim）fail-closed 转 delivery_unknown。
        terminal: dict[str, str] = {}
        needs_claim: list[str] = []
        for channel in targets:
            entry = config["channels"][channel]
            if not entry.get("enabled"):
                results[channel] = {"status": "skipped", "summary": "渠道未启用"}
                continue
            existing = _existing_status(event_id, channel)
            if existing in ("sent", "delivery_unknown", "failed"):
                terminal[channel] = existing
                results[channel] = {"status": existing, "summary": "已存在投递记录，不重复发送"}
                continue
            if existing == "dispatching":
                # 进程曾在发送前/中死亡：结果不可知 → delivery_unknown（fail-closed），0 自动重发。
                _resolve_stale_claim(event_id, channel)
                results[channel] = {
                    "status": "delivery_unknown",
                    "summary": "结果未确认（进程中断），不再自动重试",
                }
                terminal[channel] = "delivery_unknown"
                continue
            needs_claim.append(channel)

        # 阶段 1：第一个 HTTP 之前，为全部待发渠道做单次原子 batch claim。
        # 要么所有渠道都有 durable dispatching，要么一个都没有（失败抛硬错误，0 HTTP）。
        if needs_claim:
            _claim_records(event_id, needs_claim, event_type=event_type)

        # 阶段 2：逐渠道发送并把结果落盘（每渠道一次；结果独立）。
        for channel in needs_claim:
            entry = config["channels"][channel]
            status = _provider_send(channel, entry, title, body)
            _finalize_record(event_id, channel, status, summary)
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
