from __future__ import annotations

from pathlib import Path

from src.bilibili_client import BilibiliClient
from src.sources.common import CheckResult, save_result as write_result
from src.sources.recent_articles import check_recent_articles
from src.state_store import DATA_DIR

SOURCE_ID = "DS-2"
MID = 3546836235193146
OUTPUT_PATH = DATA_DIR / "output" / "ds2_latest.json"


def article_url(cv_id: int | str) -> str:
    return f"https://www.bilibili.com/read/cv{cv_id}"


def check_update(*, force: bool = False) -> CheckResult:
    with BilibiliClient() as client:
        return check_recent_articles(
            client, source_id=SOURCE_ID, mid=MID, output_path=OUTPUT_PATH,
            kind="cv", force=force,
        )


def save_result(result: CheckResult) -> Path | None:
    return write_result(OUTPUT_PATH, result)
