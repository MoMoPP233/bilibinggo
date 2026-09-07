"""Profile 级跨进程 WriterLease（M5）。

目标：同一 Profile 的完整“写型业务 workflow”同一时间只有一个进程写入
（Web 写型 Job + 写型 CLI）。不是每条 SQL 一锁，而是整个 workflow 一锁。

- per-Profile 身份：canonical profile directory（resolve）派生；Profile A/B 互不干扰。
- 锁文件位置：各 Profile 自己的运行目录内 writer.lock（profile 目录已含 DB/cookie/json）。
- 真实互斥完全由 OS 文件锁（Windows msvcrt / 其它 fcntl）决定；锁文件存在但无人
  持有 OS 锁时允许正常获取。PID/owner/started_at 仅用于展示。
- fail-closed：获取失败 → ProfileWriterLeaseBusy（0 写、0 远程 mutation）。
- 不无限等待：默认单次尝试快速失败（可由 wait_seconds 指定一个很短的上限）。
- 进程崩溃/被 kill → OS 自动释放锁（不依赖手工清 stale 文件）。
- 同进程内再次获取同一 Profile：直接 busy（避免自身死锁；接入策略为最外层获取一次）。

底层 OS lock 纯实现直接复用自 bil-1-main src/writer_lock.py（Windows msvcrt.locking
LK_NBLCK / POSIX flock LOCK_EX|LOCK_NB、文件补 1 字节、acquire/release 原子语义），
仅把锁路径从“全局 data_dir”改为 per-Profile，并增加诊断字段。
"""
from __future__ import annotations

import os
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator

from src.data_paths import get_profile_dir

WRITER_LOCK_FILENAME = "binggo.writer.lock"
WRITER_LOCK_BUSY_MESSAGE = "当前 Profile 正在被另一个写入任务使用，请稍后重试。"
_owner_pid = os.getpid()

if os.name == "nt":  # pragma: no cover - 平台分支
    import msvcrt

    def _try_lock(handle: IO[str]) -> bool:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[str]) -> None:
        try:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass

else:  # pragma: no cover - 平台分支
    import fcntl

    def _try_lock(handle: IO[str]) -> bool:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(handle: IO[str]) -> None:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass


class ProfileWriterLeaseBusy(RuntimeError):
    """同一 Profile 已被另一进程（或本进程其它 writer）持有写锁。fail-closed。"""


class _ProfileWriterLease:
    """单把 per-Profile 写锁（OS 锁 handle 持真实 ownership）。"""

    def __init__(self, path: Path, *, owner: str = "") -> None:
        self.path = path
        self.owner = owner
        self._handle: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
        except OSError:
            return False  # fail-closed：目录/权限异常时不假定独占
        try:
            if handle.tell() == 0:
                handle.write("binggo\n")
                handle.flush()
        except OSError:
            pass
        if not _try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            _unlock(handle)
        finally:
            handle.close()


# 同进程并发 writer 保护：进程内已持有某 Profile 锁时，其它线程/嵌套获取直接 busy。
_registry_guard = threading.Lock()
_active: dict[str, _ProfileWriterLease] = {}


def writer_lease_path(profile_id: str | None = None) -> Path:
    """per-Profile 锁文件：位于该 Profile 的本地运行目录内。"""
    profile_dir = get_profile_dir(profile_id)
    return profile_dir / WRITER_LOCK_FILENAME


def _lock_key(profile_id: str | None = None) -> str:
    directory = get_profile_dir(profile_id)
    return os.path.normcase(str(directory.resolve(strict=False)))


def profile_writer_acquire(
    *,
    owner: str = "",
    profile_id: str | None = None,
    wait_seconds: float = 0.0,
) -> _ProfileWriterLease:
    """尝试获取当前 Profile 写锁；可指定极短 wait_seconds（默认 0 即快速失败）。

    返回已持有的 lease；失败抛 ProfileWriterLeaseBusy。
    """
    key = _lock_key(profile_id)
    deadline = time.monotonic() + max(0.0, float(wait_seconds or 0.0))
    while True:
        with _registry_guard:
            existing = _active.get(key)
            if existing is not None and existing.held:
                raise ProfileWriterLeaseBusy(WRITER_LOCK_BUSY_MESSAGE)
        lease = _ProfileWriterLease(writer_lease_path(profile_id), owner=owner)
        if lease.acquire():
            with _registry_guard:
                _active[key] = lease
            return lease
        if time.monotonic() >= deadline:
            break
        time.sleep(0.02)
    raise ProfileWriterLeaseBusy(WRITER_LOCK_BUSY_MESSAGE)


def profile_writer_release(lease: _ProfileWriterLease, *, profile_id: str | None = None) -> None:
    key = _lock_key(profile_id)
    with _registry_guard:
        if _active.get(key) is lease:
            _active.pop(key, None)
    lease.release()


@contextmanager
def profile_writer_lease(
    *,
    owner: str = "",
    profile_id: str | None = None,
    wait_seconds: float = 0.0,
) -> Iterator[_ProfileWriterLease]:
    """写型 workflow 最外层入口：获取 → yield → finally 释放。"""
    lease = profile_writer_acquire(owner=owner, profile_id=profile_id, wait_seconds=wait_seconds)
    try:
        yield lease
    finally:
        profile_writer_release(lease, profile_id=profile_id)


@contextmanager
def cli_profile_writer_lease(owner: str, *, profile_id: str | None = None) -> Iterator[None]:
    """CLI 专用：失败打印清晰中文错误并以退出码 2 结束，绝不无锁继续。"""
    try:
        with profile_writer_lease(owner=owner, profile_id=profile_id) as lease:
            yield lease
    except ProfileWriterLeaseBusy:
        print(WRITER_LOCK_BUSY_MESSAGE, file=sys.stderr)
        raise SystemExit(2)
