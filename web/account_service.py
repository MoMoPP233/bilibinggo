from __future__ import annotations

import threading
import time
from typing import Any

from src.bilibili_auth import get_login_uid
from src.bilibili_client import BilibiliClient
from src.message_api import get_unread_summary
from src.message_watch import BILIBILI_AT_NOTIFY_URL, acknowledge_at_unread, evaluate_at_unread_alert
from web.user_messages import friendly_network_error

NAV_URL = "https://api.bilibili.com/x/web-interface/nav"
NAV_STAT_URL = "https://api.bilibili.com/x/web-interface/nav/stat"
ACCOUNT_CLIENT_TIMEOUT = 8.0
# 平台风控 / 限流：Cookie 并未失效，只是暂时不可读，不能当成“已过期”显示。
_NAV_RISK_CODES = frozenset({-352, -509, -799, 429})

# /api/account 短 TTL：成功验证快照仅在很短窗口内命中，之后允许真实刷新。
# 选择依据：账号信息主要由 bootstrap（1 次）、Job 完成后台同步（≤1 次）与
# 用户手动刷新触发；15 秒足以折叠同刻多模块并发读，又远小于用户两次
# “刷新账号”的间隔，不产生可感知的陈旧。
ACCOUNT_PROFILE_TTL_SECONDS = 15.0

_cache_lock = threading.Lock()
_success_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_inflight: dict[str, threading.Event] = {}


def invalidate_account_cache() -> None:
    """显式失效所有账号成功快照（logout / 明确 -101 / no-cookie 等）。

    之后的下一次 /api/account 会重新远程验证，不会返回旧快照。
    """
    with _cache_lock:
        _success_cache.clear()
        _inflight.clear()


def _cookie_cache_key() -> str | None:
    """基于“真实 cookie 文件 + 内容指纹”的缓存键；文件不存在则不缓存。

    指纹变化（登录写入 / Cookie 更新/替换 / 文件删除）自动视为失效；
    键包含绝对路径，天然 Profile 隔离（不同 Profile 的文件路径不同）。
    """
    try:
        from src import app_paths

        path = app_paths.cookie_file()
        stat = path.stat()
    except OSError:
        return None
    return f"{path.resolve()}::{stat.st_size}::{stat.st_mtime_ns}"


def _read_success_cache(key: str) -> dict[str, Any] | None:
    with _cache_lock:
        entry = _success_cache.get(key)
        if entry is None:
            return None
        verified_at, snapshot = entry
        if time.monotonic() - verified_at > ACCOUNT_PROFILE_TTL_SECONDS:
            return None
        return dict(snapshot)


def _store_success_cache(key: str, snapshot: dict[str, Any]) -> None:
    with _cache_lock:
        if len(_success_cache) >= 4:
            oldest = min(_success_cache, key=lambda item: _success_cache[item][0])
            _success_cache.pop(oldest, None)
        _success_cache[key] = (time.monotonic(), dict(snapshot))


def _wait_shared_remote(key: str, timeout: float = ACCOUNT_CLIENT_TIMEOUT + 3.0) -> bool:
    """等待同 key 在途远程验证完成；返回是否已有可用快照。"""
    with _cache_lock:
        event = _inflight.get(key)
        if event is None:
            event = threading.Event()
            _inflight[key] = event
            return False
    event.wait(timeout=timeout)
    return _read_success_cache(key) is not None


def _finish_inflight(key: str) -> None:
    with _cache_lock:
        event = _inflight.pop(key, None)
        if event is not None:
            event.set()


def get_local_login_state() -> dict[str, Any]:
    """纯本地 runtime 登录视图（0 Bilibili 请求）。

    用于本地页面数据/配置定位：只回答“本 Profile 是否有本地 Cookie、
    本地曾验证过的账号元数据是什么”，不回答“B 站此刻是否仍确认登录”。
    真正的实时确认请使用 get_account_profile()（短 TTL 远程验证）。
    """
    if not has_login_cookie():
        return {
            "logged_in": False,
            "mid": None,
            "uname": "",
            "face": "",
        }
    cached = _load_account_cache()
    uid = get_login_uid()
    mid = cached.get("mid") if cached.get("mid") is not None else uid
    if mid is None and not cached:
        return {
            "logged_in": False,
            "mid": None,
            "uname": "",
            "face": "",
        }
    return {
        "logged_in": True,
        "mid": mid,
        "uname": str(cached.get("uname") or ""),
        "face": str(cached.get("face") or ""),
    }



def _api_code(payload: dict[str, Any]) -> int:
    code = payload.get("code")
    if code is None:
        return -1
    try:
        return int(code)
    except (TypeError, ValueError):
        return -1


def _empty_profile(message: str = "") -> dict[str, Any]:
    return {
        "logged_in": False,
        "expired": True,
        "message": message,
        "uname": "",
        "face": "",
        "mid": None,
        "following": None,
        "dynamic_count": None,
        "unread_messages": None,
        "unread_at": None,
        "extras_loading": False,
        "at_alert": _default_at_alert(),
        "at_notify_url": BILIBILI_AT_NOTIFY_URL,
    }


def _default_at_alert(current: int = 0) -> dict[str, Any]:
    return {
        "increased": False,
        "delta": 0,
        "previous": 0,
        "current": current,
    }


def _load_account_cache() -> dict[str, Any]:
    from src.db.json_cols import loads_json
    from src.db.models import AccountProfileCacheRow
    from src.db.session import session_scope

    try:
        with session_scope() as session:
            row = session.get(AccountProfileCacheRow, 1)
            if row is None:
                return {}
            raw = loads_json(row.raw_json, default={})
            if isinstance(raw, dict) and raw:
                return raw
            return {
                key: getattr(row, key)
                for key in ("uname", "face", "mid", "following", "dynamic_count")
                if getattr(row, key) is not None
            }
    except Exception:
        return {}


def _save_account_cache(profile: dict[str, Any]) -> None:
    from src.db.json_cols import dumps_json
    from src.db.models import AccountProfileCacheRow
    from src.db.session import session_scope

    payload = {
        key: profile.get(key)
        for key in ("uname", "face", "mid", "following", "dynamic_count")
        if profile.get(key) is not None
    }
    if not payload:
        return
    try:
        import time

        with session_scope() as session:
            row = session.get(AccountProfileCacheRow, 1)
            now = int(time.time())
            if row is None:
                session.add(
                    AccountProfileCacheRow(
                        id=1,
                        uname=payload.get("uname"),
                        face=payload.get("face"),
                        mid=int(payload["mid"]) if payload.get("mid") is not None else None,
                        following=int(payload["following"])
                        if payload.get("following") is not None
                        else None,
                        dynamic_count=int(payload["dynamic_count"])
                        if payload.get("dynamic_count") is not None
                        else None,
                        updated_at=now,
                        raw_json=dumps_json(payload),
                    )
                )
            else:
                row.uname = payload.get("uname")
                row.face = payload.get("face")
                row.mid = int(payload["mid"]) if payload.get("mid") is not None else None
                row.following = (
                    int(payload["following"]) if payload.get("following") is not None else None
                )
                row.dynamic_count = (
                    int(payload["dynamic_count"])
                    if payload.get("dynamic_count") is not None
                    else None
                )
                row.updated_at = now
                row.raw_json = dumps_json(payload)
    except Exception:
        pass


def _sync_profile_metadata(profile: dict[str, Any]) -> None:
    """同步已验证账号的展示信息；写入失败不应影响现有登录流程。"""
    try:
        from src.profile_manager import update_profile_metadata

        update_profile_metadata(
            mid=profile.get("mid"),
            nickname=str(profile.get("uname") or ""),
        )
    except (OSError, RuntimeError, ValueError):
        pass


def _profile_from_network_error(exc: RuntimeError) -> dict[str, Any]:
    profile = _empty_profile(friendly_network_error(str(exc)))
    profile["cookie_saved"] = True
    profile["network_error"] = True
    uid = get_login_uid()
    if uid:
        profile["mid"] = uid
    cached = _load_account_cache()
    for key in ("uname", "face", "following", "dynamic_count", "mid"):
        if cached.get(key) is not None:
            profile[key] = cached[key]
    return profile


def clear_login_cookie() -> None:
    from src import app_paths
    from src.secure_files import harden_file_permissions

    path = app_paths.cookie_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 明确退出登录：立即失效成功快照，绝不能继续展示“已登录”。
    invalidate_account_cache()
    if path.exists():
        try:
            path.unlink()
            return
        except OSError:
            path.write_text("", encoding="utf-8")
            harden_file_permissions(path)
    else:
        path.write_text("", encoding="utf-8")
        harden_file_permissions(path)


def has_login_cookie() -> bool:
    from src import app_paths

    path = app_paths.cookie_file()
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return bool(text) and "SESSDATA" in text


def _fetch_and_store(key: str) -> dict[str, Any]:
    """执行一次真实远程验证并按规则写成功快照（force 与普通 owner 共用）。"""
    profile = _fetch_remote_profile()
    # 只有“真实成功验证”才能写入成功快照；网络失败 fallback 与
    # -101/未登录等非成功结果绝不写缓存（否则失败会把 verified_at 前移续期）。
    if (
        profile.get("logged_in") is True
        and not profile.get("network_error")
        and not profile.get("expired")
    ):
        _store_success_cache(key, profile)
    return profile


def get_account_profile(*, force: bool = False) -> dict[str, Any]:
    """返回账号基础信息，不阻塞在私信/@ 未读接口上。

    本地真实 cookie 文件存在时使用“短 TTL 成功快照 + single-flight”：
    - TTL 内多次普通调用只有首次真实远程验证；
    - force=True（用户主动“刷新账号”）明确绕过 TTL 与在途合并，真实远程验证；
    - TTL 到期 / cookie 指纹变化 / 明确登出 / -101 都会重新远程验证；
    - 网络失败回退旧可信快照属于 fallback，绝不把 verified_at 前移。
    危险写操作不应以本快照替代自己的实时身份 recheck（由调用方保证）。
    """
    if not has_login_cookie():
        return _empty_profile("请使用侧边栏扫码登录")

    key = _cookie_cache_key()
    if key is None:
        # 本地文件不可读（罕见）：按旧行为直接远程验证，不缓存。
        return _fetch_remote_profile()

    if force:
        # 主动刷新：不读缓存、不与在途请求合并，执行一次真实验证。
        return _fetch_and_store(key)

    cached = _read_success_cache(key)
    if cached is not None:
        return cached

    shared = _wait_shared_remote(key)
    if shared:
        hit = _read_success_cache(key)
        if hit is not None:
            return hit

    # 并发窗口内的最后检查：若在途验证刚完成（本线程随后才获得所有权），
    # 直接复用其成功快照，不再发起第二次远程验证。
    late_hit = _read_success_cache(key)
    if late_hit is not None:
        return late_hit

    try:
        return _fetch_and_store(key)
    finally:
        _finish_inflight(key)


def _fetch_remote_profile() -> dict[str, Any]:
    try:
        with BilibiliClient(timeout=ACCOUNT_CLIENT_TIMEOUT, warmup=False) as client:
            nav_payload = client.request_json(
                NAV_URL,
                referer="https://www.bilibili.com",
                retries=0,
            )
            code = _api_code(nav_payload)
            data = nav_payload.get("data") or {}
            is_login = bool(data.get("isLogin"))
            if not is_login and code in _NAV_RISK_CODES:
                # 展示语义分层：-352/-509/429 是正式平台风控/限流；
                # -799 只是临时限频（temporary restriction），不是正式 platform risk，
                # 二者都不代表 Cookie 失效，且都不写任何 cooldown/pause。
                from src.platform_risk import is_platform_risk_code

                if is_platform_risk_code(code):
                    message = (
                        f"平台风控/暂时限流（{code}），本地 Cookie 仍已保存，稍后可刷新账号"
                    )
                else:
                    message = (
                        f"请求暂时受限（{code}，临时限频），本地 Cookie 仍有效，稍后可刷新账号"
                    )
                return _profile_from_network_error(RuntimeError(message))
            if code == -101 or not is_login:
                # 明确不再登录：旧成功快照立即失效。
                invalidate_account_cache()
                return _empty_profile("Cookie 已过期，请重新扫码登录")

            mid = int(data.get("mid") or 0) or None
            profile: dict[str, Any] = {
                "logged_in": True,
                "expired": False,
                "message": "已登录",
                "uname": str(data.get("uname") or ""),
                "face": str(data.get("face") or ""),
                "mid": mid,
                "following": None,
                "dynamic_count": None,
                "unread_messages": None,
                "unread_at": None,
                "extras_loading": True,
                "at_alert": _default_at_alert(),
                "at_notify_url": BILIBILI_AT_NOTIFY_URL,
            }

            try:
                stat_payload = client.request_json(
                    NAV_STAT_URL,
                    params={"mid": mid} if mid else None,
                    referer="https://www.bilibili.com",
                    retries=0,
                )
                if _api_code(stat_payload) == 0:
                    stat = stat_payload.get("data") or {}
                    profile["following"] = int(stat.get("following") or 0)
                    profile["dynamic_count"] = int(stat.get("dynamic_count") or 0)
            except RuntimeError:
                pass

            _save_account_cache(profile)
            _sync_profile_metadata(profile)
            return profile
    except RuntimeError as exc:
        if has_login_cookie():
            return _profile_from_network_error(exc)
        return _empty_profile(friendly_network_error(str(exc)))


def get_account_extras() -> dict[str, Any]:
    """补充私信/@ 未读与提醒，供前端后台加载。"""
    if not has_login_cookie():
        raise RuntimeError("未登录，请先扫码登录")

    uid = get_login_uid()
    extras: dict[str, Any] = {
        "unread_messages": 0,
        "unread_at": 0,
        "extras_loading": False,
        "at_alert": _default_at_alert(),
        "at_notify_url": BILIBILI_AT_NOTIFY_URL,
    }

    try:
        with BilibiliClient(timeout=ACCOUNT_CLIENT_TIMEOUT, warmup=False) as client:
            unread_summary = get_unread_summary(client, retries=0)
            extras["unread_messages"] = unread_summary.get("dm", 0)
            extras["unread_at"] = unread_summary.get("at", 0)
            extras["unread_by_type"] = unread_summary
    except RuntimeError:
        pass

    if uid:
        try:
            alert = evaluate_at_unread_alert(uid, int(extras.get("unread_at") or 0))
            extras["at_alert"] = alert.to_dict()
        except OSError:
            extras["at_alert"] = _default_at_alert(int(extras.get("unread_at") or 0))

    return extras


def ack_at_unread_notice(current: int) -> dict[str, Any]:
    uid = get_login_uid()
    if not uid:
        raise RuntimeError("未登录，请先扫码登录")
    baseline = acknowledge_at_unread(uid, current)
    alert = evaluate_at_unread_alert(uid, current)
    return {
        "acknowledged": True,
        "last_seen_unread_at": baseline,
        "at_alert": alert.to_dict(),
        "at_notify_url": BILIBILI_AT_NOTIFY_URL,
    }
