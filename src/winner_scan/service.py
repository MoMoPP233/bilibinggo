"""中奖深检（M3）：@我的 / 回复我的 / 未读私信 → 疑似中奖线索。

只读扫描：绝不执行已读确认/回复/私信/领奖/打开链接类写操作。
三类来源独立 Source 状态（cursor/checkpoint、最近成功/错误、partial），全部 per-Profile。

- 关键词命中只产生 winning_candidate（疑似中奖线索），绝不自动 confirmed。
- 稳定去重键：
    mention → mention:<平台 item id>
    reply   → reply:<平台 reply id>
    dm      → dm:<talker_id>:<seqno>
  正文 hash 不作主键。
- 每条线索生成独立稳定 event_id：winning_candidate:<去重键>，
  再交给 M2 NotificationService（event_type=winning_candidate）。
  同一消息反复扫描 → 相同 event_id → M2 outbox 0 重发；
  不同中奖消息 → 不同 event_id → 各自通知。
- 顺序：GET → 完整解析 → 识别 → 去重 → 本地落线索 → 推进 Source checkpoint →
  （通知失败不影响 checkpoint/落库，投递状态归 M2 outbox）。
- winning_candidate 通知开关关闭：仍扫描/保存/推进，只是 0 webhook。
- 平台风控（-352/-509/HTTP429）：0 retry、立即停止所有后续 Bilibili 请求并上抛，
  进入既有 refresh risk 路径与 AutoScheduler 6h cooldown。普通网络失败只影响当前
  Source 的 checkpoint，其它 Source 可继续；失败绝不伪装成空结果。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from src.bilibili_auth import require_login
from src.data_paths import get_atomic_path_lock, get_profile_dir
from src.message_api import fetch_dm_messages, list_dm_sessions, list_feed
from src.message_types import MessageEntry, MessageType
from src.notification.service import NotificationLocalError, NotificationService
from src.platform_risk import risk_code_of

_STATE_FILE = "winner_scan_state.json"
_CANDIDATES_FILE = "winner_candidates.json"
MAX_REPLY_ENTRIES = 20
MAX_DM_SESSIONS = 20
MAX_DM_MESSAGES_PER_SESSION = 20
KEYWORD_LIMIT = 30

DEFAULT_KEYWORDS = [
    "~预约成功|预约主题|预约已成功",
    "中奖|恭喜|获得|幸运|抽中|奖品|填写地址|核对信息",
]


def judge_keywords(text: str, patterns: list[str] | None = None) -> bool:
    """关键词判定（复用 bil-1-main 语义）：`~` 开头为黑名单（命中置 False），
    其余为白名单（命中置 True）；后面的规则优先级更高。仅表示“疑似中奖”。"""
    text = str(text or "")
    if not text:
        return False
    result = False
    effective_patterns = DEFAULT_KEYWORDS if patterns is None else patterns
    for raw in effective_patterns:
        raw = str(raw).strip()
        if not raw:
            continue
        is_black = raw.startswith("~")
        pattern = raw[1:] if is_black else raw
        try:
            matched = re.search(pattern, text) is not None
        except re.error:
            matched = pattern in text
        if matched:
            result = not is_black
    return result


# ---------------------------------------------------------------------------
# per-Profile 状态存取
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return get_profile_dir() / _STATE_FILE


def _candidates_path() -> Path:
    return get_profile_dir() / _CANDIDATES_FILE


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


def _default_sources_state() -> dict[str, Any]:
    return {
        "mention": {"cursor_id": 0, "cursor_time": 0, "last_success_at": None, "last_error": "", "last_scan_at": None},
        "reply": {"cursor_id": 0, "cursor_time": 0, "last_success_at": None, "last_error": "", "last_scan_at": None},
        "dm": {"last_success_at": None, "last_error": "", "last_scan_at": None},
    }


def _load_state() -> dict[str, Any]:
    stored = _read_json(_state_path())
    state = _default_sources_state()
    sources = stored.get("sources")
    if isinstance(sources, dict):
        for key in state:
            entry = sources.get(key)
            if isinstance(entry, dict):
                state[key].update(
                    {
                        field: entry.get(field, state[key][field])
                        for field in state[key]
                    }
                )
    return state


def _save_state(state: dict[str, Any]) -> None:
    _write_json(_state_path(), {"version": 1, "sources": state})


def _load_candidates() -> dict[str, dict[str, Any]]:
    stored = _read_json(_candidates_path())
    records = stored.get("candidates")
    if not isinstance(records, list):
        return {}
    return {str(item.get("key") or ""): item for item in records if isinstance(item, dict)}


def _save_candidates(candidates: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(candidates.values(), key=lambda item: int(item.get("discovered_at") or 0), reverse=True)
    _write_json(_candidates_path(), {"version": 1, "candidates": ordered[-500:]})


def list_candidates(limit: int = 50) -> list[dict[str, Any]]:
    candidates = _load_candidates()
    ordered = sorted(candidates.values(), key=lambda item: int(item.get("discovered_at") or 0), reverse=True)
    return ordered[:limit]


def scan_status_view() -> dict[str, Any]:
    return {
        "sources": _load_state(),
        "candidates": list_candidates(30),
    }


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def _source_entry_key(source_type: str, entry: MessageEntry) -> str:
    if source_type == "dm":
        talker_id = int(entry.talker_id or 0)
        raw = entry.raw or {}
        seqno = str(raw.get("msg_seqno") or entry.id or "0")
        return f"dm:{talker_id}:{seqno}"
    return f"{source_type}:{entry.id}"


def _source_url(entry: MessageEntry) -> str:
    raw = entry.raw or {}
    uri = str(raw.get("uri") or "").strip()
    return uri if uri.startswith("http") else ""


def _summary_for(source_type: str, entry: MessageEntry) -> str:
    who = entry.talker_name or (str(entry.talker_id) if entry.talker_id else "")
    text = str(entry.text or entry.title or "").strip()
    if who:
        return f"{who}: {text[:80]}"
    return text[:80]


def _entry_timestamp(entry: MessageEntry) -> int:
    return int(entry.timestamp or 0)


class WinnerScanService:
    """三类来源统一扫描服务（手动 action / 后续调度共用一个入口）。"""

    def __init__(self, notification: NotificationService | None = None) -> None:
        self.notification = notification or NotificationService()

    # -- 单来源实现（各自独立成功/失败与推进） --

    def _scan_feed_source(
        self,
        source_type: str,
        msg_type: MessageType,
        entries: list[MessageEntry],
        *,
        state: dict[str, Any],
        existing: dict[str, dict[str, Any]],
    ) -> tuple[int, list[str]]:
        found_keys: list[str] = []
        newly_saved = 0
        for entry in entries[:MAX_REPLY_ENTRIES]:
            text = str(entry.text or entry.title or "")
            if not judge_keywords(text):
                continue
            key = _source_entry_key(source_type, entry)
            existing_record = existing.get(key)
            if existing_record is not None:
                continue
            event_id = f"winning_candidate:{key}"
            existing[key] = {
                "key": key,
                "source_type": source_type,
                "source_item_id": str(entry.id or ""),
                "sender": {"id": entry.talker_id, "name": entry.talker_name} if entry.talker_id else None,
                "summary": _summary_for(source_type, entry),
                "source_time": _entry_timestamp(entry),
                "discovered_at": int(time.time()),
                "source_url": _source_url(entry),
                "status": "candidate",
                "notification_event_id": event_id,
            }
            newly_saved += 1
            found_keys.append(key)
        return newly_saved, found_keys

    def _scan_dm_source(
        self,
        client: Any,
        *,
        state: dict[str, Any],
        existing: dict[str, dict[str, Any]],
    ) -> tuple[int, list[str]]:
        sessions = list_dm_sessions(client, size=MAX_DM_SESSIONS)
        newly_saved = 0
        found_keys: list[str] = []
        for session in sessions:
            if session.unread_count <= 0:
                continue
            messages = fetch_dm_messages(
                client,
                session.talker_id,
                session_type=1,
                size=min(max(1, session.unread_count), MAX_DM_MESSAGES_PER_SESSION),
            )
            for entry in messages:
                if entry.msg_type != MessageType.DM:
                    continue
                text = str(entry.text or entry.title or "")
                if not judge_keywords(text):
                    continue
                key = _source_entry_key("dm", entry)
                if existing.get(key) is not None:
                    continue
                event_id = f"winning_candidate:{key}"
                existing[key] = {
                    "key": key,
                    "source_type": "dm",
                    "source_item_id": str(entry.id or ""),
                    "sender": {"id": int(session.talker_id), "name": session.talker_name},
                    "summary": _summary_for("dm", entry),
                    "source_time": _entry_timestamp(entry),
                    "discovered_at": int(time.time()),
                    "source_url": _source_url(entry),
                    "status": "candidate",
                    "notification_event_id": event_id,
                }
                newly_saved += 1
                found_keys.append(key)
        return newly_saved, found_keys

    def run(
        self,
        client: Any = None,
        *,
        cancel_check: Callable[[], bool] | None = None,
        expected_uid: int | None = None,
        keywords: list[str] | None = None,
        notify: bool = True,
    ) -> dict[str, Any]:
        """执行一轮中奖深检。client 由调用方（Job/action）提供。

        返回 {ok, partial, sources:{mention/reply/dm:{status,last_error?,found,advance}}}
        - 普通失败 → partial=True，该 source advance=False（checkpoint 不推进）；
        - 平台风控/身份不一致 → 立即停止并抛出（不伪装成功）。
        """
        from src.bilibili_client import BilibiliClient

        close_after = client is None
        active_client = client if client is not None else BilibiliClient(warmup=False)
        _, uid = require_login()
        expected = int(expected_uid or uid)
        if int(uid) != expected:
            raise RuntimeError("登录账号与预期不符，已停止深检")

        state = _load_state()
        existing = _load_candidates()
        partial = False
        outcomes: dict[str, Any] = {}
        try:
            # @我的（feed，无全局分页则按最近一页解析）
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            try:
                entries, cursor = list_feed(active_client, MessageType.AT)
                saved, keys = self._scan_feed_source("mention", MessageType.AT, entries, state=state["mention"], existing=existing)
                # 1) candidate durable；2) 通知（claim→HTTP→terminal durable）；3) 最后推进 checkpoint
                _save_candidates(existing)
                self._dispatch_new(existing, keys, notify=notify)
                self._advance_source_state(state, "mention", cursor=cursor)
                outcomes["mention"] = {"status": "success", "found": saved, "advance": True}
            except Exception as exc:
                if risk_code_of(exc) is not None:
                    raise
                partial = True
                self._record_source_error(state, "mention", exc)
                outcomes["mention"] = {"status": "failed", "error": _safe_error(exc), "advance": False}

            # 回复我的
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            try:
                entries, cursor = list_feed(active_client, MessageType.REPLY)
                saved, keys = self._scan_feed_source("reply", MessageType.REPLY, entries, state=state["reply"], existing=existing)
                _save_candidates(existing)
                self._dispatch_new(existing, keys, notify=notify)
                self._advance_source_state(state, "reply", cursor=cursor)
                outcomes["reply"] = {"status": "success", "found": saved, "advance": True}
            except Exception as exc:
                if risk_code_of(exc) is not None:
                    raise
                partial = True
                self._record_source_error(state, "reply", exc)
                outcomes["reply"] = {"status": "failed", "error": _safe_error(exc), "advance": False}

            # 未读私信（串行读会话）
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            try:
                saved, keys = self._scan_dm_source(active_client, state=state["dm"], existing=existing)
                _save_candidates(existing)
                self._dispatch_new(existing, keys, notify=notify)
                self._advance_source_state(state, "dm")
                outcomes["dm"] = {"status": "success", "found": saved, "advance": True}
            except Exception as exc:
                if risk_code_of(exc) is not None:
                    raise
                partial = True
                self._record_source_error(state, "dm", exc)
                outcomes["dm"] = {"status": "failed", "error": _safe_error(exc), "advance": False}
        finally:
            if close_after:
                try:
                    active_client.close()
                except Exception:
                    pass
        return {"ok": not partial, "partial": partial, "sources": outcomes}

    # -- 落库 / 推进 / 通知 --

    def _advance_source_state(
        self,
        state: dict[str, Any],
        source_type: str,
        *,
        cursor: dict[str, Any] | None = None,
    ) -> None:
        """checkpoint 推进：只在候选已落库且通知 durable state 就绪后调用。"""
        entry = state[source_type]
        entry["last_success_at"] = int(time.time())
        entry["last_scan_at"] = int(time.time())
        entry["last_error"] = ""
        if cursor:
            cursor_id = str(cursor.get("id") or "0").strip()
            if cursor_id.isdigit():
                entry["cursor_id"] = int(cursor_id)
            try:
                entry["cursor_time"] = int(cursor.get("time") or entry.get("cursor_time") or 0)
            except (TypeError, ValueError):
                pass
        _save_state(state)

    def _record_source_error(self, state: dict[str, Any], source_type: str, exc: BaseException) -> None:
        entry = state[source_type]
        entry["last_error"] = _safe_error(exc)
        entry["last_scan_at"] = int(time.time())
        _save_state(state)

    def _dispatch_new(
        self,
        existing: dict[str, dict[str, Any]],
        keys: list[str],
        *,
        notify: bool,
    ) -> None:
        if not notify:
            return
        for key in keys:
            record = existing.get(key)
            if record is None:
                continue
            event_id = str(record.get("notification_event_id") or f"winning_candidate:{key}")
            lines = [
                f"来源：{record.get('source_type')}",
                f"发送者：{_sender_text(record.get('sender'))}",
                f"时间：{record.get('source_time')}",
                f"摘要：{record.get('summary')}",
            ]
            url = str(record.get("source_url") or "")
            if url:
                lines.append(f"链接：{url}")
            # 通知本地硬失败（claim/terminal 无法落盘）属于本地 durable 失败：
            # 当前 Source 不得推进 checkpoint（由调用方 catch 处理）。
            try:
                self.notification.dispatch(
                    event_id,
                    title="Binggo：疑似中奖线索",
                    body="\n".join(lines),
                    event_type="winning_candidate",
                )
            except NotificationLocalError:
                raise
            except Exception:  # noqa: BLE001 - 其余通知异常不影响扫描推进
                pass


def _sender_text(sender: Any) -> str:
    if not isinstance(sender, dict):
        return ""
    name = str(sender.get("name") or "")
    sender_id = sender.get("id")
    return name or (str(sender_id) if sender_id else "")


def _safe_error(exc: BaseException) -> str:
    # 普通错误不含 secret/URL；统一裁剪避免意外信息外泄。
    return str(exc).strip()[:200] or type(exc).__name__


service = WinnerScanService()
