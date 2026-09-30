"""定时点击调度器配置。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.user_data_io import atomic_write_json

# V72 调度频率（单一事实来源）。固定任务全部错峰并落在 07:00～24:00。
REFRESH_SLOTS = ((7, 10), (12, 40), (18, 10), (22, 40))
# 兼容旧状态/测试消费者；实际判断统一使用 REFRESH_SLOTS。
REFRESH_HOURS = frozenset(hour for hour, _minute in REFRESH_SLOTS)
# V38：上一轮结束后独立等待 240～420 秒，不再使用固定分钟槽。
TRIPLE_DELAY_MIN_SECONDS = 240
TRIPLE_DELAY_MAX_SECONDS = 420
# Legacy compatibility value for old API consumers. Cleanup has no scheduler cadence.
CLEANUP_MAINTAIN_INTERVAL_HOURS = 24
# 中奖深检：沿用 CN_TZ，每天 12:10、22:10 两次。
WINNER_SCAN_HOURS = frozenset({12, 22})
WINNER_SCAN_MINUTE = 10
# V72：关注动态每天 6 次，固定在 08:00/11:00/14:00/17:00/20:00/23:00。
FOLLOWING_FEED_SCAN_HOURS = frozenset({8, 11, 14, 17, 20, 23})
FOLLOWING_FEED_SCAN_MINUTE = 0
# Legacy names retained for read-only consumers; scheduler no longer derives cadence from them.
FOLLOWING_FEED_SCAN_HOUR_INTERVAL = 3
FOLLOWING_FEED_SCAN_HOUR_OFFSET = 2

ALLOWED_CLICK_ACTIONS = frozenset(
    {
        "refresh_all",
        "refresh_watch",
        "refresh_status",
        "participate_triple",
        "following_feed_scan",
        "winner_deep_scan",
    }
)

ACTION_LABELS = {
    "refresh_all": "一键更新活动链接",
    "refresh_watch": "更新监控用户动态",
    "refresh_status": "刷新任务状态",
    "participate_triple": "三连参与",
    "cleanup_auto_maintain": "清理数据自动维护",
    "cleanup_maintenance_cycle": "清理维护",
    "following_feed_scan": "关注动态补漏扫描",
    "winner_deep_scan": "中奖深探",
}

# Legacy compatibility state for the /api/auto/cleanup-maintain contract.
# The scheduler never consults this value; cleanup is manual-only.
_CLEANUP_MAINTAIN_ENABLED = False

NIGHT_CONTINUE_CONFIG_FILE = "auto_scheduler_config.json"
_NIGHT_CONTINUE_KEY = "night_continue_enabled"


def _night_continue_path() -> Any:
    from src.data_paths import get_profile_dir

    return get_profile_dir() / NIGHT_CONTINUE_CONFIG_FILE


def get_night_continue_enabled() -> bool:
    """读取当前 Profile 的夜间继续运行开关；缺失/损坏均按默认 OFF。"""
    try:
        import json

        payload = json.loads(_night_continue_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    return bool(payload.get(_NIGHT_CONTINUE_KEY)) if isinstance(payload, dict) else False


def set_night_continue_enabled(enabled: bool) -> bool:
    value = bool(enabled)
    atomic_write_json(_night_continue_path(), {_NIGHT_CONTINUE_KEY: value})
    return value


def is_night_quiet(now: datetime, *, config: bool | None = None) -> bool:
    """返回当前本地时间是否处于 00:00（含）至 07:00（不含）的静默窗。"""
    enabled = config if config is not None else get_night_continue_enabled()
    if enabled:
        return False
    return 0 <= now.hour < 7


def cleanup_maintain_enabled() -> bool:
    return _CLEANUP_MAINTAIN_ENABLED


def set_cleanup_maintain_enabled(enabled: bool) -> bool:
    global _CLEANUP_MAINTAIN_ENABLED
    _CLEANUP_MAINTAIN_ENABLED = bool(enabled)
    return _CLEANUP_MAINTAIN_ENABLED

JOB_POLL_INTERVAL_SEC = 2.0
JOB_POLL_TIMEOUT_SEC = 6 * 60 * 60

# 不同“自动远程大任务”之间的最小间隔（如刷新批次→自动参与）。
# 同一刷新批次内部的 refresh_all → refresh_watch → refresh_status 不受影响。
MIN_REMOTE_STAGE_GAP_SECONDS = 60

# 明确风控后的自动远程固定冷却（6 小时）。用户不可配置；冷却只恢复调度资格。
AUTO_REMOTE_RISK_COOLDOWN_SECONDS = 20 * 60

# 关注动态 cadence 的旧常量在上方保留，仅供旧只读消费者导入；实际调度使用明确小时集合。
