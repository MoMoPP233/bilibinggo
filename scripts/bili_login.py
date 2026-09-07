"""哔哩哔哩扫码登录 — 自动保存 Cookie 到当前 Profile。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.bilibili_login import BilibiliLoginError, COOKIE_PATH, login_with_qrcode


def main() -> int:
    # M5: writer CLI holds the current Profile WriterLease before any write/
    # Bilibili mutation; fail-closed exit code 2 when busy.
    from src.profile_writer_lease import cli_profile_writer_lease

    with cli_profile_writer_lease(
        "cli:bili_login"
    ):
        return _main_impl()


def _main_impl() -> int:
    try:
        cookie = login_with_qrcode()
    except BilibiliLoginError as exc:
        print(f"登录失败: {exc}", file=sys.stderr)
        return 1

    print(f"Cookie 已保存到: {COOKIE_PATH}")
    print(f"长度: {len(cookie)} 字符")
    print("现在可以运行: python scripts/check_ds1.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
