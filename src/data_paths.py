"""Binggo 数据根与 Profile 路径的唯一权威入口。"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

DATA_ROOT_ENV = "BINGGO_DATA_ROOT"
LEGACY_HOME_ENV = "BINGGO_HOME"
LEGACY_DATA_ROOT_ENV = "BINGGO_LEGACY_DATA_ROOT"
_LOCATOR_ENV = "BINGGO_DATA_ROOT_LOCATOR"
_PROFILE_ID_RE = re.compile(r"^(?:default|account-[1-9][0-9]*)$")

_runtime_profile_id: str | None = None
_runtime_data_root: Path | None = None


class DataRootNotConfiguredError(RuntimeError):
    """安装版尚未选择数据目录。"""


def _absolute_path(value: str | os.PathLike[str]) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return path.resolve(strict=False)


def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _portable_root() -> Path:
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin" and len(executable.parts) >= 3:
        if executable.parts[-2] == "MacOS" and executable.parts[-3] == "Contents":
            return executable.parents[3]
    return executable.parent


def _portable_enabled() -> bool:
    return os.environ.get("BINGGO_PORTABLE", "").strip().lower() in {"1", "true", "yes"}


def data_root_locator_path() -> Path:
    """返回仅保存 DATA_ROOT 指针的小型配置文件路径。"""
    override = os.environ.get(_LOCATOR_ENV, "").strip()
    if override:
        return _absolute_path(override)
    appdata = os.environ.get("APPDATA", "").strip()
    if appdata:
        return _absolute_path(appdata) / "Binggo" / "data_root.json"
    return Path.home().resolve() / ".config" / "Binggo" / "data_root.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
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


def _read_persisted_data_root() -> Path | None:
    value = str(_read_json(data_root_locator_path()).get("data_root") or "").strip()
    return _absolute_path(value) if value else None


def persist_data_root(path: str | os.PathLike[str]) -> Path:
    root = _absolute_path(path)
    _write_json_atomic(data_root_locator_path(), {"data_root": str(root)})
    return root


def get_data_root() -> Path:
    """解析统一数据根；新环境变量优先，BINGGO_HOME 仅作兼容别名。"""
    override = os.environ.get(DATA_ROOT_ENV, "").strip()
    if override:
        return _absolute_path(override)

    legacy_override = os.environ.get(LEGACY_HOME_ENV, "").strip()
    if legacy_override:
        return _absolute_path(legacy_override)

    if _portable_enabled():
        return _portable_root()

    persisted = _read_persisted_data_root()
    if persisted is not None:
        return persisted

    if not _is_frozen():
        # 源码入口很多，统一在路径层完成首次选择，避免某个 CLI 绕过 launcher
        # 后把数据库或 Cookie 静默写进仓库目录。
        return ensure_data_root_selected()

    raise DataRootNotConfiguredError("尚未选择 Binggo 数据目录")


def _choose_data_root() -> Path | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:  # pragma: no cover - 取决于本机 Python 发行版
        raise DataRootNotConfiguredError(
            "无法打开数据目录选择窗口，请先设置 BINGGO_DATA_ROOT"
        ) from exc

    window = tk.Tk()
    window.withdraw()
    try:
        window.attributes("-topmost", True)
    except tk.TclError:
        pass
    try:
        selected = filedialog.askdirectory(
            parent=window,
            title="请选择 Binggo 数据目录（可选择 D 盘或其他磁盘）",
            mustexist=False,
        )
    finally:
        window.destroy()
    return _absolute_path(selected) if selected else None


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _ensure_writable_directory(root: Path) -> None:
    """确认数据根可创建且可写，不把失效盘符或只读目录持久化。"""
    probe: Path | None = None
    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=".binggo-write-probe-",
            suffix=".tmp",
            dir=root,
            delete=False,
        ) as handle:
            handle.write(b"binggo")
            probe = Path(handle.name)
        probe.unlink()
    except OSError as exc:
        if probe is not None:
            try:
                probe.unlink(missing_ok=True)
            except OSError:
                pass
        raise DataRootNotConfiguredError(
            f"数据目录不可用或不可写：{root}\n{exc}"
        ) from exc


def ensure_data_root_selected(
    *, chooser: Callable[[], Path | None] | None = None
) -> Path:
    """在任何业务路径导入前确定 DATA_ROOT；安装版首次启动会弹目录选择。"""
    configured = os.environ.get(DATA_ROOT_ENV, "").strip()
    legacy = os.environ.get(LEGACY_HOME_ENV, "").strip()
    persisted = _read_persisted_data_root()
    if configured or legacy or _portable_enabled() or persisted is not None:
        root = get_data_root()
    else:
        root = (chooser or _choose_data_root)()
        if root is None:
            raise DataRootNotConfiguredError("未选择数据目录，Binggo 已取消启动")
        if _is_frozen() and _is_inside(root, Path(sys.executable).resolve().parent):
            raise DataRootNotConfiguredError("数据目录不能位于 Binggo 安装目录内")
        _ensure_writable_directory(root)
        try:
            persist_data_root(root)
        except OSError as exc:
            raise DataRootNotConfiguredError(
                f"无法保存数据目录选择：{data_root_locator_path()}\n{exc}"
            ) from exc

    _ensure_writable_directory(root)
    # 父/子进程以及后续动态读取统一使用同一个已解析绝对路径。
    os.environ[DATA_ROOT_ENV] = str(root)
    return root


def validate_profile_id(profile_id: str) -> str:
    value = str(profile_id or "").strip()
    if not _PROFILE_ID_RE.fullmatch(value):
        raise ValueError("Profile ID 无效")
    return value


def get_profiles_dir() -> Path:
    return get_data_root() / "profiles"


def get_shared_dir() -> Path:
    return get_data_root() / "shared"


def get_active_profile_path() -> Path:
    return get_data_root() / "active_profile.json"


def _profile_sort_key(profile_id: str) -> tuple[int, int | str]:
    if profile_id == "default":
        return (0, 0)
    try:
        return (1, int(profile_id.removeprefix("account-")))
    except ValueError:
        return (2, profile_id)


def _existing_profile_ids() -> list[str]:
    profiles = get_profiles_dir()
    if not profiles.is_dir():
        return []
    values = [
        item.name
        for item in profiles.iterdir()
        if item.is_dir() and _PROFILE_ID_RE.fullmatch(item.name)
    ]
    return sorted(values, key=_profile_sort_key)


def get_selected_profile_id() -> str:
    """读取磁盘上的下次启动 Profile；不改变本进程运行时绑定。"""
    raw = str(_read_json(get_active_profile_path()).get("profile_id") or "").strip()
    try:
        profile_id = validate_profile_id(raw)
    except ValueError:
        profile_id = ""
    if profile_id and (get_profiles_dir() / profile_id).is_dir():
        return profile_id

    existing = _existing_profile_ids()
    if existing:
        return existing[0]
    if _legacy_data_exists():
        return "default"
    return "account-1"


def get_active_profile_id() -> str:
    """兼容名称：返回 active_profile.json 指向的下次启动 Profile。"""
    return get_selected_profile_id()


def get_runtime_profile_id() -> str:
    """返回进程启动时冻结的 Profile，切换指针后仍保持不变。"""
    global _runtime_profile_id, _runtime_data_root
    root = get_data_root().resolve(strict=False)
    if _runtime_profile_id is None:
        _runtime_profile_id = get_selected_profile_id()
        _runtime_data_root = root
    elif _runtime_data_root != root:
        raise RuntimeError("BINGGO_DATA_ROOT 在进程运行期间发生变化，请重新启动 Binggo")
    return _runtime_profile_id


def get_profile_dir(profile_id: str | None = None) -> Path:
    if profile_id is None:
        # 任意业务 CLI 都可能先通过 app_paths/db.engine 取得当前目录。
        # 必须在返回可写路径前完成一次性迁移，避免空库先创建后遮蔽旧库。
        initialize_data_layout()
        value = get_runtime_profile_id()
    else:
        value = validate_profile_id(profile_id)
    return get_profiles_dir() / value


def get_database_path(profile_id: str | None = None) -> Path:
    return get_profile_dir(profile_id) / "binggo.db"


def get_cookie_path(profile_id: str | None = None) -> Path:
    return get_profile_dir(profile_id) / "cookies.txt"


def get_llm_env_path() -> Path:
    return get_shared_dir() / "llm.env"


def get_profile_metadata_path(profile_id: str | None = None) -> Path:
    return get_profile_dir(profile_id) / "profile.json"


def _legacy_roots() -> list[Path]:
    roots: list[Path] = [get_data_root()]
    explicit_legacy = os.environ.get(LEGACY_DATA_ROOT_ENV, "").strip()
    if explicit_legacy:
        roots.append(_absolute_path(explicit_legacy))
    legacy = os.environ.get(LEGACY_HOME_ENV, "").strip()
    if legacy:
        roots.append(_absolute_path(legacy))
    if not _is_frozen() and not explicit_legacy:
        # 任意源码 CLI 都可能成为升级后的第一次入口。集中加入仓库根，
        # 避免只有 launcher/run_dashboard 能发现旧 data/ 与 config/。
        roots.append(Path(__file__).resolve().parents[1])

    unique: list[Path] = []
    seen: set[str] = set()
    for item in roots:
        key = os.path.normcase(str(item.resolve(strict=False)))
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _first_file(candidates: list[Path]) -> Path | None:
    return next((item for item in candidates if item.is_file()), None)


def _legacy_files() -> tuple[Path | None, Path | None, Path | None]:
    account_candidates: list[tuple[Path | None, Path | None]] = []
    llm_candidates: list[Path] = []
    for root in _legacy_roots():
        root_database = _first_file(
            [root / "data" / "binggo.db", root / "binggo.db"]
        )
        root_cookie = _first_file(
            [
                root / "data" / "cookies.txt",
                root / "config" / "cookies.txt",
                root / "cookies.txt",
            ]
        )
        # DB 与 Cookie 是账号绑定数据：只能从同一个 legacy root 迁移，
        # 绝不能把两个候选目录里的不同账号拼成一个 Profile。
        if root_database is not None or root_cookie is not None:
            account_candidates.append((root_database, root_cookie))
        llm_candidates.extend((root / "config" / "llm.env", root / "llm.env"))

    # 优先选择同一 root 内完整的 DB+Cookie；没有完整候选时才采用首个
    # partial root。两种情况都禁止跨 root 拼接账号数据。
    selected_account = next(
        (
            candidate
            for candidate in account_candidates
            if candidate[0] is not None and candidate[1] is not None
        ),
        account_candidates[0] if account_candidates else (None, None),
    )
    database, cookie = selected_account
    return database, cookie, _first_file(llm_candidates)


def _legacy_data_exists() -> bool:
    database, cookie, _llm = _legacy_files()
    return database is not None or cookie is not None


def _copy_sqlite_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    try:
        source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
        src_conn = sqlite3.connect(source_uri, uri=True)
        try:
            source_check = src_conn.execute("PRAGMA quick_check").fetchone()
            if not source_check or str(source_check[0]).lower() != "ok":
                raise sqlite3.DatabaseError(f"旧数据库完整性检查失败：{source_check}")
            dst_conn = sqlite3.connect(destination)
            try:
                src_conn.backup(dst_conn)
                destination_check = dst_conn.execute("PRAGMA quick_check").fetchone()
                if not destination_check or str(destination_check[0]).lower() != "ok":
                    raise sqlite3.DatabaseError(
                        f"迁移数据库完整性检查失败：{destination_check}"
                    )
            finally:
                dst_conn.close()
        finally:
            src_conn.close()
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise RuntimeError(f"旧数据库无法安全迁移：{source}\n{exc}") from exc


def _copy_file_atomic(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        return
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def _cookie_mid(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = re.search(r"(?:^|;\s*)DedeUserID=([^;\s]+)", text)
    return match.group(1).strip() if match else ""


def initialize_data_layout() -> str:
    """创建首个 Profile，并仅在 profiles 为空时复制旧 DB/Cookie。"""
    root = get_data_root()
    shared = get_shared_dir()
    profiles = get_profiles_dir()
    shared.mkdir(parents=True, exist_ok=True)
    profiles.mkdir(parents=True, exist_ok=True)

    database, cookie, llm = _legacy_files()
    existing = _existing_profile_ids()
    if not existing:
        initial_id = "default" if database is not None or cookie is not None else "account-1"
        profile_dir = profiles / initial_id
        staging = Path(tempfile.mkdtemp(prefix=f".{initial_id}.migrating-", dir=profiles))
        try:
            if database is not None:
                _copy_sqlite_database(database, staging / "binggo.db")
            if cookie is not None:
                shutil.copy2(cookie, staging / "cookies.txt")
            _write_json_atomic(
                staging / "profile.json",
                {
                    "profile_id": initial_id,
                    "mid": _cookie_mid(cookie),
                    "nickname": "",
                },
            )
            # LLM 是共享配置，但也只在首次初始化时迁移。先于 Profile
            # 目录提交，保证 Profile 一旦可见，共享迁移已经完成。
            if llm is not None and not get_llm_env_path().exists():
                _copy_file_atomic(llm, get_llm_env_path())
            # Windows 的 os.replace 不保证可移动目录；目标此时必须不存在。
            staging.rename(profile_dir)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        existing = [initial_id]
    else:
        for profile_id in existing:
            profile_dir = profiles / profile_id
            metadata = profiles / profile_id / "profile.json"
            if not metadata.exists():
                _write_json_atomic(
                    metadata,
                    {
                        "profile_id": profile_id,
                        "mid": _cookie_mid(profile_dir / "cookies.txt"),
                        "nickname": "",
                    },
                )

    selected = get_selected_profile_id()
    if selected not in existing:
        selected = existing[0]
    active_path = get_active_profile_path()
    active_payload = _read_json(active_path)
    if active_payload.get("profile_id") != selected:
        _write_json_atomic(active_path, {"profile_id": selected})

    # 运行时快照若已在导入期确定，必须与迁移的确定性初始 ID 一致。
    runtime = get_runtime_profile_id()
    if runtime not in existing:
        raise RuntimeError("运行时 Profile 不存在，请重新启动 Binggo")
    return runtime


def reset_runtime_profile_for_tests() -> None:
    global _runtime_profile_id, _runtime_data_root
    _runtime_profile_id = None
    _runtime_data_root = None


__all__ = [
    "DATA_ROOT_ENV",
    "LEGACY_DATA_ROOT_ENV",
    "DataRootNotConfiguredError",
    "data_root_locator_path",
    "ensure_data_root_selected",
    "get_active_profile_id",
    "get_active_profile_path",
    "get_cookie_path",
    "get_data_root",
    "get_database_path",
    "get_llm_env_path",
    "get_profile_dir",
    "get_profile_metadata_path",
    "get_profiles_dir",
    "get_runtime_profile_id",
    "get_selected_profile_id",
    "get_shared_dir",
    "initialize_data_layout",
    "persist_data_root",
    "reset_runtime_profile_for_tests",
    "validate_profile_id",
]
