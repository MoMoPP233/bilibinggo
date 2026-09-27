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
from src.sources.common import is_valid_dynamic_id, opus_link

_STATE_FILE = "winner_scan_state.json"
_CANDIDATES_FILE = "winner_candidates.json"
_NICKNAME_CACHE_FILE = "winner_nickname_cache.json"
_NICKNAME_CACHE_LIMIT = 500
_NICKNAME_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
MAX_REPLY_ENTRIES = 20
MAX_DM_SESSIONS = 20
MAX_DM_MESSAGES_PER_SESSION = 20
KEYWORD_LIMIT = 30

DEFAULT_KEYWORDS = [
    "~预约成功|预约主题|预约已成功",
    "中奖|恭喜|获得|幸运|抽中|奖品|填写地址|核对信息",
]

# V25：关注后自动回复（欢迎/感谢关注）过滤。
# 平台当前没有已确认的 auto_reply 专用字段，因此只做保守、高置信度的本地结构组合：
#   普通用户 DM + inbound(对方→我) + 会话存在关注关系标记 + 纯欢迎模板
#   + 无强中奖/领奖信号 + 无链接
# 任一条件不满足 → 保留为正常 Winner 扫描对象（宁可留可疑候选，不误杀真实中奖）。
_FILTERED_AUTO_REPLIES_LIMIT = 200
_SUPPRESSED_CANDIDATES_LIMIT = 500

# 强中奖/领奖信号：命中任一即否决过滤（优先级高于欢迎模板）。
# 注意：不含“恭喜/获得/幸运”这类泛化词——“恭喜成为我的粉丝”正是本次误判来源。
STRONG_WINNER_MARKERS = (
    "中奖",
    "抽中",
    "获奖",
    "领奖",
    "兑奖",
    "奖品",
    "抽奖",
    "开奖",
    "中奖名单",
    "中奖用户",
    "中奖信息",
    "获奖名单",
    "获奖用户",
    "幸运用户",
    "填写地址",
    "收货地址",
    "核对信息",
    "联系方式",
    "领取奖品",
    "领取奖励",
    "恭喜中奖",
    "恭喜获奖",
    "收件人",
    "联系电话",
    "收件地址",
    "收货信息",
)

_WINNER_INTENT_MARKERS = (
    "中奖",
    "获奖",
    "恭喜中奖",
    "恭喜获奖",
    "领奖",
    "领取奖品",
    "领取奖励",
    "兑奖",
    "奖品",
)
_DELIVERY_INFO_MARKERS = (
    "收件人",
    "联系电话",
    "联系方式",
    "收件地址",
    "收货地址",
    "收货信息",
    "收件信息",
    "回复收货信息",
    "发送收货信息",
)
_DELIVERY_AUXILIARY_MARKERS = (
    "逾期未发送",
    "自动放弃领奖",
    "放弃领奖",
    "请在",
    "请于",
)

_LINK_RE = re.compile(
    r"https?://|www\.|://|b23\.tv|bilibili\.com|t\.bilibili\.com|bili2233\.cn",
    re.IGNORECASE,
)

# 受控欢迎短语（顺序即匹配优先级；每次从剩余串开头消费一个短语）。
_WELCOME_PHRASES = (
    re.compile(r"(?:感谢|谢谢|多谢|感恩)(?:你|您|你的|您的)?(?:关注|支持|喜欢|陪伴)"),
    re.compile(
        r"欢迎(?:你|您|大家)?(?:关注|来到|加入|成为)"
        r"(?:我的|本|这里|我)?(?:频道|空间|主页|直播间|粉丝团|粉丝|大家庭)?"
    ),
    re.compile(r"(?:感谢|谢谢|多谢|感恩)?(?:你|您)?(?:成为|加入)(?:我的|本)?(?:粉丝|粉丝团|大家庭)"),
    re.compile(r"(?:恭喜|祝贺)(?:你|您)?(?:成为|加入)(?:我的|本)?(?:粉丝|粉丝团|大家庭)"),
    re.compile(r"(?:与|和|以及)(?:关注|支持|喜欢|陪伴)"),
)

# 允许出现在欢迎语最前面的称呼/问候（消费后必须被欢迎短语完全覆盖）。
_LEADING_ADDRESS_RE = re.compile(
    r"^(?:亲爱的|亲|宝子|小可爱|朋友们|朋友|小伙伴们|小伙伴|大家|"
    r"你好|您好|哈喽|嗨|hello|hi|在吗)+"
)


def has_strong_winner_signal(text: str) -> bool:
    value = str(text or "")
    return any(marker in value for marker in STRONG_WINNER_MARKERS)


def has_delivery_winner_signal(text: str) -> bool:
    """识别领奖收货信息组合，避免“电话/地址”单独误报。"""
    value = str(text or "")
    intent = any(marker in value for marker in _WINNER_INTENT_MARKERS)
    fields = sum(marker in value for marker in _DELIVERY_INFO_MARKERS)
    auxiliary = any(marker in value for marker in _DELIVERY_AUXILIARY_MARKERS)
    return fields >= 2 or (intent and (fields >= 1 or auxiliary))


def contains_link(text: str) -> bool:
    return bool(_LINK_RE.search(str(text or "")))


def is_welcome_template(text: str) -> bool:
    """严格欢迎模板判定：文本（去标点/emoji/空白后）必须由受控欢迎短语完整组成。

    不做模糊语义分类、不做 LLM；任何剩余未知内容一律判定为“非欢迎模板”。
    """
    raw = str(text or "").strip()
    if not raw or len(raw) > 80:
        return False
    compact = "".join(ch for ch in raw if ch.isalnum())
    if not compact:
        return False
    while True:
        match = _LEADING_ADDRESS_RE.match(compact)
        if not match or match.end() == 0:
            break
        compact = compact[match.end() :]
    if not compact:
        return False
    matched = False
    for _ in range(8):
        progressed = False
        for pattern in _WELCOME_PHRASES:
            match = pattern.match(compact)
            if match and match.end() > 0:
                compact = compact[match.end() :]
                matched = True
                progressed = True
        if not progressed:
            break
    return matched and not compact


def is_high_confidence_follow_auto_reply(entry: MessageEntry, session: Any) -> bool:
    """高置信度“关注后自动回复”判定（仅用于 DM，纯本地、无副作用）。"""
    raw = entry.raw or {}
    # A. 普通用户 DM：system_msg_type == 0（平台系统消息已由上游过滤）。
    try:
        if int(raw.get("system_msg_type") or 0) != 0:
            return False
    except (TypeError, ValueError):
        return False
    # B. inbound：对方（talker）→ 我；sender 缺失或为自己时绝不按自动回复过滤。
    try:
        talker_id = int(entry.talker_id or 0)
        sender_uid = int(raw.get("sender_uid") or 0)
    except (TypeError, ValueError):
        return False
    if talker_id <= 0 or sender_uid <= 0 or sender_uid != talker_id:
        return False
    # C. 会话存在关注关系标记（is_follow 语义见报告；仅在明确 True 时使用）。
    if getattr(session, "is_follow", None) is not True:
        return False
    text = str(entry.text or entry.title or "")
    # E/F. 强中奖信号或链接一律否决过滤。
    if has_strong_winner_signal(text) or contains_link(text):
        return False
    # D. 纯欢迎模板。
    return is_welcome_template(text)


def is_explicit_auto_reply(entry: MessageEntry) -> bool:
    """识别平台 payload 明确标记的自动回复，不依赖正文文案。"""
    raw = entry.raw or {}
    for key in ("auto_reply", "is_auto_reply"):
        if raw.get(key) is True or str(raw.get(key) or "").strip().lower() in {"true", "1", "yes"}:
            return True
    for key in ("message_type", "msg_type", "messageType"):
        marker = str(raw.get(key) or "").strip().lower().replace("-", "_")
        if marker in {"auto_reply", "autoreply", "automatic_reply"}:
            return True
    return False


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
    # 默认规则之外补充领奖收货信息组合；单独“地址/电话/回复/私信”仍不会命中。
    return result or (patterns is None and has_delivery_winner_signal(text))


# ---------------------------------------------------------------------------
# per-Profile 状态存取
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    return get_profile_dir() / _STATE_FILE


def _candidates_path() -> Path:
    return get_profile_dir() / _CANDIDATES_FILE


def _nickname_cache_path() -> Path:
    return get_profile_dir() / _NICKNAME_CACHE_FILE


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
        for key in ("mention", "reply", "dm"):
            entry = sources.get(key)
            if isinstance(entry, dict):
                state[key].update(
                    {
                        field: entry.get(field, state[key][field])
                        for field in state[key]
                    }
                )
    # V25：已过滤的关注自动回复 stable key（per-Profile，有界，只存 key 不存正文）。
    filtered = stored.get("filtered_auto_replies")
    if isinstance(filtered, list):
        state["filtered_auto_replies"] = [
            str(item) for item in filtered if str(item or "").strip()
        ][-_FILTERED_AUTO_REPLIES_LIMIT:]
    else:
        state["filtered_auto_replies"] = []
    suppressed = stored.get("suppressed_candidates")
    if isinstance(suppressed, list):
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in suppressed:
            if isinstance(item, dict):
                key = str(item.get("key") or "").strip()
                reason = str(item.get("reason") or "manual_delete").strip() or "manual_delete"
                at = item.get("suppressed_at")
            else:
                key = str(item or "").strip()
                reason = "manual_delete"
                at = None
            if not key or key in seen:
                continue
            seen.add(key)
            normalized.append({"key": key, "reason": reason, "suppressed_at": at})
        state["suppressed_candidates"] = normalized[-_SUPPRESSED_CANDIDATES_LIMIT:]
    else:
        state["suppressed_candidates"] = []
    return state


def _save_state(state: dict[str, Any]) -> None:
    sources = {
        key: state[key] for key in ("mention", "reply", "dm") if key in state
    }
    filtered = state.get("filtered_auto_replies") or []
    suppressed = state.get("suppressed_candidates") or []
    _write_json(
        _state_path(),
        {
            "version": 1,
            "sources": sources,
            "filtered_auto_replies": [
                str(item) for item in filtered if str(item or "").strip()
            ][-_FILTERED_AUTO_REPLIES_LIMIT:],
            "suppressed_candidates": [
                item
                for item in suppressed
                if isinstance(item, dict) and str(item.get("key") or "").strip()
            ][-_SUPPRESSED_CANDIDATES_LIMIT:],
        },
    )


def _load_candidates() -> dict[str, dict[str, Any]]:
    stored = _read_json(_candidates_path())
    records = stored.get("candidates")
    if not isinstance(records, list):
        return {}
    return {str(item.get("key") or ""): item for item in records if isinstance(item, dict)}


def _save_candidates(candidates: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(candidates.values(), key=lambda item: int(item.get("discovered_at") or 0), reverse=True)
    _write_json(_candidates_path(), {"version": 1, "candidates": ordered[-500:]})


def _load_nickname_cache() -> dict[str, dict[str, Any]]:
    stored = _read_json(_nickname_cache_path())
    entries = stored.get("entries")
    if not isinstance(entries, dict):
        return {}
    now = int(time.time())
    result: dict[str, dict[str, Any]] = {}
    for uid, item in entries.items():
        key = str(uid).strip()
        if not key.isdigit() or int(key) <= 0 or not isinstance(item, dict):
            continue
        nickname = str(item.get("nickname") or "").strip()
        cached_at = int(item.get("cached_at") or 0)
        if nickname and cached_at > 0 and now - cached_at <= _NICKNAME_CACHE_TTL_SECONDS:
            result[key] = {"nickname": nickname, "cached_at": cached_at}
    return result


def _save_nickname_cache(cache: dict[str, dict[str, Any]]) -> None:
    ordered = sorted(cache.items(), key=lambda pair: int(pair[1].get("cached_at") or 0), reverse=True)
    _write_json(
        _nickname_cache_path(),
        {"version": 1, "entries": dict(ordered[:_NICKNAME_CACHE_LIMIT])},
    )


def _candidate_is_explicit_auto_reply(record: dict[str, Any]) -> bool:
    """只根据候选中持久化的结构化元数据判断历史自动回复。"""
    payloads: list[dict[str, Any]] = [record]
    for key in ("raw", "metadata", "source_payload", "message"):
        nested = record.get(key)
        if isinstance(nested, dict):
            payloads.append(nested)
    for payload in payloads:
        for key in ("auto_reply", "is_auto_reply"):
            if payload.get(key) is True or str(payload.get(key) or "").strip().lower() in {"true", "1", "yes"}:
                return True
        for key in ("message_type", "msg_type", "messageType"):
            marker = str(payload.get(key) or "").strip().lower().replace("-", "_")
            if marker in {"auto_reply", "autoreply", "automatic_reply"}:
                return True
    if str(record.get("source_type") or "") == "dm":
        for payload in payloads:
            try:
                if int(payload.get("system_msg_type") or 0) in {1, 5, 7, 8, 9}:
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _suppressed_candidate_keys(state: dict[str, Any] | None = None) -> set[str]:
    current = state if state is not None else _load_state()
    values = current.get("suppressed_candidates") or []
    result: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            key = str(item.get("key") or "").strip()
        else:
            key = str(item or "").strip()
        if key:
            result.add(key)
    return result


def suppress_candidate(key: str, *, reason: str = "manual_delete") -> dict[str, Any]:
    """Persist Profile-local suppression for one stable candidate identity.

    Candidate and notification records remain untouched; the key is filtered from
    future views/scans so the same source event cannot reappear or notify again.
    """
    stable_key = str(key or "").strip()
    if not stable_key:
        raise ValueError("候选 identity 不能为空")
    candidates = _load_candidates()
    if stable_key not in candidates:
        raise KeyError(stable_key)
    with get_atomic_path_lock(_state_path()):
        state = _load_state()
        entries = list(state.get("suppressed_candidates") or [])
        if not any(isinstance(item, dict) and str(item.get("key") or "") == stable_key for item in entries):
            entries.append(
                {
                    "key": stable_key,
                    "reason": str(reason or "manual_delete"),
                    "suppressed_at": int(time.time()),
                }
            )
            state["suppressed_candidates"] = entries[-_SUPPRESSED_CANDIDATES_LIMIT:]
            _save_state(state)
    return {"key": stable_key, "reason": str(reason or "manual_delete"), "suppressed": True}


def list_candidates(limit: int = 50) -> list[dict[str, Any]]:
    candidates = _load_candidates()
    suppressed = _suppressed_candidate_keys()
    ordered = sorted(
        (
            item
            for item in candidates.values()
            if str(item.get("key") or "") not in suppressed
            and not _candidate_is_explicit_auto_reply(item)
        ),
        key=lambda item: int(item.get("discovered_at") or 0),
        reverse=True,
    )
    return [_candidate_view(item) for item in ordered[:limit]]


def scan_status_view() -> dict[str, Any]:
    return {
        "sources": _load_state(),
        "candidates": list_candidates(30),
    }


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------


def _source_entry_key(source_type: str, entry: MessageEntry) -> str | None:
    """构造候选/事件去重键；缺少真实稳定平台 ID 时返回 None（fail-closed，避免空 key 碰撞）。

    稳定字段缺失时绝不回退到时间/随机/索引——那会让同一条消息在不同扫描生成
    不同 key 并造成重复通知。缺失项由调用方记录 malformed 并跳过（不影响其它项）。
    """
    if source_type == "dm":
        talker_id = int(entry.talker_id or 0)
        raw = entry.raw or {}
        seqno = str(raw.get("msg_seqno") or "").strip() or str(entry.id or "").strip()
        if talker_id <= 0 or not seqno or seqno == "0":
            return None
        return f"dm:{talker_id}:{seqno}"
    item_id = str(entry.id or "").strip()
    if not item_id or item_id == "0":
        return None
    return f"{source_type}:{item_id}"


def _payloads(raw: dict[str, Any] | None) -> list[dict[str, Any]]:
    root = raw if isinstance(raw, dict) else {}
    payloads = [root]
    for key in ("item", "detail", "source", "data", "message"):
        nested = root.get(key)
        if isinstance(nested, dict):
            payloads.append(nested)
    return payloads


def _dynamic_id_from_raw(raw: dict[str, Any] | None) -> str:
    for payload in _payloads(raw):
        for field in ("dynamic_id", "dynamicId", "business_id"):
            value = str(payload.get(field) or "").strip()
            if is_valid_dynamic_id(value):
                return value
    return ""


def _source_url_from_raw(raw: dict[str, Any] | None) -> str:
    for payload in _payloads(raw):
        for field in (
            "source_url",
            "dynamic_url",
            "reply_url",
            "jump_url",
            "detail_url",
            "native_url",
            "conversation_url",
            "session_url",
            "uri",
            "url",
        ):
            uri = str(payload.get(field) or "").strip()
            if uri.startswith(("http://", "https://")):
                return uri
    dynamic_id = _dynamic_id_from_raw(raw)
    return opus_link(dynamic_id) if dynamic_id else ""


def _source_url(entry: MessageEntry) -> str:
    return _source_url_from_raw(entry.raw)


def _dm_source_url(entry: MessageEntry, session: Any) -> str:
    url = _source_url(entry)
    if url:
        return url
    raw = getattr(session, "raw", None)
    url = _source_url_from_raw(raw)
    if url:
        return url
    # 普通用户会话可直接进入对应会话；系统消息不得套用 whisper pattern。
    try:
        system_msg_type = int(getattr(session, "system_msg_type", 0) or 0)
        sender_uid = int(getattr(session, "talker_id", 0) or 0)
        entry_system_msg_type = int((entry.raw or {}).get("system_msg_type") or 0)
    except (TypeError, ValueError):
        return ""
    if system_msg_type in {1, 5, 7, 8, 9} or entry_system_msg_type in {1, 5, 7, 8, 9} or sender_uid <= 0:
        return ""
    if not re.fullmatch(r"[1-9][0-9]*", str(sender_uid)):
        return ""
    return f"https://message.bilibili.com/#/whisper/mid{sender_uid}"


def _sender_name_from_raw(raw: dict[str, Any] | None) -> str:
    for payload in _payloads(raw):
        for field in ("nickname", "sender_name", "senderName", "uname", "name"):
            value = str(payload.get(field) or "").strip()
            if value:
                return value
        for field in ("user", "sender", "account_info"):
            nested = payload.get(field)
            if isinstance(nested, dict):
                value = _sender_name_from_raw(nested)
                if value:
                    return value
    return ""


def _candidate_sender(record: dict[str, Any]) -> dict[str, Any] | None:
    sender = record.get("sender") if isinstance(record.get("sender"), dict) else {}
    sender_id = sender.get("id") or record.get("sender_id") or record.get("talker_id")
    sender_name = (
        sender.get("name")
        or sender.get("nickname")
        or record.get("sender_name")
        or record.get("nickname")
        or _sender_name_from_raw(record.get("raw"))
    )
    if not sender_id and not sender_name:
        return None
    return {"id": sender_id, "name": str(sender_name or "")}


def _candidate_view(record: dict[str, Any]) -> dict[str, Any]:
    view = dict(record)
    sender = _candidate_sender(record)
    if sender is not None:
        view["sender"] = sender
    dynamic_id = str(view.get("dynamic_id") or "").strip() or _dynamic_id_from_raw(record.get("raw"))
    if dynamic_id:
        view["dynamic_id"] = dynamic_id
    if not str(view.get("source_url") or ""):
        source_url = _source_url_from_raw(record.get("raw"))
        if not source_url and is_valid_dynamic_id(dynamic_id):
            source_url = opus_link(dynamic_id)
        if source_url:
            view["source_url"] = source_url
    return view


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

    def _lookup_nickname(
        self,
        client: Any,
        sender_uid: int,
        *,
        cache: dict[str, dict[str, Any]],
        attempted: set[int],
        blocked: list[bool],
    ) -> str:
        """Resolve one newly discovered DM sender without fan-out.

        Cache hits are local-only. A UID is attempted at most once per scan; a
        platform-risk response disables all subsequent nickname lookups while
        leaving the winner candidate on the UID fallback path.
        """
        if sender_uid <= 0 or blocked[0]:
            return ""
        key = str(sender_uid)
        cached = cache.get(key)
        now = int(time.time())
        if cached and now - int(cached.get("cached_at") or 0) <= _NICKNAME_CACHE_TTL_SECONDS:
            return str(cached.get("nickname") or "").strip()
        if sender_uid in attempted:
            return ""
        attempted.add(sender_uid)
        try:
            url = "https://api.bilibili.com/x/space/acc/info"
            if hasattr(client, "get_json"):
                payload = client.get_json(
                    url,
                    params={"mid": sender_uid},
                    referer="https://space.bilibili.com",
                    retries=0,
                )
            else:
                payload = client.request_json(
                    url,
                    params={"mid": sender_uid},
                    referer="https://space.bilibili.com",
                    retries=0,
                )
            code = payload.get("code") if isinstance(payload, dict) else None
            if code not in (None, 0):
                raise RuntimeError(f"API error {code}: {payload.get('message') or payload.get('msg') or ''}")
            data = payload.get("data") if isinstance(payload, dict) else None
            data = data if isinstance(data, dict) else {}
            nickname = str(data.get("name") or data.get("uname") or data.get("nickname") or "").strip()
            if not nickname:
                return ""
            cache[key] = {"nickname": nickname, "cached_at": now}
            _save_nickname_cache(cache)
            return nickname
        except Exception as exc:  # noqa: BLE001 - enrichment must never drop a candidate
            if risk_code_of(exc) is not None:
                blocked[0] = True
            return ""

    # -- 单来源实现（各自独立成功/失败与推进） --

    def _scan_feed_source(
        self,
        source_type: str,
        msg_type: MessageType,
        entries: list[MessageEntry],
        *,
        state: dict[str, Any],
        existing: dict[str, dict[str, Any]],
        suppressed: set[str] | None = None,
    ) -> tuple[int, list[str]]:
        found_keys: list[str] = []
        newly_saved = 0
        suppressed_keys = suppressed if suppressed is not None else _suppressed_candidate_keys()
        for entry in entries[:MAX_REPLY_ENTRIES]:
            text = str(entry.text or entry.title or "")
            if not judge_keywords(text):
                continue
            key = _source_entry_key(source_type, entry)
            if key is None:
                self._record_malformed(state, source_type, entry)
                continue
            existing_record = existing.get(key)
            if existing_record is not None or key in suppressed_keys:
                continue
            event_id = f"winning_candidate:{key}"
            existing[key] = {
                "key": key,
                "source_type": source_type,
                "source_item_id": str(entry.id or ""),
                "sender": {
                    "id": entry.talker_id,
                    "name": entry.talker_name or _sender_name_from_raw(entry.raw),
                }
                if entry.talker_id
                else None,
                "summary": _summary_for(source_type, entry),
                "source_time": _entry_timestamp(entry),
                "discovered_at": int(time.time()),
                "source_url": _source_url(entry),
                "status": "candidate",
                "notification_event_id": event_id,
            }
            dynamic_id = _dynamic_id_from_raw(entry.raw)
            if dynamic_id:
                existing[key]["dynamic_id"] = dynamic_id
            newly_saved += 1
            found_keys.append(key)
        return newly_saved, found_keys

    def _record_malformed(self, state: dict[str, Any], source_type: str, entry: MessageEntry) -> None:
        """命中关键词但缺少稳定平台 ID 的条目：记录本地 skip 原因（有界），不建 candidate。

        只保存通用原因与来源（不落消息正文，避免观察面泄露消息内容）。
        该条目无法被稳定引用，本轮跳过；不影响同批其它成功项，也不推进任何错误状态。
        """
        skipped = state.setdefault("malformed_skipped", [])
        skipped.append(
            {
                "ts": int(time.time()),
                "source_type": source_type,
                "reason": "条目缺少稳定平台 ID，已跳过（避免空 key 碰撞 / 随机 key 重复通知）",
            }
        )
        state["malformed_skipped"] = skipped[-20:]

    def _scan_dm_source(
        self,
        client: Any,
        *,
        state: dict[str, Any],
        existing: dict[str, dict[str, Any]],
    ) -> tuple[int, list[str], int]:
        """返回 (newly_saved, found_keys, auto_reply_skipped)。

        过滤只发生在 candidate/dispatch 之前；被过滤的消息不落 candidate、不通知。
        同一 stable key 已记录过滤后不再重复计数（V25 去噪）。
        """
        sessions = list_dm_sessions(client, size=MAX_DM_SESSIONS)
        filtered = {
            str(item)
            for item in (state.get("filtered_auto_replies") or [])
            if str(item or "").strip()
        }
        newly_filtered: list[str] = []
        auto_reply_skipped = 0
        newly_saved = 0
        found_keys: list[str] = []
        suppressed = _suppressed_candidate_keys(state)
        nickname_cache = _load_nickname_cache()
        nickname_attempted: set[int] = set()
        nickname_lookup_blocked = [False]
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
                key = _source_entry_key("dm", entry)
                if key is not None and key in filtered:
                    # 已记录为关注自动回复：不生成 candidate、不通知、不重复计数。
                    continue
                if is_explicit_auto_reply(entry) or is_high_confidence_follow_auto_reply(entry, session):
                    if key is not None:
                        filtered.add(key)
                        newly_filtered.append(key)
                        auto_reply_skipped += 1
                    continue
                text = str(entry.text or entry.title or "")
                if not judge_keywords(text):
                    continue
                if key is None:
                    self._record_malformed(state["dm"], "dm", entry)
                    continue
                if existing.get(key) is not None or key in suppressed:
                    continue
                event_id = f"winning_candidate:{key}"
                sender_uid = int(session.talker_id)
                sender_name = entry.talker_name or session.talker_name or _sender_name_from_raw(entry.raw)
                if not sender_name:
                    sender_name = self._lookup_nickname(
                        client,
                        sender_uid,
                        cache=nickname_cache,
                        attempted=nickname_attempted,
                        blocked=nickname_lookup_blocked,
                    )
                existing[key] = {
                    "key": key,
                    "source_type": "dm",
                    "source_item_id": str(entry.id or ""),
                    "sender": {
                        "id": sender_uid,
                        "name": sender_name,
                    },
                    "summary": _summary_for("dm", entry),
                    "source_time": _entry_timestamp(entry),
                    "discovered_at": int(time.time()),
                    "source_url": _dm_source_url(entry, session),
                    "status": "candidate",
                    "notification_event_id": event_id,
                }
                newly_saved += 1
                found_keys.append(key)
        if newly_filtered:
            merged: list[str] = []
            seen: set[str] = set()
            for item in list(state.get("filtered_auto_replies") or []) + newly_filtered:
                value = str(item or "").strip()
                if not value or value in seen:
                    continue
                seen.add(value)
                merged.append(value)
            state["filtered_auto_replies"] = merged[-_FILTERED_AUTO_REPLIES_LIMIT:]
        return newly_saved, found_keys, auto_reply_skipped

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

        返回 {ok, partial, sources:{mention/reply/dm:{status,last_error?,found,advance}},
              auto_reply_skipped_count}
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
        suppressed = _suppressed_candidate_keys(state)
        partial = False
        outcomes: dict[str, Any] = {}
        try:
            # @我的（feed，无全局分页则按最近一页解析）
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            try:
                entries, cursor = list_feed(active_client, MessageType.AT)
                saved, keys = self._scan_feed_source(
                    "mention", MessageType.AT, entries, state=state["mention"], existing=existing, suppressed=suppressed
                )
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
                saved, keys = self._scan_feed_source(
                    "reply", MessageType.REPLY, entries, state=state["reply"], existing=existing, suppressed=suppressed
                )
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
                saved, keys, auto_reply_skipped = self._scan_dm_source(
                    active_client, state=state, existing=existing
                )
                _save_candidates(existing)
                self._dispatch_new(existing, keys, notify=notify)
                self._advance_source_state(state, "dm")
                outcomes["dm"] = {
                    "status": "success",
                    "found": saved,
                    "advance": True,
                    "auto_reply_skipped": auto_reply_skipped,
                }
            except Exception as exc:
                if risk_code_of(exc) is not None:
                    raise
                partial = True
                self._record_source_error(state, "dm", exc)
                outcomes["dm"] = {
                    "status": "failed",
                    "error": _safe_error(exc),
                    "advance": False,
                    "auto_reply_skipped": 0,
                }
        finally:
            if close_after:
                try:
                    active_client.close()
                except Exception:
                    pass
        auto_reply_skipped_count = sum(
            int(item.get("auto_reply_skipped") or 0) for item in outcomes.values()
        )
        return {
            "ok": not partial,
            "partial": partial,
            "sources": outcomes,
            "auto_reply_skipped_count": auto_reply_skipped_count,
        }

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
