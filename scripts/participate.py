"""参与活动：互动/转发执行五项操作，预约执行关注 + 预约。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.app_paths import ensure_user_dirs
from src.fetch_activity_info import ENRICHED_OUTPUT_PATH
from src.participation import participate_activity
from src.sources.common import load_previous_output

SUPPORTED_TYPES = {"互动抽奖", "转发抽奖", "预约抽奖"}


def _lookup_lottery_type(dynamic_id: str) -> str:
    payload = load_previous_output(ENRICHED_OUTPUT_PATH)
    if not payload:
        raise RuntimeError(f"未找到活动库: {ENRICHED_OUTPUT_PATH}")
    for item in payload.get("activities") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("dynamic_id") or "") != dynamic_id:
            continue
        lottery_type = str(item.get("lottery_type") or "")
        if lottery_type in SUPPORTED_TYPES:
            return lottery_type
    raise RuntimeError(
        f"未找到活动 {dynamic_id} 的类型信息，请先运行一键更新或维护脚本补全活动库"
    )


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, OSError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="参与抽奖活动")
    parser.add_argument("dynamic_id", help="动态 ID")
    args = parser.parse_args()

    try:
        # CLI 可能是升级后的首个入口，先完成当前 Profile 的 schema 检查。
        ensure_user_dirs()
        lottery_type = _lookup_lottery_type(args.dynamic_id)
    except RuntimeError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    result = participate_activity(
        dynamic_id=args.dynamic_id,
        lottery_type=lottery_type,
        dry_run=False,
        persist=True,
        preflight=True,
    )

    payload = result.to_dict()
    print(json.dumps(payload, ensure_ascii=False))
    dedup_skip = (
        payload.get("status") == "skipped"
        and payload.get("skip_reason") in {
            "already_joined", "participation_busy", "repost_pending", "repost_unknown",
            "repost_suspected", "platform_joined",
        }
        and not payload.get("actions")
    )
    return 0 if result.status == "joined" or dedup_skip else 1


if __name__ == "__main__":
    raise SystemExit(main())
