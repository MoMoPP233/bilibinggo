from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from src.app_logging import (
    DailyFileHandler,
    _cleanup_daily_logs,
    get_logger,
    reset_logging_for_tests,
    setup_logging,
)


def test_setup_logging_creates_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("src.app_logging.DATA_DIR", tmp_path)
    reset_logging_for_tests()
    path = setup_logging(console=False)
    assert path.parent == tmp_path / "logs"
    assert path.name.startswith("binggo-") and path.name.endswith(".log")
    assert path.exists()

    child = get_logger("test")
    child.info("hello from test")
    content = path.read_text(encoding="utf-8")
    assert "hello from test" in content
    assert "binggo.test" in content
    last = [line for line in content.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["msg"] == "hello from test"
    assert payload["logger"] == "binggo.test"
    reset_logging_for_tests()


def test_daily_handler_switches_date_and_retains_seven_days(tmp_path) -> None:
    handler = DailyFileHandler(tmp_path)
    handler.setFormatter(logging.Formatter("%(message)s"))
    today = datetime.now().astimezone().date()
    for offset, message in ((1, "today"), (2, "tomorrow")):
        record = logging.LogRecord("binggo.test", logging.INFO, __file__, 1, message, (), None)
        record.created = datetime.combine(today + timedelta(days=offset - 1), datetime.min.time()).timestamp()
        handler.emit(record)
    assert (tmp_path / f"binggo-{today.isoformat()}.log").exists()
    assert (tmp_path / f"binggo-{(today + timedelta(days=1)).isoformat()}.log").exists()

    old = today - timedelta(days=7)
    recent = today - timedelta(days=6)
    (tmp_path / f"binggo-{old.isoformat()}.log").write_text("old\n", encoding="utf-8")
    (tmp_path / f"binggo-{recent.isoformat()}.log").write_text("recent\n", encoding="utf-8")
    _cleanup_daily_logs(tmp_path, today=today)
    assert not (tmp_path / f"binggo-{old.isoformat()}.log").exists()
    assert (tmp_path / f"binggo-{recent.isoformat()}.log").exists()
    handler.close()


def test_profile_log_directories_are_isolated(tmp_path, monkeypatch) -> None:
    first = tmp_path / "profile-a"
    second = tmp_path / "profile-b"
    monkeypatch.setattr("src.app_logging.DATA_DIR", first)
    reset_logging_for_tests()
    setup_logging(console=False)
    get_logger("profile").info("only-a")
    reset_logging_for_tests()
    monkeypatch.setattr("src.app_logging.DATA_DIR", second)
    setup_logging(console=False)
    get_logger("profile").info("only-b")
    assert "only-a" in (first / "logs" / f"binggo-{datetime.now().astimezone().date().isoformat()}.log").read_text(encoding="utf-8")
    assert "only-b" in (second / "logs" / f"binggo-{datetime.now().astimezone().date().isoformat()}.log").read_text(encoding="utf-8")
    reset_logging_for_tests()
