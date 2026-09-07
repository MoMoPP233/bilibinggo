"""拉取分类结果中的新活动详情。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.fetch_activity_info import ENRICHED_OUTPUT_PATH, fetch_activity_info, save_enriched


def main() -> int:
    # M5: writer CLI holds current Profile WriterLease before any Profile write;
    # busy -> fail-closed exit code 2 before the first write.
    from src.profile_writer_lease import cli_profile_writer_lease

    with cli_profile_writer_lease(
        "cli:fetch_activity_info"
    ):
        return _main_impl()


def _main_impl() -> int:
    try:
        result = fetch_activity_info()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1

    if result.new_count > 0:
        out_path = save_enriched(result)
    else:
        out_path = ENRICHED_OUTPUT_PATH

    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    print(
        f"\n详情拉取完成: 新入库 {result.new_count} 条，共 {result.total_count} 条"
        f"\n已写入: {out_path}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
