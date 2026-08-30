"""Profile 创建、切换、删除与元数据管理。"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from src.data_paths import (
    get_active_profile_path,
    get_profile_dir,
    get_profile_metadata_path,
    get_profiles_dir,
    get_runtime_profile_id,
    get_selected_profile_id,
    initialize_data_layout,
    validate_profile_id,
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


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
    initialize_data_layout()
    return [get_profile_metadata(profile_id) for profile_id in _profile_ids()]


def create_profile() -> dict[str, str]:
    profiles_dir = get_profiles_dir()
    profiles_dir.mkdir(parents=True, exist_ok=True)
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
    shutil.rmtree(profile_dir)


def update_profile_metadata(
    profile_id: str | None = None,
    *,
    mid: str | int | None = None,
    nickname: str | None = None,
) -> dict[str, str]:
    value = validate_profile_id(profile_id) if profile_id is not None else get_runtime_profile_id()
    metadata = get_profile_metadata(value)
    if mid is not None:
        metadata["mid"] = str(mid).strip()
    if nickname is not None:
        metadata["nickname"] = str(nickname).strip()
    _write_json(get_profile_metadata_path(value), metadata)
    return metadata


__all__ = [
    "create_profile",
    "delete_profile",
    "get_active_profile",
    "get_profile_metadata",
    "list_profiles",
    "set_active_profile",
    "update_profile_metadata",
]
