"""M7：扫码登录新账号 → 自动创建/复用独立 Profile（staging onboarding commit）。

设计：
- 扫码阶段与正式 Profile 完全隔离：只产生内存/staging Cookie，绝不写当前 Profile。
- UID 必须来自本次 staged Cookie（DedeUserID）；拿不到 UID → onboarding 失败。
- commit 时进入极窄 ProfileRegistryLease：UID 二次查重 → 分配 account-N →
  `.creating-*` 初始化（profile.json/cookies/DB）→ durable 后 atomic publish →
  原子写 active pointer → 释放 registry（restart 由上层现有机制触发）。
- 已有 UID：RegistryLease 内确认 → 释放 registry → 获取该 ProfileWriterLease →
  安全更新 Cookie/profile metadata（busy → 0 写入 fail-closed）→ 写 pointer。
- V18 保护：active pointer invalid/unreadable/dangling 时 onboarding fail-closed
  （不自动覆盖/回退/降级为第一个 Profile）；missing 视为用户显式选择的合法前置状态。
- 锁顺序恒定 RegistryLease → ProfileWriterLease；绝不反向。
- 失败语义：rename 前失败 → 无正式半成品（.creating 不进入列表/忽略）；
  rename 成功但 pointer 写失败 → 保留新 Profile + 旧 pointer，返回 created_not_activated。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from src.app_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)

_logger = get_logger("login_onboarding")

from src.data_paths import (
    get_active_profile_path,
    get_atomic_path_lock,
    get_data_root,
    get_profile_dir,
    get_profile_metadata_path,
    get_profiles_dir,
    read_active_profile_pointer_state,
)
from src.profile_manager import get_profile_metadata, sweep_stale_creating_profiles
from src.profile_writer_lease import (
    ProfileWriterLeaseBusy,
    profile_registry_lease,
    profile_writer_lease,
)
from src.secure_files import write_text_secret

_ACCOUNT_ID_RE = re.compile(r"^account-([1-9][0-9]*)$")
_UID_RE = re.compile(r"(?:^|;\s*)DedeUserID=([^;\s]+)")


class OnboardingError(RuntimeError):
    """onboarding fail-closed（附稳定错误代码）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def uid_from_cookie(cookie_str: str) -> str:
    text = str(cookie_str or "")
    match = _UID_RE.search(text)
    if not match:
        raise OnboardingError("uid_unavailable", "无法从 staged Cookie 可靠获取 UID")
    uid = match.group(1).strip()
    if not uid.isdigit():
        raise OnboardingError("uid_unavailable", "staged Cookie UID 非法")
    return uid


def _pointer_state() -> dict[str, Any]:
    return read_active_profile_pointer_state()


def _require_pointer_recoverable() -> str | None:
    """V18 fail-closed：invalid/unreadable/dangling 时禁止自动覆盖 pointer。"""
    state = _pointer_state()
    status = str(state.get("state_status") or "")
    if status in ("invalid", "unreadable", "dangling"):
        raise OnboardingError(
            "pointer_fail_closed",
            "active_profile 状态异常，请先手动恢复/选择目标账号后再扫码添加",
        )
    return state.get("profile_id")


def _existing_profile_for_uid(uid: str) -> str | None:
    profiles_dir = get_profiles_dir()
    if not profiles_dir.is_dir():
        return None
    for directory in profiles_dir.iterdir():
        if not directory.is_dir():
            continue
        name = directory.name
        if not _ACCOUNT_ID_RE.match(name) or name.startswith(".creating-"):
            continue
        metadata = directory / "profile.json"
        try:
            raw = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(raw.get("mid") or "") == str(uid):
            return name
    return None


def _next_account_number() -> int:
    profiles_dir = get_profiles_dir()
    used = {0}
    if profiles_dir.is_dir():
        for directory in profiles_dir.iterdir():
            match = _ACCOUNT_ID_RE.match(directory.name)
            if match:
                used.add(int(match.group(1)))
    number = 1
    while number in used:
        number += 1
    return number


def _atomic_write_text(path: Path, content: str) -> None:
    with get_atomic_path_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise


def _write_profile_metadata(profile_dir: Path, *, profile_id: str, mid: str, nickname: str) -> None:
    _atomic_write_text(
        profile_dir / "profile.json",
        json.dumps(
            {"profile_id": profile_id, "mid": str(mid), "nickname": str(nickname)},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def _init_profile_db(profile_dir: Path) -> None:
    """在独立子进程中为指定 Profile 目录初始化 DB（不触碰当前 runtime 引擎/状态）。"""
    root = Path(__file__).resolve().parents[1]
    db_file = (profile_dir / "binggo.db").resolve()
    code = (
        "import sys; sys.path.insert(0, r'%s')\n"
        "from pathlib import Path\n"
        "from src.db import engine as eng\n"
        "eng.db_path = lambda: Path(r'%s')\n"
        "from src.db import schema as schema_mod\n"
        "schema_mod.init_db()\n"
    ) % (str(root), str(db_file))
    env = dict(os.environ)
    env.pop("BINGGO_HOME", None)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(root),
        timeout=120,
    )
    if result.returncode != 0:
        raise OnboardingError(
            "profile_init_failed",
            f"新 Profile 数据库初始化失败（{result.stderr.strip()[-200:] or '未知错误'}）",
        )


def _update_active_pointer(profile_id: str) -> None:
    _atomic_write_text(
        get_active_profile_path(),
        json.dumps({"profile_id": profile_id}, ensure_ascii=False, indent=2) + "\n",
    )


def _publish_new_profile(*, uid: str, nickname: str, cookie_str: str, attempt_id: str) -> str:
    profiles_dir = get_profiles_dir()
    profiles_dir.mkdir(parents=True, exist_ok=True)
    sweep_stale_creating_profiles()
    number = _next_account_number()
    profile_id = f"account-{number}"
    staging = profiles_dir / f".creating-{profile_id}-{attempt_id}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        _write_profile_metadata(staging, profile_id=profile_id, mid=uid, nickname=nickname)
        cookie_path = staging / "cookies.txt"
        write_text_secret(cookie_path, cookie_str)
        _init_profile_db(staging)
        # durable 目录内容后 atomic publish（Windows: 目标必须不存在）
        if (profiles_dir / profile_id).exists():
            raise OnboardingError("profile_exists", f"Profile 已存在：{profile_id}")
        staging.rename(profiles_dir / profile_id)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return profile_id


def _save_cookie_into_existing_profile(profile_id: str, *, uid: str, nickname: str, cookie_str: str) -> None:
    profile_dir = get_profile_dir(profile_id)
    if not profile_dir.is_dir():
        raise OnboardingError("profile_missing", f"目标 Profile 不存在：{profile_id}")
    with profile_writer_lease(profile_id=profile_id, owner="onboarding:relogin"):
        # 双保险：WriterLease 内再次核对 UID 归属
        current = _existing_profile_for_uid(uid)
        if current != profile_id:
            raise OnboardingError("uid_mismatch", "Profile 与 UID 归属不一致，已停止")
        write_text_secret(profile_dir / "cookies.txt", cookie_str)
        _write_profile_metadata(profile_dir, profile_id=profile_id, mid=uid, nickname=nickname)


def commit_staged_login(
    *,
    cookie_str: str,
    uid: str | None = None,
    nickname: str = "",
) -> dict[str, Any]:
    """扫码成功拿到 staged Cookie 后的 commit（不进行任何扫码等待）。"""
    try:
        return _commit_staged_login_impl(
            cookie_str=cookie_str, uid=uid, nickname=nickname
        )
    except ProfileWriterLeaseBusy as exc:
        raise OnboardingError("writer_busy", str(exc)) from exc


def _commit_staged_login_impl(
    *,
    cookie_str: str,
    uid: str | None = None,
    nickname: str = "",
) -> dict[str, Any]:
    _require_pointer_recoverable()
    cookie_uid = uid_from_cookie(cookie_str)
    resolved_uid = str(uid or "").strip() or cookie_uid
    if resolved_uid != cookie_uid:
        raise OnboardingError("uid_mismatch", "UID 与 staged Cookie 不一致（服务端以 Cookie 为准）")

    if not str(resolved_uid or "").isdigit():
        raise OnboardingError("uid_unavailable", "UID 非法")
    attempt_id = uuid.uuid4().hex[:12]

    with profile_registry_lease(owner="onboarding"):
        existing = _existing_profile_for_uid(resolved_uid)
        if existing is not None:
            # 已存在：registry 内确认目标后释放，再在 WriterLease 下安全写 Cookie。
            pass
        else:
            profile_id = _publish_new_profile(
                uid=str(resolved_uid), nickname=str(nickname or ""),
                cookie_str=cookie_str, attempt_id=attempt_id,
            )
            try:
                _update_active_pointer(profile_id)
            except Exception as exc:  # noqa: BLE001
                # rename 已成功但 pointer 失败：保留新 Profile、旧 pointer 不变。
                raise OnboardingError(
                    "profile_created_not_activated",
                    f"新 Profile {profile_id} 已创建但激活指针更新失败（{exc}），"
                    "请稍后手动选择该账号后重启",
                ) from exc
            return {
                "profile_id": profile_id,
                "uid": str(resolved_uid),
                "display_name": str(nickname or ""),
                "created_new": True,
                "reused_existing": False,
                "restart_required": True,
            }

    # existing UID 重登录：registry 已释放
    if existing is not None:
        _save_cookie_into_existing_profile(
            existing, uid=str(resolved_uid), nickname=str(nickname or ""), cookie_str=cookie_str
        )
        # pointer 指向已有 Profile（短 registry 临界区）
        with profile_registry_lease(owner="onboarding:activate"):
            _update_active_pointer(existing)
        return {
            "profile_id": existing,
            "uid": str(resolved_uid),
            "display_name": str(nickname or ""),
            "created_new": False,
            "reused_existing": True,
            "restart_required": True,
        }
    raise OnboardingError("unreachable", "unreachable")  # pragma: no cover


# ---------------------------------------------------------------------------
# Server-side onboarding session（M7 完整流程）
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = 600
RECEIPT_TTL_SECONDS = 24 * 60 * 60
_SESSION_DIR = None  # data root / shared / onboarding（惰性解析）


def _onboarding_dir() -> Path:
    from src.data_paths import get_shared_dir

    directory = get_shared_dir() / "onboarding"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _session_path(onboarding_id: str) -> Path:
    return _onboarding_dir() / f"{onboarding_id}.json"


_SECRETS_DIRNAME = ".secrets"
_ONBOARDING_SECRET_KINDS = ("qr_key", "cookie")


def _secrets_dir_path() -> Path:
    return _onboarding_dir() / _SECRETS_DIRNAME


def _secret_path(onboarding_id: str, kind: str) -> Path:
    """secret sidecar 路径由 onboarding_id + 固定 kind 推导；文件名不含 secret。"""
    if kind not in _ONBOARDING_SECRET_KINDS:
        raise ValueError(f"未知 onboarding secret 类型：{kind}")
    return _secrets_dir_path() / f"{onboarding_id}.{kind}"


def _write_onboarding_secret(onboarding_id: str, kind: str, value: str) -> None:
    """统一走项目 secure secret 原语（原子写 + 尽力权限收紧）。"""
    write_text_secret(_secret_path(onboarding_id, kind), value)


def _read_onboarding_secret(onboarding_id: str, kind: str) -> str:
    try:
        return _secret_path(onboarding_id, kind).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _delete_onboarding_secret(onboarding_id: str, kind: str) -> None:
    try:
        _secret_path(onboarding_id, kind).unlink(missing_ok=True)
    except OSError:
        pass


def _delete_onboarding_secrets(onboarding_id: str) -> None:
    for kind in _ONBOARDING_SECRET_KINDS:
        _delete_onboarding_secret(onboarding_id, kind)


def _render_qr_png(onboarding_id: str, qr_url: str) -> Path:
    """把 QR payload 渲染为 PNG（qr_url 只在此使用，绝不写入普通 session JSON）。"""
    image_path = _session_path(onboarding_id).with_suffix(".png")
    try:
        import qrcode  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise OnboardingError("qr_render_unavailable", f"缺少二维码渲染库：{type(exc).__name__}") from exc
    try:
        image = qrcode.make(qr_url)
        image.save(image_path)
    except Exception as exc:  # noqa: BLE001
        raise OnboardingError("qr_render_unavailable", f"二维码渲染失败：{type(exc).__name__}") from exc
    return image_path


def onboarding_create(*, qr_url: str, qr_key: str) -> dict[str, Any]:
    """创建未绑定 Profile 的 onboarding session。

    - qr_url 只用于立即渲染 QR PNG，不持久化到普通 session JSON；
    - qrcode_key 写入 .secrets/<id>.qr_key（write_text_secret）；
    - 普通 JSON 只保存非敏感 metadata 与 has_* 标志。
    """
    onboarding_id = uuid.uuid4().hex[:16]
    _render_qr_png(onboarding_id, qr_url)
    now = int(time.time())
    session: dict[str, Any] = {
        "onboarding_id": onboarding_id,
        "created_at": now,
        "updated_at": now,
        "status": "waiting",
        "has_qr_key": True,
        "has_cookie": False,
        "failure_code": "",
        "result": None,
    }
    try:
        _write_onboarding_secret(onboarding_id, "qr_key", qr_key)
        _atomic_write_text(_session_path(onboarding_id), json.dumps(session, ensure_ascii=False) + "\n")
    except BaseException:
        _cleanup_session_material(onboarding_id)
        raise
    return {
        "onboarding_id": onboarding_id,
        "status": "waiting",
        "ttl_seconds": SESSION_TTL_SECONDS,
    }


def _read_session(onboarding_id: str) -> dict[str, Any]:
    path = _session_path(onboarding_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise OnboardingError("session_missing", "onboarding 会话不存在或已过期")
    return raw if isinstance(raw, dict) else {}


def _write_session(session: dict[str, Any]) -> None:
    _atomic_write_text(_session_path(str(session["onboarding_id"])), json.dumps(session, ensure_ascii=False) + "\n")


def _session_expired(session: dict[str, Any], now: int) -> bool:
    return int(session.get("created_at") or 0) + SESSION_TTL_SECONDS < int(now)


def onboarding_store_scanned_cookie(
    onboarding_id: str,
    cookie_str: str,
    *,
    now: int | None = None,
    qr_key_consumed: bool = False,
) -> None:
    """扫码成功：Cookie 只落 secret sidecar（write_text_secret），普通 JSON 只记 has_cookie。"""
    session = _read_session(onboarding_id)
    timestamp = int(now if now is not None else time.time())
    if _session_expired(session, timestamp):
        raise OnboardingError("session_expired", "onboarding 已过期，不能 commit")
    if session.get("status") in ("completed", "failed"):
        raise OnboardingError("session_closed", "onboarding 已结束")
    _write_onboarding_secret(onboarding_id, "cookie", cookie_str)
    session["status"] = "scanned"
    session["has_cookie"] = True
    if qr_key_consumed:
        session["has_qr_key"] = False
    session["updated_at"] = timestamp
    _write_session(session)


def onboarding_complete(onboarding_id: str, *, nickname: str = "") -> dict[str, Any]:
    """服务端用 staged Cookie（secret sidecar）解析 UID 并 commit；同 id 重复调用幂等。

    crash/retry 语义：
    - 读取 cookie sidecar → commit_staged_login → 原子写 sanitized completed receipt
      → 删除全部 onboarding secret / QR PNG；
    - receipt durable 之前崩溃：cookie sidecar 仍在（status=committing），重试会因 UID
      查重复用 Profile，不会创建第二个；随后写入 receipt 并清理 secret；
    - receipt durable 之后：任何重复 complete 直接返回相同 result（不要求 secret 存在）。
    """
    session = _read_session(onboarding_id)
    if session.get("result") is not None and session.get("status") == "completed":
        return dict(session["result"])  # 幂等：已固化的 sanitized receipt
    now = int(time.time())
    if _session_expired(session, now) or session.get("status") == "failed":
        _strip_secrets(session)
        _write_session(session)
        _delete_onboarding_secrets(onboarding_id)
        raise OnboardingError("session_expired", "onboarding 已过期或失败，不能 commit")
    cookie_str = _read_onboarding_secret(onboarding_id, "cookie")
    if not cookie_str:
        _logger.warning(
            "onboarding secret missing: session=%s kind=cookie", str(onboarding_id)[:8]
        )
        raise OnboardingError("session_secret_missing", "扫码会话凭据已失效，请重新获取二维码")
    # UID 只由服务端从 staged Cookie 确认
    uid = uid_from_cookie(cookie_str)
    session["status"] = "committing"
    session["updated_at"] = int(time.time())
    _write_session(session)
    try:
        result = commit_staged_login(cookie_str=cookie_str, uid=uid, nickname=nickname)
    except Exception:
        # 失败：清除 secret 并保留失败态（不可再次 commit）
        session["status"] = "failed"
        session["failure_code"] = "commit_failed"
        session["has_cookie"] = False
        _strip_secrets(session)
        _write_session(session)
        _delete_onboarding_secrets(onboarding_id)
        raise
    # uid（DedeUserID）不写入 receipt / 返回体，保持 sanitized
    sanitized_result = {key: value for key, value in dict(result).items() if key != "uid"}
    # Profile/pointer 已确定：构造并原子 durable 保存 sanitized completed receipt（无任何 secret）
    completed = {
        "onboarding_id": onboarding_id,
        "status": "completed",
        "completed_at": int(time.time()),
        "nickname": str(nickname or ""),
        "result": sanitized_result,
    }
    _atomic_write_text(_session_path(onboarding_id), json.dumps(completed, ensure_ascii=False) + "\n")
    # receipt durable 之后才清理 secret / QR PNG；失败只记录，残留由 sweep 兜底。
    _delete_onboarding_secrets(onboarding_id)
    try:
        _session_path(onboarding_id).with_suffix(".png").unlink(missing_ok=True)
    except OSError:
        _logger.warning("onboarding complete 后清理 QR PNG 失败：%s", onboarding_id)
    return dict(sanitized_result)


def _strip_secrets(session: dict[str, Any]) -> dict[str, Any]:
    # 兼容升级前的旧格式 session（新格式不含这些键）
    for key in ("cookie", "qr_url", "qr_key"):
        session.pop(key, None)
    return session


def onboarding_fail(onboarding_id: str, *, reason: str = "") -> None:
    session = _read_session(onboarding_id)
    session["status"] = "failed"
    session["failure_code"] = str(reason or "")
    session["has_qr_key"] = False
    session["has_cookie"] = False
    session["updated_at"] = int(time.time())
    _strip_secrets(session)
    _write_session(session)
    _delete_onboarding_secrets(onboarding_id)


def _cleanup_session_material(onboarding_id: str) -> None:
    for path in (
        _session_path(onboarding_id),
        _session_path(onboarding_id).with_suffix(".png"),
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _delete_onboarding_secrets(onboarding_id)


def _sweep_orphan_secrets(now_ts: int) -> None:
    """清理无对应 session 且已明显超过 TTL 的 orphan secret sidecar（崩溃窗口兜底）。"""
    secrets_dir = _secrets_dir_path()
    if not secrets_dir.is_dir():
        return
    try:
        entries = sorted(secrets_dir.iterdir())
    except OSError:
        return
    sessions_dir = _onboarding_dir()
    for path in entries:
        if not path.is_file():
            continue
        onboarding_id = path.name.rsplit(".", 1)[0]
        if (sessions_dir / f"{onboarding_id}.json").exists():
            continue
        try:
            age = now_ts - int(path.stat().st_mtime)
        except OSError:
            continue
        if age > SESSION_TTL_SECONDS:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def onboarding_sweep_expired(now: int | None = None) -> list[str]:
    """Opportunistic 清理过期 onboarding staging（同步、无 thread/timer）。

    - waiting/scanned/committing（含 secret sidecar / has_* 标志）：超过
      SESSION_TTL_SECONDS 即删除（敏感凭据不留存）；同名 QR PNG 与 secret 一并删除。
    - completed/failed sanitized receipt（已无凭据）：保留 RECEIPT_TTL_SECONDS
      供 duplicate complete 幂等，超龄后删除。
    - 额外清理无 session 且超过 TTL 的 orphan secret sidecar。
    - 只清理 shared/onboarding 目录，绝不触碰任何 Profile。
    返回被清理的 onboarding_id 列表。
    """
    now_ts = int(now if now is not None else time.time())
    try:
        directory = _onboarding_dir()
        entries = sorted(directory.glob("*.json"))
    except OSError:
        return []
    removed: list[str] = []
    for path in entries:
        onboarding_id = path.stem
        session: dict[str, Any] = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            session = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            # 无法解析：仅在文件本身已超龄时删除（避免误删正在写入的会话）
            try:
                too_old = now_ts - int(path.stat().st_mtime) > SESSION_TTL_SECONDS
            except OSError:
                continue
            if not too_old:
                continue
        else:
            base_ts = int(session.get("completed_at") or session.get("created_at") or 0)
            if not base_ts:
                continue
            has_sensitive = bool(
                session.get("cookie")
                or session.get("qr_url")
                or session.get("qr_key")
                or session.get("has_qr_key")
                or session.get("has_cookie")
            )
            ttl = SESSION_TTL_SECONDS if has_sensitive else RECEIPT_TTL_SECONDS
            if base_ts + ttl >= now_ts:
                continue
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        _delete_onboarding_secrets(onboarding_id)
        try:
            path.with_suffix(".png").unlink(missing_ok=True)
        except OSError:
            pass
        removed.append(onboarding_id)
    # 孤儿 QR PNG（对应 session 已不存在）：一并清理
    try:
        directory = _onboarding_dir()
        for png in directory.glob("*.png"):
            if not png.with_suffix(".json").exists():
                try:
                    png.unlink(missing_ok=True)
                except OSError:
                    pass
    except OSError:
        pass
    # 孤儿 secret sidecar（无 session 且超 TTL）
    _sweep_orphan_secrets(now_ts)
    return removed


def onboarding_status_public(onboarding_id: str) -> dict[str, Any]:
    session = _read_session(onboarding_id)
    payload = {
        "onboarding_id": onboarding_id,
        "status": str(session.get("status") or "waiting"),
    }
    if session.get("result") is not None:
        payload["result"] = dict(session["result"])
    return payload


# ---------------------------------------------------------------------------
# QR 网络层（真实流程使用；测试通过注入/monkeypatch 保持离线）
# ---------------------------------------------------------------------------

_ONBOARDING_COOKIE_REQUIRED_KEYS = ("SESSDATA", "bili_jct", "DedeUserID")


def _log_poll_failure(onboarding_id: str, stage: str, exc: Exception) -> None:
    """脱敏记录 poll 失败：短 session id + 类型 + HTTP 状态 + attempt，绝不记录 qrcode_key/Cookie。"""
    http_status = getattr(exc, "http_status", None)
    error_type = getattr(exc, "last_exception_type", "") or type(exc).__name__
    attempts = int(getattr(exc, "attempts", 1) or 1)
    _logger.warning(
        "onboarding qr poll failed: session=%s stage=%s type=%s http_status=%s attempt=%s",
        str(onboarding_id)[:8],
        stage,
        error_type,
        http_status if http_status is not None else "-",
        attempts,
    )


def onboarding_start_remote() -> dict[str, Any]:
    """真实流程：请求 Bilibili 二维码并创建未绑定 onboarding session。"""
    from src import bilibili_login as blogin

    onboarding_sweep_expired()
    try:
        qr_url, qr_key = blogin.generate_qrcode()
    except blogin.BilibiliLoginError as exc:
        raise OnboardingError("qr_unavailable", str(exc)) from exc
    return onboarding_create(qr_url=qr_url, qr_key=qr_key)


def onboarding_poll(onboarding_id: str) -> dict[str, Any]:
    """轮询扫码状态；成功时 Cookie 只写入 staging session（不写任何 Profile）。

    请求语义与 legacy QR poll 完全共享（PASSPORT_HEADERS/timeout/有限重试/4xx 分类）。
    状态：0 → success（Cookie staging）；86090 → scanned（等待手机确认）；
    86101 → waiting；86038/-2/86039 → expired；未知 → protocol_error 并结束。
    """
    from src import bilibili_login as blogin

    session = _read_session(onboarding_id)
    if session.get("status") in ("completed", "failed"):
        return onboarding_status_public(onboarding_id)
    if _session_expired(session, int(time.time())):
        raise OnboardingError("session_expired", "onboarding 已过期")
    qr_key = _read_onboarding_secret(onboarding_id, "qr_key")
    if not qr_key:
        _logger.warning(
            "onboarding secret missing: session=%s kind=qr_key", str(onboarding_id)[:8]
        )
        raise OnboardingError("session_secret_missing", "扫码会话凭据已失效，请重新获取二维码")

    client = blogin.create_passport_client()
    try:
        try:
            body, resp = blogin.poll_qrcode_request(client, qr_key)
        except blogin.QrPollError as exc:
            _log_poll_failure(onboarding_id, "transport", exc)
            if exc.category == "http_error":
                raise OnboardingError(
                    "poll_http_error",
                    f"B站拒绝查询扫码状态（HTTP {exc.http_status}），请重新获取二维码",
                ) from exc
            if exc.category == "upstream_error":
                raise OnboardingError(
                    "poll_upstream_error",
                    "暂时无法查询扫码状态，请重新获取二维码",
                ) from exc
            if exc.category == "protocol_error":
                onboarding_fail(onboarding_id, reason="poll_protocol_error")
                raise OnboardingError(
                    "poll_protocol_error",
                    "登录状态查询失败（B站响应异常），请重新获取二维码",
                ) from exc
            raise OnboardingError(
                "poll_network_error",
                "暂时无法查询扫码状态，请重新获取二维码",
            ) from exc

        state = blogin._poll_status(body)
        if state == blogin.POLL_SUCCESS:
            try:
                raw_cookie = blogin._collect_login_cookies(client, resp, body)
            except Exception as exc:  # noqa: BLE001 - Cookie 收集失败必须显式分类
                _log_poll_failure(onboarding_id, "cookie", exc)
                onboarding_fail(onboarding_id, reason="cookie_collect_failed")
                raise OnboardingError(
                    "cookie_incomplete", "登录 Cookie 收集失败，请重新扫码"
                ) from exc
            missing = [
                key
                for key in _ONBOARDING_COOKIE_REQUIRED_KEYS
                if not str(raw_cookie.get(key) or "").strip()
            ]
            if missing:
                _logger.warning(
                    "onboarding qr poll cookie incomplete: session=%s missing=%s",
                    str(onboarding_id)[:8],
                    ",".join(missing),
                )
                onboarding_fail(onboarding_id, reason="cookie_incomplete")
                raise OnboardingError("cookie_incomplete", "登录 Cookie 不完整，请重新扫码")
            essential = {k: v for k, v in raw_cookie.items() if k in blogin.ESSENTIAL_KEYS}
            cookie_str = blogin.cookies_to_header(essential or raw_cookie)
            onboarding_store_scanned_cookie(
                onboarding_id, cookie_str, now=int(time.time()), qr_key_consumed=True
            )
            # 扫码已确认，qrcode_key 不再需要：立即删除 secret sidecar
            _delete_onboarding_secret(onboarding_id, "qr_key")
            return {"onboarding_id": onboarding_id, "status": "success", "need_confirm": True}

        if state in (-2, blogin.POLL_EXPIRED, 86039):
            onboarding_fail(onboarding_id, reason="expired")
            return {"onboarding_id": onboarding_id, "status": "expired", "reason": "expired"}

        if state == blogin.POLL_SCANNED:
            return {"onboarding_id": onboarding_id, "status": "scanned"}

        if state == blogin.POLL_WAITING:
            return {"onboarding_id": onboarding_id, "status": "waiting"}

        onboarding_fail(onboarding_id, reason="unknown_status")
        _logger.warning(
            "onboarding qr poll unknown status: session=%s status=%s",
            str(onboarding_id)[:8],
            state,
        )
        raise OnboardingError(
            "poll_protocol_error",
            f"登录状态查询失败（B站返回未知状态 {state}），请重新获取二维码",
        )
    finally:
        client.close()


def onboarding_qr_image_path(onboarding_id: str) -> Path:
    """返回 staging 二维码 PNG 路径（无网络）；PNG 在 onboarding_create 时已渲染。"""
    session = _read_session(onboarding_id)
    if session.get("status") in ("completed", "failed"):
        raise OnboardingError("session_closed", "会话已结束")
    image_path = _session_path(onboarding_id).with_suffix(".png")
    if image_path.exists():
        return image_path
    raise OnboardingError("qr_render_unavailable", "二维码图片不可用，请重新获取")

