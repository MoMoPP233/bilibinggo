"""V92: daily retention maintenance stays local without weakening log writes."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from src import app_logging as logs


def _record(message: str, *, at: datetime | None = None) -> logging.LogRecord:
    record = logging.LogRecord("binggo.v92", logging.INFO, __file__, 1, message, (), None)
    if at is not None:
        record.created = at.timestamp()
    return record


def _handler(directory: Path) -> logs.DailyFileHandler:
    handler = logs.DailyFileHandler(directory)
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


def _count_cleanup(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    real_cleanup = logs._cleanup_daily_logs
    calls: list[Path] = []

    def cleanup(directory: Path, *, today=None) -> None:
        calls.append(directory)
        real_cleanup(directory, today=today)

    monkeypatch.setattr(logs, "_cleanup_daily_logs", cleanup)
    return calls


@pytest.mark.parametrize("count", [1, 100, 1000])
def test_same_day_writes_all_lines_but_scans_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int,
) -> None:
    calls = _count_cleanup(monkeypatch)
    real_glob = Path.glob
    glob_calls: list[Path] = []

    def glob(directory: Path, pattern: str):
        if directory == tmp_path and pattern == "binggo-????-??-??.log":
            glob_calls.append(directory)
        return real_glob(directory, pattern)

    monkeypatch.setattr(Path, "glob", glob)
    handler = _handler(tmp_path)
    try:
        for index in range(count):
            handler.emit(_record(f"line-{index}"))
    finally:
        handler.close()
    today = datetime.now().astimezone().date()
    lines = (tmp_path / f"binggo-{today}.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == count
    assert lines[0] == "line-0"
    assert lines[-1] == f"line-{count - 1}"
    assert calls == [tmp_path]
    assert glob_calls == [tmp_path]


def test_every_emit_still_flushes(tmp_path: Path) -> None:
    handler = _handler(tmp_path)
    handler.emit(_record("first"))
    stream = handler._stream
    assert stream is not None

    class FlushSpy:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.flush_count = 0

        def write(self, text: str):
            return self.wrapped.write(text)

        def flush(self):
            self.flush_count += 1
            return self.wrapped.flush()

        def close(self):
            return self.wrapped.close()

    spy = FlushSpy(stream)
    handler._stream = spy
    try:
        for index in range(100):
            handler.emit(_record(f"next-{index}"))
        assert spy.flush_count == 100
        assert len(handler._path.read_text(encoding="utf-8").splitlines()) == 101
    finally:
        handler.close()


def test_midnight_rotates_file_and_rechecks_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_zone = datetime.now().astimezone().tzinfo
    day = datetime.now().astimezone().date()
    before = datetime.combine(day, time(23, 59), tzinfo=local_zone)
    after = before + timedelta(minutes=1)

    class Clock(datetime):
        current = before

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    monkeypatch.setattr(logs, "datetime", Clock)
    calls = _count_cleanup(monkeypatch)
    handler = _handler(tmp_path)
    try:
        handler.emit(_record("before", at=before))
        handler.emit(_record("still-before", at=before))
        Clock.current = after
        handler.emit(_record("after", at=after))
    finally:
        handler.close()
    assert calls == [tmp_path, tmp_path]
    assert (tmp_path / f"binggo-{day}.log").read_text(encoding="utf-8").splitlines() == [
        "before", "still-before",
    ]
    assert (tmp_path / f"binggo-{day + timedelta(days=1)}.log").read_text(encoding="utf-8") == "after\n"


def test_new_handler_rechecks_retention(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_cleanup(monkeypatch)
    for message in ("first-process", "second-process"):
        handler = _handler(tmp_path)
        try:
            handler.emit(_record(message))
        finally:
            handler.close()
    assert calls == [tmp_path, tmp_path]


def test_retention_boundary_active_file_and_nonmatching_files(tmp_path: Path) -> None:
    today = datetime.now().astimezone().date()
    expired = tmp_path / f"binggo-{today - timedelta(days=7)}.log"
    boundary = tmp_path / f"binggo-{today - timedelta(days=6)}.log"
    unrelated = tmp_path / "other-2000-01-01.log"
    malformed = tmp_path / "binggo-2000-99-99.log"
    for path in (expired, boundary, unrelated, malformed):
        path.write_text(path.name, encoding="utf-8")
    handler = _handler(tmp_path)
    try:
        handler.emit(_record("active"))
        current = handler._path
        assert current is not None and current.exists()
        assert current.read_text(encoding="utf-8") == "active\n"
    finally:
        handler.close()
    assert not expired.exists()
    assert boundary.exists()
    assert unrelated.exists()
    assert malformed.exists()


def test_unlink_failure_does_not_block_writes_and_new_handler_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / f"binggo-{datetime.now().astimezone().date() - timedelta(days=7)}.log"
    old.write_text("old", encoding="utf-8")
    real_unlink = Path.unlink

    def locked(path: Path, *args, **kwargs):
        if path == old:
            raise PermissionError("test-only locked file")
        return real_unlink(path, *args, **kwargs)

    handler = _handler(tmp_path)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", locked)
        handler.emit(_record("survives-cleanup-failure"))
    handler.close()
    assert old.exists()
    active = tmp_path / f"binggo-{datetime.now().astimezone().date()}.log"
    assert active.read_text(encoding="utf-8") == "survives-cleanup-failure\n"
    restarted = _handler(tmp_path)
    try:
        restarted.emit(_record("after-restart"))
    finally:
        restarted.close()
    assert not old.exists()


def test_glob_failure_is_retried_and_logs_remain_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    old = tmp_path / f"binggo-{datetime.now().astimezone().date() - timedelta(days=7)}.log"
    old.write_text("old", encoding="utf-8")
    real_glob = Path.glob
    calls = 0

    def flaky(directory: Path, pattern: str):
        nonlocal calls
        if directory == tmp_path:
            calls += 1
            if calls == 1:
                raise OSError("test-only glob failure")
        return real_glob(directory, pattern)

    monkeypatch.setattr(Path, "glob", flaky)
    monkeypatch.setattr(logging, "raiseExceptions", False)
    handler = _handler(tmp_path)
    try:
        handler.emit(_record("first"))
        handler.emit(_record("second"))
    finally:
        handler.close()
    assert calls == 2
    assert not old.exists()
    active = tmp_path / f"binggo-{datetime.now().astimezone().date()}.log"
    assert active.read_text(encoding="utf-8").splitlines() == ["first", "second"]


def test_handlers_in_two_profiles_clean_only_their_own_logs(tmp_path: Path) -> None:
    today = datetime.now().astimezone().date()
    first, second = tmp_path / "account-1" / "logs", tmp_path / "account-2" / "logs"
    for directory in (first, second):
        directory.mkdir(parents=True)
    first_old = first / f"binggo-{today - timedelta(days=7)}.log"
    second_old = second / f"binggo-{today - timedelta(days=7)}.log"
    first_old.write_text("first-old", encoding="utf-8")
    second_old.write_text("second-old", encoding="utf-8")
    first_handler = _handler(first)
    second_handler = _handler(second)
    try:
        first_handler.emit(_record("account-1"))
        assert not first_old.exists()
        assert second_old.exists()
        second_handler.emit(_record("account-2"))
        assert not second_old.exists()
    finally:
        first_handler.close()
        second_handler.close()
    assert "account-1" in (first / f"binggo-{today}.log").read_text(encoding="utf-8")
    assert "account-2" in (second / f"binggo-{today}.log").read_text(encoding="utf-8")


def test_two_handlers_can_safely_cleanup_same_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _count_cleanup(monkeypatch)
    old = tmp_path / f"binggo-{datetime.now().astimezone().date() - timedelta(days=7)}.log"
    old.write_text("old", encoding="utf-8")
    first, second = _handler(tmp_path), _handler(tmp_path)
    try:
        first.emit(_record("first"))
        second.emit(_record("second"))
    finally:
        first.close()
        second.close()
    assert calls == [tmp_path, tmp_path]
    assert not old.exists()


def test_threaded_emits_use_one_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _count_cleanup(monkeypatch)
    handler = _handler(tmp_path)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda index: handler.emit(_record(f"thread-{index}")), range(100)))
    finally:
        handler.close()
    today = datetime.now().astimezone().date()
    assert len((tmp_path / f"binggo-{today}.log").read_text(encoding="utf-8").splitlines()) == 100
    assert calls == [tmp_path]
