"""定时点击调度器配置。"""

from __future__ import annotations

# V26 调度频率（单一事实来源）。
# refresh_all：00:00/06:00/12:00/18:00（4 次/天）。
REFRESH_HOURS = frozenset({0, 6, 12, 18})
# V38：上一轮结束后独立等待 240～420 秒，不再使用固定分钟槽。
TRIPLE_DELAY_MIN_SECONDS = 240
TRIPLE_DELAY_MAX_SECONDS = 420
# 仅用于 UI/状态展示口径；真实执行 cadence 由 slot 决定（每天 04:05 一次）。
CLEANUP_MAINTAIN_INTERVAL_HOURS = 24
# 中奖深检：沿用 CN_TZ，每天 23:55 一次。
WINNER_SCAN_HOURS = frozenset({23})
WINNER_SCAN_MINUTE = 55
# 清理维护：每天 04:05 一次；V58 预算续跑不是固定 slot。
CLEANUP_HOURS = frozenset({4})
CLEANUP_MINUTE = 5

ALLOWED_CLICK_ACTIONS = frozenset(
    {
        "refresh_all",
        "refresh_watch",
        "refresh_status",
        "participate_triple",
        "cleanup_maintenance_cycle",
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
# The scheduler never consults this value; fixed cleanup is always 04:05.
_CLEANUP_MAINTAIN_ENABLED = False


def cleanup_maintain_enabled() -> bool:
    return _CLEANUP_MAINTAIN_ENABLED


def set_cleanup_maintain_enabled(enabled: bool) -> bool:
    global _CLEANUP_MAINTAIN_ENABLED
    _CLEANUP_MAINTAIN_ENABLED = bool(enabled)
    return _CLEANUP_MAINTAIN_ENABLED

JOB_POLL_INTERVAL_SEC = 2.0
JOB_POLL_TIMEOUT_SEC = 6 * 60 * 60

# 不同“自动远程大任务”之间的最小间隔（如刷新批次→cleanup/自动参与）。
# 同一刷新批次内部的 refresh_all → refresh_watch → refresh_status 不受影响。
MIN_REMOTE_STAGE_GAP_SECONDS = 60

# 明确风控后的自动远程固定冷却（6 小时）。用户不可配置；冷却只恢复调度资格。
AUTO_REMOTE_RISK_COOLDOWN_SECONDS = 20 * 60

# 关注动态补漏 cadence：每 6 小时一次，落在 hour%6==1 的 :03（01:03/07:03/13:03/19:03），
# 与公共刷新及 cleanup 分钟错开；随机参与仍让专用分钟优先。
FOLLOWING_FEED_SCAN_HOUR_INTERVAL = 6
FOLLOWING_FEED_SCAN_HOUR_OFFSET = 1
FOLLOWING_FEED_SCAN_MINUTE = 3
