"""从活动转发列表中发现常参与抽奖的用户，导出当前 Profile 的 watch_users.json。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bilibili_client import BilibiliClient
from src.sources.common import load_previous_output, normalize_activity_id
from src import app_paths
from src.platform_risk import risk_code_of
from src.user_data_io import atomic_write_json
from src.watch_feed import SPACE_FEED_URL, extract_forward_orig_id

REACTION_URL = "https://api.bilibili.com/x/polymer/web-dynamic/v1/detail/reaction"


DEFAULT_LIMIT = 30
DEFAULT_LOTTERY_LIMIT = 35
DEFAULT_REACTION_PAGES = 3
FORWARD_RATIO_SAMPLE = 25


def collect_lottery_dynamic_ids(*, lottery_limit: int) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for path in (app_paths.data_dir() / "output" / f"ds{i}_latest.json" for i in (2, 3, 5, 6, 7)):
        payload = load_previous_output(path) or {}
        for url in payload.get("activity_links") or []:
            dynamic_id = normalize_activity_id(str(url))
            if not dynamic_id or dynamic_id in seen:
                continue
            seen.add(dynamic_id)
            ids.append(dynamic_id)
            if len(ids) >= lottery_limit:
                return ids
    return ids


def fetch_forward_users(
    client: BilibiliClient,
    dynamic_id: str,
    *,
    max_pages: int,
) -> list[dict]:
    referer = f"https://www.bilibili.com/opus/{dynamic_id}"
    offset = ""
    users: list[dict] = []
    for _ in range(max_pages):
        data = client.get_json(
            REACTION_URL,
            params={
                "id": dynamic_id,
                "action_type": 2,
                "offset": offset,
            },
            referer=referer,
        )
        payload = data.get("data") or {}
        items = payload.get("items") or []
        for item in items:
            try:
                mid = int(item.get("mid") or 0)
            except (TypeError, ValueError):
                continue
            if mid <= 0:
                continue
            users.append({"mid": mid, "name": str(item.get("name") or mid)})
        if not payload.get("has_more"):
            break
        offset = str(payload.get("offset") or "")
        if not offset:
            break
        time.sleep(0.12)
    return users


def measure_forward_ratio(client: BilibiliClient, mid: int) -> float:
    referer = f"https://space.bilibili.com/{mid}/dynamic"
    try:
        data = client.get_json(
            SPACE_FEED_URL,
            params={
                "host_mid": mid,
                "offset": "",
                "type": "all",
                "timezone_offset": -480,
                "platform": "web",
                "web_location": "333.1365",
            },
            referer=referer,
            wbi=True,
        )
    except Exception as exc:
        if risk_code_of(exc) is not None:
            raise
        return 0.0
    items = (data.get("data") or {}).get("items") or []
    sample = items[:FORWARD_RATIO_SAMPLE]
    if not sample:
        return 0.0
    forward_count = sum(1 for item in sample if extract_forward_orig_id(item))
    return round(forward_count / len(sample), 2)


def discover_watch_users(
    *,
    limit: int = DEFAULT_LIMIT,
    lottery_limit: int = DEFAULT_LOTTERY_LIMIT,
    reaction_pages: int = DEFAULT_REACTION_PAGES,
) -> dict:
    lottery_ids = collect_lottery_dynamic_ids(lottery_limit=lottery_limit)
    if not lottery_ids:
        raise RuntimeError("未找到活动动态 ID，请先运行一键更新活动链接")

    hit_counter: Counter[int] = Counter()
    name_by_mid: dict[int, str] = {}
    with BilibiliClient() as client:
        for index, dynamic_id in enumerate(lottery_ids, start=1):
            print(f"[{index}/{len(lottery_ids)}] 抓取转发用户: {dynamic_id}")
            users = fetch_forward_users(client, dynamic_id, max_pages=reaction_pages)
            for user in users:
                mid = int(user["mid"])
                hit_counter[mid] += 1
                if user.get("name"):
                    name_by_mid[mid] = str(user["name"])
            time.sleep(0.15)

        ranked = hit_counter.most_common(limit * 3)
        selected: list[dict] = []
        for mid, hit_count in ranked:
            if hit_count < 2:
                continue
            forward_ratio = measure_forward_ratio(client, mid)
            if forward_ratio < 0.5:
                continue
            score = hit_count * 2 + round(forward_ratio * 3)
            selected.append(
                {
                    "mid": mid,
                    "name": name_by_mid.get(mid, str(mid)),
                    "hit_count": hit_count,
                    "forward_ratio": forward_ratio,
                    "score": score,
                }
            )
            if len(selected) >= limit:
                break
            time.sleep(0.2)

    selected.sort(key=lambda item: (-item["score"], -item["hit_count"], item["mid"]))
    return {
        "discovered_at": int(time.time()),
        "scanned_lotteries": len(lottery_ids),
        "users": selected[:limit],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="发现常参与抽奖的用户并导出当前 Profile 的 watch_users.json")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="写入用户数量")
    parser.add_argument("--lottery-limit", type=int, default=DEFAULT_LOTTERY_LIMIT, help="扫描活动动态数量上限")
    parser.add_argument("--reaction-pages", type=int, default=DEFAULT_REACTION_PAGES, help="每个活动抓取的转发列表页数")
    parser.add_argument("--output", type=Path, default=None, help="输出 JSON 路径（默认当前 Profile）")
    args = parser.parse_args()

    payload = discover_watch_users(
        limit=args.limit, lottery_limit=args.lottery_limit, reaction_pages=args.reaction_pages,
    )
    path = args.output if args.output is not None else app_paths.config_dir() / "watch_users.json"
    atomic_write_json(path, payload)
    print(f"已写入 {len(payload['users'])} 个用户 → {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
