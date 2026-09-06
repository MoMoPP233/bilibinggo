"""自动远程任务风控状态（per-Profile 本地 JSON，0 远程）。

只做自动调度层的总熔断：
- cleanup 自己的 maintenance_risk_paused 独立保留，不与这里合并。
- 任意自动远程任务（refresh batch / participate / cleanup 等）命中明确平台风控
  （-352 / -509 / HTTP 429 / rate-limit / risk-control / 风控 / 限流）时写入固定冷却。
- 冷却固定 6 小时（AUTO_REMOTE_RISK_COOLDOWN_SECONDS）。
- 冷却结束只恢复“调度资格”：程序不主动探测、不补跑；等下一次正常 Scheduler 任务。
- Profile 之间互相隔离；重启后剩余冷却仍在；无人工暂停/恢复概念。
- 不升级数据库 Schema（保持 v10）。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

from web.auto_config import AUTO_REMOTE_RISK_COOLDOWN_SECONDS
from src.platform_risk import matches_platform_risk as _matches_platform_risk

_FILE_NAME = "auto_remote_state.json"

# 同一风控事件经多层传播时，短时间内的重复 record 不重复延长冷却。
# 只有距离上次记录超过该窗口（视为新一次真实风控）才重新计时。
_RECORD_EXTEND_GRACE_SECONDS = 300


def matches_platform_risk(message: object) -> bool:
    """统一风控判定：结构化 API code / HTTP 429 / 官方错误信封，绝不扫描正文文本。"""
    return _matches_platform_risk(message)


def _state_path() -> Path:
    from src.data_paths import get_profile_dir

    return get_profile_dir() / _FILE_NAME


def _state_verdict(raw: dict[str, Any]) -> str:
    """校验“关键 cooldown 字段”格式：字段存在但类型非法 → invalid（fail-closed）。

    未知额外字段与缺失可选字段按旧语义处理，不做过度严格 Schema。
    - paused_until/paused_at 必须是不带 bool 的整数；
    - paused 必须是 bool 或 0/1（旧文件兼容）。
    """
    for key in ("paused_at", "paused_until"):
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            return "invalid"
    paused = raw.get("paused")
    if paused is not None and not isinstance(paused, bool):
        if not (isinstance(paused, int) and paused in (0, 1)):
            return "invalid"
    return "ok"


def _load_state() -> tuple[str, dict[str, Any]]:
    """读取 per-Profile 风控状态，明确区分 missing / ok / invalid / unreadable。

    - missing：从未产生过 auto risk state（新安装/新 Profile）→ 正常 unpaused；
    - ok：JSON 根是 object 且关键 cooldown 字段格式合法；
    - invalid：文件存在但 JSON 损坏/根不是 object/关键字段格式非法；
    - unreadable：存在但无法读取（PermissionError/OSError）。
    决策方一律基于该三元结果；读取/解析失败绝不会再被伪装成“没有冷却”。
    """
    path = _state_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", {}
    except OSError as exc:
        return "unreadable", {"_error": str(exc)}
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return "invalid", {}
    if not isinstance(raw, dict):
        return "invalid", {}
    if _state_verdict(raw) != "ok":
        return "invalid", {}
    return "ok", raw


def _read_state() -> dict[str, Any]:
    """仅用于原始写回/测试读取；决策逻辑不得使用本函数（见 _load_state）。"""
    return _load_state()[1]


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_FILE_NAME}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def risk_pause_state(now_ts: int | None = None) -> dict[str, Any]:
    """读取当前 Profile 的风控状态视图（只读本地，0 远程）。

    paused 为“当前是否禁止新的自动远程阶段”：
    - 文件存在但损坏/不可读 → Fail-Closed（paused=True, state_status=invalid/unreadable），
      直到文件被修复或出现新的明确风控记录，绝不自动删除/清空；
    - paused_until 已过时 paused 视为过期（expired=True），但仍只读不联网；
    - 文件缺失（从未产生过状态）→ paused=False，正常 unpaused。
    """
    status, state = _load_state()
    now = int(time.time()) if now_ts is None else int(now_ts)
    unreadable = status in ("invalid", "unreadable")
    paused = bool(state.get("paused")) or unreadable
    paused_until = state.get("paused_until")
    paused_until_int = paused_until if isinstance(paused_until, int) else None
    expired = paused and not unreadable and paused_until_int is not None and now >= paused_until_int
    effective_paused = paused and not expired
    return {
        "paused": effective_paused,
        "expired": bool(expired) and paused,
        "paused_at": state.get("paused_at"),
        "paused_until": paused_until_int,
        "trigger_stage": str(state.get("trigger_stage") or ""),
        "code": str(state.get("code") or ""),
        "reason": str(state.get("reason") or ""),
        "state_status": status,
    }


def is_risk_paused(now_ts: int | None = None) -> bool:
    return bool(risk_pause_state(now_ts=now_ts).get("paused"))


def record_auto_remote_risk(
    *,
    trigger_stage: str,
    reason: object = "",
    code: str = "",
    now_ts: int | None = None,
    cooldown_seconds: int = AUTO_REMOTE_RISK_COOLDOWN_SECONDS,
) -> dict[str, Any]:
    """命中明确风控后写入固定冷却（per-Profile 本地，0 远程）。

    同一风控事件若经过业务层 + 调度层两次传播，只在很短窗口内各记录一次：
    幂等合并（不把 paused_until 无意义向后推）；窗口之外视为新一次真实风控，
    正常重新计时（原「冷却中再次命中并延长」语义保留）。
    """
    now = int(time.time()) if now_ts is None else int(now_ts)
    status, state = _load_state()
    if status != "ok":
        # 文件缺失/损坏时记录一次“新发生的明确风控”属于正常写入（真实远程风险事件），
        # 从空状态开始写入合法记录；不会被自动清除逻辑清掉。
        state = {}
    paused_at = state.get("paused_at")
    if (
        state.get("paused")
        and isinstance(paused_at, int)
        and now - paused_at < _RECORD_EXTEND_GRACE_SECONDS
    ):
        # 同一次风控的重复传播：保留首次冷却起点，只补全可读字段，不延长。
        state.update(
            {
                "trigger_stage": str(trigger_stage or "").strip() or state.get("trigger_stage"),
                "code": str(code or "").strip() or state.get("code"),
                "reason": str(reason or "").strip() or state.get("reason"),
            }
        )
        _write_state(state)
        return risk_pause_state(now_ts=now)
    state.update(
        {
            "paused": True,
            "paused_at": now,
            "paused_until": now + int(cooldown_seconds),
            "trigger_stage": str(trigger_stage or "").strip(),
            "code": str(code or "").strip(),
            "reason": str(reason or "").strip(),
        }
    )
    _write_state(state)
    return risk_pause_state(now_ts=now)


def clear_expired_risk_pause(now_ts: int | None = None) -> bool:
    """冷却已结束则本地清除风控（0 远程）；返回是否真的清除。

    只恢复“下一次正常调度资格”，绝不主动联网、绝不补跑。
    """
    state = risk_pause_state(now_ts=now_ts)
    if not state.get("expired"):
        return False
    now = int(time.time()) if now_ts is None else int(now_ts)
    _write_state(
        {
            "paused": False,
            "paused_at": None,
            "paused_until": None,
            "trigger_stage": "",
            "code": "",
            "reason": "",
        }
    )
    return True


def _clear_for_tests() -> None:
    try:
        _state_path().unlink(missing_ok=True)
    except OSError:
        pass
