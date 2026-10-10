"""Private onboarding child entry, shared by Python and the frozen launcher.

Never run this initializer in a live service: its isolated process binds only
the unpublished staging DB, without selecting a runtime or starting Jobs.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


def initialize_staging_database(directory: str) -> None:
    path = Path(directory)
    if not path.is_absolute() or path.is_symlink():
        raise ValueError("Profile 初始化需要绝对 staging 路径")
    path = path.resolve(strict=True)
    if (not path.is_dir() or path.parent.name != "profiles"
            or not re.fullmatch(r"\.creating-account-[1-9]\d*-[0-9a-f]{12}", path.name)):
        raise ValueError("仅允许初始化未发布的新 Profile staging 目录")
    db_file = path / "binggo.db"
    if db_file.exists() or db_file.is_symlink():
        raise ValueError("新 Profile 数据库已存在，拒绝覆盖")
    from src.db import engine, schema

    engine.db_path = lambda: db_file
    try:
        schema.init_db()
    finally:
        engine.reset_engine_for_tests()


def main(directory: str) -> int:
    try:
        initialize_staging_database(directory)
    except Exception as exc:
        if sys.stderr is not None:
            print(f"Profile DB initialization failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
