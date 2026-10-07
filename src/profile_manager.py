"""Profile 创建、切换、删除与元数据管理。"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from src.data_paths import (
    ActiveProfileStateError,
    get_active_profile_path,
    get_atomic_path_lock,
    get_profile_dir,
    get_profile_metadata_path,
    get_profiles_dir,
    get_runtime_profile_id,
    get_selected_profile_id,
    initialize_data_layout,
    validate_profile_id,
)

_logger = logging.getLogger(__name__)

# .creating-* 孤儿安全 TTL：正常创建流程只需数秒，1 小时可防时钟偏差误删
CREATING_ORPHAN_TTL_SECONDS = 3600


def sweep_stale_creating_profiles(now: int | None = None) -> list[str]:
    """Opportunistic 清理崩溃遗留的 .creating-* 孤儿（无 thread/timer）。

    - 必须在 ProfileRegistryLease 保护下调用（create_profile / onboarding publish
      已持锁），与正在进行的创建流程天然互斥。
    - 只删除 mtime 超过 CREATING_ORPHAN_TTL_SECONDS 的目录；新鲜/正在创建的绝不删。
    - 单目录清理失败：记录并保留，不影响其它 Profile（fail-safe）。
    """
    now_ts = int(now if now is not None else time.time())
    removed: list[str] = []
    profiles_dir = get_profiles_dir()
    if not profiles_dir.is_dir():
        return removed
    for directory in profiles_dir.iterdir():
        if not directory.name.startswith(".creating-"):
            continue
        try:
            mtime = int(directory.stat().st_mtime)
        except OSError:
            continue
        if now_ts - mtime <= CREATING_ORPHAN_TTL_SECONDS:
            continue
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            _logger.error("清理 .creating-* 孤儿失败，保留 %s：%s", directory.name, exc)
            continue
        removed.append(directory.name)
    return removed


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """同目录唯一临时文件原子替换；同一 target 的并发写由共享 per-target 锁串行化。

    temp 使用 tempfile.mkstemp（同进程多线程不共用文件名），先关闭句柄再 os.replace，
    Windows 安全；失败时尽力清理自己的 temp 并保持旧 target 完整。
    锁与 src.data_paths 的原子写入共用同一把（get_atomic_path_lock，RLock 可重入）。
    """
    with get_atomic_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _sort_key(profile_id: str) -> tuple[int, int | str]:
    if profile_id == "default":
        return (0, 0)
    try:
        return (1, int(profile_id.removeprefix("account-")))
    except ValueError:
        return (2, profile_id)


def _profile_ids() -> list[str]:
    profiles_dir = get_profiles_dir()
    if not profiles_dir.is_dir():
        return []
    return sorted(
        (
            item.name
            for item in profiles_dir.iterdir()
            if item.is_dir()
            and (item.name == "default" or item.name.startswith("account-"))
            and _is_valid_id(item.name)
        ),
        key=_sort_key,
    )


def _is_valid_id(profile_id: str) -> bool:
    try:
        validate_profile_id(profile_id)
    except ValueError:
        return False
    return True


def get_profile_metadata(profile_id: str) -> dict[str, str]:
    value = validate_profile_id(profile_id)
    if not get_profile_dir(value).is_dir():
        raise ValueError(f"Profile 不存在：{value}")
    raw = _read_json(get_profile_metadata_path(value))
    return {
        "profile_id": value,
        "mid": str(raw.get("mid") or "").strip(),
        "nickname": str(raw.get("nickname") or "").strip(),
    }


def list_profiles() -> list[dict[str, str]]:
    # active_profile.json 损坏时本地列出仍然可用：不隐式选择、不改写指针，
    # 让 Web 选择页能继续展示 Profile 列表供用户明确重建选择。
    try:
        initialize_data_layout()
    except ActiveProfileStateError:
        pass
    return [get_profile_metadata(profile_id) for profile_id in _profile_ids()]


def create_profile() -> dict[str, str]:
    # 分配 account-N 属于全局资源：RegistryLease 内串行化 Web create 与 M7
    # onboarding publish（同一 OS 锁原语），避免两个进程同时分配同一 Profile ID。
    from src.profile_writer_lease import profile_registry_lease

    with profile_registry_lease(owner="profile:create"):
        return _create_profile_locked()


def _create_profile_locked() -> dict[str, str]:
    profiles_dir = get_profiles_dir()
    profiles_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale_creating_profiles()
    existing = _profile_ids()
    if not existing:
        initialize_data_layout()
        existing = _profile_ids()
        # initialize_data_layout 创建的首个 Profile 就是本次创建结果。
        return get_profile_metadata(existing[0])

    used = {
        int(value.removeprefix("account-"))
        for value in existing
        if value.startswith("account-") and value.removeprefix("account-").isdigit()
    }
    number = 1
    while number in used:
        number += 1
    profile_id = f"account-{number}"
    profile_dir = get_profile_dir(profile_id)
    profile_dir.mkdir(parents=False, exist_ok=False)
    metadata = {"profile_id": profile_id, "mid": "", "nickname": ""}
    _write_json(get_profile_metadata_path(profile_id), metadata)
    return metadata


def get_active_profile() -> dict[str, str]:
    initialize_data_layout()
    # “active” 是 active_profile.json 中的下次启动选择；当前进程绑定另由
    # data_paths.get_runtime_profile_id() 表示，二者在待重启期间可以不同。
    return get_profile_metadata(get_selected_profile_id())


def set_active_profile(profile_id: str) -> dict[str, str]:
    value = validate_profile_id(profile_id)
    metadata = get_profile_metadata(value)
    # 只持久化下次启动值；严禁修改 data_paths 的运行时快照或重置 engine。
    _write_json(get_active_profile_path(), {"profile_id": value})
    return metadata


def delete_profile(profile_id: str) -> None:
    value = validate_profile_id(profile_id)
    if value == get_runtime_profile_id():
        raise ValueError("不能删除当前正在使用的 Profile")
    if value == get_selected_profile_id():
        raise ValueError("不能删除下次启动将使用的 Profile")
    profile_dir = get_profile_dir(value)
    if not profile_dir.is_dir():
        raise ValueError(f"Profile 不存在：{value}")
    from src.runtime_instances import (
        RuntimeBusyError, acquire_runtime_lock, get_runtime_instance,
    )

    if get_runtime_instance(value):
        raise ValueError("账号正在运行，请先退出该账号。")
    try:
        runtime_lease = acquire_runtime_lock(value)
    except RuntimeBusyError as exc:
        raise ValueError("账号正在运行，请先退出该账号。") from exc
    try:
        # Reservation may have appeared between the first registry read and
        # lock acquisition. Never delete a Profile a supervisor is starting.
        if get_runtime_instance(value):
            raise ValueError("账号正在运行，请先退出该账号。")
        _delete_profile_contents(value, profile_dir)
    finally:
        runtime_lease.release()


def _delete_profile_contents(value: str, profile_dir: Path) -> None:

    # 目标 Profile 的跨进程 WriterLease：busy → 拒绝删除，绝不 rmtree 正在被写入的目录。
    from src.profile_writer_lease import (
        WRITER_LOCK_FILENAME,
        ProfileWriterLeaseBusy,
        profile_writer_lease,
    )

    profiles_dir = get_profiles_dir()
    tombstone = profiles_dir / (
        f".deleting-{value}-{os.getpid()}-{int(time.time() * 1000)}"
    )
    try:
        # 锁文件（binggo.writer.lock）位于 Profile 目录内部，Windows 不能删除/重命名
        # 含打开句柄的目录；因此持锁期间把除锁文件外的全部内容移动到隐藏 tombstone，
        # 释放锁（锁文件句柄关闭）后删除锁文件与空目录，再删除 tombstone。
        with profile_writer_lease(owner="profile:delete", profile_id=value):
            tombstone.mkdir()
            for child in profile_dir.iterdir():
                if child.name == WRITER_LOCK_FILENAME:
                    continue
                os.rename(str(child), str(tombstone / child.name))
    except ProfileWriterLeaseBusy as exc:
        raise ValueError("该账号正在执行任务，请稍后再试。") from exc
    try:
        (profile_dir / WRITER_LOCK_FILENAME).unlink(missing_ok=True)
        profile_dir.rmdir()
        shutil.rmtree(tombstone)
    except OSError as exc:  # noqa: BLE001 - 明确失败语义
        _logger.error("删除 Profile %s：内容已移出但目录清理残留失败：%s", value, exc)
        raise ValueError(
            f"Profile {value} 已停用但残留清理失败，残留目录：{tombstone.name}，请手动检查。"
        ) from exc


def update_profile_metadata(
    profile_id: str | None = None,
    *,
    mid: str | int | None = None,
    nickname: str | None = None,
) -> dict[str, str]:
    value = validate_profile_id(profile_id) if profile_id is not None else get_runtime_profile_id()
    path = get_profile_metadata_path(value)
    with get_atomic_path_lock(path):
        # read-modify-write：read + mutate + write 处于同一 per-target 临界区，
        # 并发更新不同字段不会互相丢失（锁内全部为本地 IO）。
        metadata = get_profile_metadata(value)
        if mid is not None:
            metadata["mid"] = str(mid).strip()
        if nickname is not None:
            metadata["nickname"] = str(nickname).strip()
        _write_json(path, metadata)
    return metadata


__all__ = [
    "create_profile",
    "delete_profile",
    "get_active_profile",
    "get_profile_metadata",
    "list_profiles",
    "set_active_profile",
    "sweep_stale_creating_profiles",
    "update_profile_metadata",
]
