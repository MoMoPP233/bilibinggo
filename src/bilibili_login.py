from __future__ import annotations

import threading
import time
import urllib.parse
import re
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Callable

import httpx

try:
    from src.app_logging import get_logger
except ImportError:  # pragma: no cover
    import logging

    def get_logger(name: str) -> logging.Logger:
        return logging.getLogger(name)

logger = get_logger("login")

from src.app_paths import COOKIE_PATH, QR_IMAGE_PATH, ensure_user_dirs

PASSPORT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.bilibili.com/",
    "Origin": "https://www.bilibili.com",
}

QR_GENERATE_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
QR_POLL_URL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"

PASSPORT_TIMEOUT_SECONDS = 20.0
QR_POLL_TIMEOUT_SECONDS = 12.0
QR_POLL_MAX_ATTEMPTS = 3
QR_POLL_RETRY_BACKOFF_SECONDS = 0.4

POLL_WAITING = 86101
POLL_SCANNED = 86090
POLL_EXPIRED = 86038
POLL_SUCCESS = 0

ESSENTIAL_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "buvid3", "b_nut")


class BilibiliLoginError(Exception):
    pass


class LoginCancelledError(BilibiliLoginError):
    pass


def _safe_response_body_summary(response: httpx.Response, limit: int = 200) -> str:
    """Return a short diagnostic body with login-related values redacted."""
    try:
        summary = " ".join(response.text.split())[:limit]
    except Exception:  # pragma: no cover - defensive around malformed responses
        return "<unavailable>"
    return re.sub(
        r'(?i)("?(?:cookie|sessdata|bili_jct|qrcode_key)"?\s*[:=]\s*)("[^"]*"|[^,\s}]+)',
        r"\1<redacted>",
        summary,
    )


def create_passport_client(*, timeout: float = PASSPORT_TIMEOUT_SECONDS) -> httpx.Client:
    """统一的 passport 请求 client（PASSPORT_HEADERS + follow_redirects）。"""
    return httpx.Client(headers=PASSPORT_HEADERS, follow_redirects=True, timeout=timeout)


def generate_qrcode(client: httpx.Client | None = None) -> tuple[str, str]:
    """Generate one login QR payload without polling or writing Profile state."""
    owns_client = client is None
    transport = client or create_passport_client()
    try:
        try:
            response = transport.get(
                QR_GENERATE_URL,
                params={"source": "main-fe-header"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            response = exc.response
            request_url = response.request.url
            logger.error(
                "二维码生成请求失败: status=%s host=%s path=%s content_type=%s redirects=%s body_summary=%s",
                response.status_code,
                request_url.host,
                request_url.path,
                response.headers.get("content-type", ""),
                len(response.history),
                _safe_response_body_summary(response),
            )
            raise BilibiliLoginError(
                f"获取二维码失败（B站返回 HTTP {response.status_code}）"
            ) from exc
        except httpx.HTTPError as exc:
            raise BilibiliLoginError(
                f"获取二维码失败（网络请求异常：{type(exc).__name__}）"
            ) from exc

        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise BilibiliLoginError("获取二维码失败（B站响应不是有效 JSON）") from exc
        if not isinstance(body, dict):
            raise BilibiliLoginError("获取二维码失败（B站响应不是 JSON 对象）")
        if body.get("code") != 0:
            raise BilibiliLoginError(
                f"获取二维码失败（B站 code {body.get('code')}：{body.get('message') or '未知错误'}）"
            )
        data = body.get("data") or {}
        qrcode_url = str(data.get("url") or "").strip()
        qrcode_key = str(data.get("qrcode_key") or "").strip()
        if not qrcode_url or not qrcode_key:
            raise BilibiliLoginError("获取二维码失败：响应缺少 url 或 qrcode_key")
        request = getattr(response, "request", None)
        request_url = getattr(request, "url", None)
        logger.info(
            "二维码生成请求成功: status=%s host=%s path=%s content_type=%s redirects=%s",
            getattr(response, "status_code", 200),
            getattr(request_url, "host", ""),
            getattr(request_url, "path", ""),
            response.headers.get("content-type", ""),
            len(getattr(response, "history", ())),
        )
        return qrcode_url, qrcode_key
    finally:
        if owns_client:
            transport.close()


def _poll_status(body: dict) -> int:
    """扫码状态在 data.code，不是顶层 code。"""
    data = body.get("data") or {}
    for candidate in (data.get("code"), body.get("code")):
        if candidate is None:
            continue
        try:
            return int(candidate)
        except (TypeError, ValueError):
            continue
    return -1


def _parse_set_cookie_headers(response: httpx.Response) -> dict[str, str]:
    jar: dict[str, str] = {}
    try:
        headers = response.headers.get_list("set-cookie")
    except AttributeError:
        raw = response.headers.get("set-cookie")
        headers = [raw] if raw else []
    for header in headers:
        if not header:
            continue
        sc = SimpleCookie()
        try:
            sc.load(header)
        except (ValueError, KeyError):
            continue
        for key, morsel in sc.items():
            jar[key] = morsel.value
    return jar


def _parse_cross_domain_url(url: str) -> dict[str, str]:
    if not url:
        return {}
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qs(parsed.query)
    jar: dict[str, str] = {}
    for key in ESSENTIAL_KEYS:
        if key in query and query[key]:
            jar[key] = query[key][0]
    return jar


def _merge_cookies(*sources: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for src in sources:
        merged.update(src)
    return merged


def _cookies_from_jar(client: httpx.Client) -> dict[str, str]:
    """从 cookie jar 安全提取，同名多 domain 时优先 .bilibili.com。"""
    grouped: dict[str, list] = {}
    for cookie in client.cookies.jar:
        grouped.setdefault(cookie.name, []).append(cookie)

    result: dict[str, str] = {}
    for name, items in grouped.items():
        chosen = items[-1]
        for item in items:
            domain = item.domain or ""
            if domain.endswith("bilibili.com"):
                chosen = item
        result[name] = chosen.value
    return result


def cookies_to_header(cookies: dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items())


def save_cookies(cookie_str: str) -> Path:
    from src import app_paths
    from src.config_validate import validate_cookie_string
    from src.secure_files import write_text_secret

    try:
        content = validate_cookie_string(cookie_str)
    except ValueError as exc:
        raise BilibiliLoginError(str(exc)) from exc
    path = app_paths.cookie_file()
    write_text_secret(path, content)
    return path


def _sync_profile_mid(cookies: dict[str, str]) -> None:
    """扫码响应已带 DedeUserID 时立即写 Profile；昵称由后续 nav 同步。"""
    mid = str(cookies.get("DedeUserID") or "").strip()
    if not mid:
        return
    try:
        from src.profile_manager import update_profile_metadata

        update_profile_metadata(mid=mid)
    except (OSError, RuntimeError, ValueError):
        logger.warning("登录成功，但写入 Profile MID 失败", exc_info=True)


def _uid_from_login_cookies(cookies: dict[str, str]) -> str:
    return str(cookies.get("DedeUserID") or "").strip()


def _bound_uid_of_current_profile() -> str:
    try:
        from src.data_paths import get_runtime_profile_id
        from src.profile_manager import get_profile_metadata

        metadata = get_profile_metadata(get_runtime_profile_id())
    except (OSError, ValueError, RuntimeError):
        return ""
    return str(metadata.get("mid") or "").strip()


def _other_profile_for_uid(uid: str) -> str | None:
    try:
        from src.profile_manager import list_profiles

        for metadata in list_profiles():
            if str(metadata.get("mid") or "").strip() == uid:
                return str(metadata.get("profile_id") or "")
    except (OSError, ValueError, RuntimeError):
        return None
    return None


def verify_login_uid_for_current_profile(cookies: dict[str, str]) -> str:
    """扫码成功后、任何持久化之前校验身份，禁止把当前 Profile 换成其它 UID。

    - 当前 Profile 未绑定 UID（全新）：允许本次登录完成首次绑定。
    - 已绑定：扫码 UID 必须一致，否则拒绝；该 UID 已属其它 Profile 同样拒绝。
    """
    scanned_uid = _uid_from_login_cookies(cookies)
    bound_uid = _bound_uid_of_current_profile()
    if not bound_uid:
        return scanned_uid
    if not scanned_uid:
        raise BilibiliLoginError(
            "无法从扫码结果获取账号 UID，为防止身份漂移已取消登录，请重试。"
        )
    if scanned_uid == bound_uid:
        return scanned_uid
    other = _other_profile_for_uid(scanned_uid)
    if other:
        raise BilibiliLoginError(
            f"该扫码账号（UID {scanned_uid}）已绑定其它 Profile（{other}），"
            "请勿在当前 Profile 重复登录。"
        )
    raise BilibiliLoginError(
        "扫码账号与当前 Profile 不一致，请使用「扫码登录新账号」添加该账号。"
    )


def _request_json(
    client: httpx.Client,
    url: str,
    *,
    params: dict[str, str] | None = None,
    retries: int = 3,
) -> dict:
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("响应不是 JSON 对象")
            return body
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            last_exc = exc
            if attempt + 1 < retries:
                logger.warning("请求失败，重试 %s/%s: %s", attempt + 1, retries, exc)
                time.sleep(0.4 * (attempt + 1))
    raise BilibiliLoginError("网络请求失败，请检查网络后重试") from last_exc


class QrPollError(BilibiliLoginError):
    """共享 QR 轮询失败（结构化分类，由 legacy/onboarding 各自映射为用户语义）。"""

    def __init__(
        self,
        category: str,
        message: str,
        *,
        attempts: int,
        http_status: int | None = None,
        last_exception: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.category = str(category)
        self.attempts = int(attempts)
        self.http_status = http_status
        self.last_exception = last_exception

    @property
    def last_exception_type(self) -> str:
        return type(self.last_exception).__name__ if self.last_exception is not None else ""


def poll_qrcode_request(
    client: httpx.Client,
    qrcode_key: str,
    *,
    max_attempts: int = QR_POLL_MAX_ATTEMPTS,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict, httpx.Response]:
    """共享 QR 轮询请求语义（legacy login 与 M7 onboarding 唯一实现）。

    - 仅对 transport/timeout 与 HTTP 5xx 做有限重试（短退避）
    - HTTP 4xx 立即失败并保留 http_status（不猜测重试）
    - 非 JSON / 非对象响应 → protocol_error
    """
    attempts = max(1, int(max_attempts))
    do_sleep = sleep or time.sleep
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            response = client.get(
                QR_POLL_URL,
                params={"qrcode_key": qrcode_key, "source": "main-fe-header"},
                timeout=QR_POLL_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            last_exc = exc
            status = int(exc.response.status_code)
            if status < 500:
                raise QrPollError(
                    "http_error",
                    f"B站拒绝扫码状态查询（HTTP {status}）",
                    attempts=attempt,
                    http_status=status,
                    last_exception=exc,
                ) from exc
            if attempt < attempts:
                logger.warning("扫码轮询上游异常，重试 %s/%s: status=%s", attempt, attempts, status)
                do_sleep(QR_POLL_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise QrPollError(
                "upstream_error",
                f"B站扫码状态查询异常（HTTP {status}）",
                attempts=attempt,
                http_status=status,
                last_exception=exc,
            ) from exc
        except httpx.HTTPError as exc:
            last_exc = exc
            if attempt < attempts:
                logger.warning(
                    "扫码轮询网络异常，重试 %s/%s: %s", attempt, attempts, type(exc).__name__
                )
                do_sleep(QR_POLL_RETRY_BACKOFF_SECONDS * attempt)
                continue
            raise QrPollError(
                "network_error",
                "扫码轮询失败，请检查网络后重试",
                attempts=attempt,
                last_exception=exc,
            ) from exc
        try:
            body = response.json()
        except (ValueError, TypeError) as exc:
            raise QrPollError(
                "protocol_error",
                "B站扫码状态响应不是有效 JSON",
                attempts=attempt,
                http_status=int(getattr(response, "status_code", 0) or 0) or None,
                last_exception=exc,
            ) from exc
        if not isinstance(body, dict):
            raise QrPollError(
                "protocol_error",
                "B站扫码状态响应不是 JSON 对象",
                attempts=attempt,
                http_status=int(getattr(response, "status_code", 0) or 0) or None,
            )
        return body, response
    raise QrPollError(  # pragma: no cover - 防御性兜底
        "network_error",
        "扫码轮询失败，请检查网络后重试",
        attempts=attempts,
        last_exception=last_exc,
    )


def _poll_request(client: httpx.Client, qrcode_key: str) -> tuple[dict, httpx.Response]:
    """legacy 兼容包装：与 onboarding 共享同一 QR poll 请求语义。"""
    return poll_qrcode_request(client, qrcode_key)


def _generate_qrcode_image(url: str, out_path: Path) -> bool:
    try:
        import qrcode
    except ImportError:
        return False
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = qrcode.make(url)
    img.save(out_path)
    return True


def _collect_login_cookies(client: httpx.Client, poll: httpx.Response, body: dict) -> dict[str, str]:
    data = body.get("data") or {}
    cross_url = str(data.get("url") or "").strip()

    cookies = _merge_cookies(
        _parse_set_cookie_headers(poll),
        _parse_cross_domain_url(cross_url),
    )

    if cross_url:
        try:
            client.get(cross_url, timeout=12.0)
            cookies = _merge_cookies(cookies, _cookies_from_jar(client))
        except httpx.HTTPError as exc:
            logger.warning("登录 SSO 跳转失败: %s", exc)

    if "SESSDATA" not in cookies or "bili_jct" not in cookies:
        try:
            client.get("https://www.bilibili.com", timeout=12.0)
            cookies = _merge_cookies(cookies, _cookies_from_jar(client))
        except httpx.HTTPError as exc:
            logger.warning("登录后访问首页失败: %s", exc)

    if "SESSDATA" not in cookies:
        try:
            client.get("https://api.bilibili.com/x/web-interface/nav", timeout=12.0)
            cookies = _merge_cookies(cookies, _cookies_from_jar(client))
        except httpx.HTTPError as exc:
            logger.warning("登录后校验 nav 失败: %s", exc)

    return cookies


def login_with_qrcode(
    *,
    timeout: float = 180.0,
    open_image: bool = True,
    auto_refresh_on_expire: bool = False,
    cancel_event: threading.Event | None = None,
    on_qrcode_ready: Callable[[], None] | None = None,
    on_status_change: Callable[[str, str], None] | None = None,
) -> str:
    ensure_user_dirs()
    client = create_passport_client()

    def _check_cancelled() -> None:
        if cancel_event and cancel_event.is_set():
            raise LoginCancelledError("登录已取消")

    def _emit(phase: str, message: str) -> None:
        if on_status_change:
            on_status_change(phase, message)

    def _issue_qrcode() -> tuple[str, str]:
        qrcode_url, qrcode_key = generate_qrcode(client)
        if open_image:
            print("请使用哔哩哔哩手机 App 扫描下方二维码登录：")
            print(qrcode_url)
        image_ok = _generate_qrcode_image(qrcode_url, QR_IMAGE_PATH)
        if on_qrcode_ready and not image_ok:
            raise BilibiliLoginError("无法生成二维码图片，请安装: pip install qrcode[pil]")
        if image_ok and open_image:
            print(f"\n二维码图片已保存: {QR_IMAGE_PATH}")
            try:
                import os

                os.startfile(QR_IMAGE_PATH)
            except OSError:
                pass
        elif open_image and not image_ok:
            print("\n（未安装 qrcode 库，请安装: pip install qrcode[pil]）")
        if on_qrcode_ready:
            on_qrcode_ready()
        _emit("waiting", "请使用哔哩哔哩 App 扫描二维码")
        return qrcode_url, qrcode_key

    try:
        if QR_IMAGE_PATH.exists():
            try:
                QR_IMAGE_PATH.unlink()
            except OSError:
                pass
        client.get("https://www.bilibili.com")
        _, qrcode_key = _issue_qrcode()

        if open_image:
            print("\n等待扫码确认", end="", flush=True)
        deadline = time.time() + timeout
        scanned_seen = False
        scanned_notified = False
        while time.time() < deadline:
            _check_cancelled()
            body, poll = _poll_request(client, qrcode_key)
            status = _poll_status(body)
            if status != POLL_WAITING:
                logger.info("扫码轮询 status=%s", status)
            else:
                logger.debug("扫码轮询 status=%s", status)

            if status == POLL_SUCCESS:
                _emit("confirming", "已确认，正在完成登录…")
                if open_image:
                    print("\n登录成功！")
                cookies = _collect_login_cookies(client, poll, body)
                if "SESSDATA" not in cookies or "bili_jct" not in cookies:
                    raise BilibiliLoginError("登录未完成，请重新扫码")
                verify_login_uid_for_current_profile(cookies)
                essential = {k: v for k, v in cookies.items() if k in ESSENTIAL_KEYS}
                cookie_str = cookies_to_header(essential or cookies)
                save_cookies(cookie_str)
                _sync_profile_mid(cookies)
                _emit("success", "登录成功")
                return cookie_str

            if status == POLL_EXPIRED:
                if scanned_seen:
                    raise BilibiliLoginError("扫码后确认超时，请重新发起登录")
                if auto_refresh_on_expire:
                    _emit("refreshing", "二维码已过期，正在自动刷新…")
                    _, qrcode_key = _issue_qrcode()
                    scanned_seen = False
                    scanned_notified = False
                    time.sleep(1)
                    continue
                raise BilibiliLoginError("二维码已过期，请重新发起登录")

            if status == POLL_SCANNED:
                scanned_seen = True
                if not scanned_notified:
                    scanned_notified = True
                    _emit("scanned", "扫码成功，请在手机上点击「确认登录」")
                if open_image:
                    print(".", end="", flush=True)
                time.sleep(0.3)
                continue

            if status == POLL_WAITING:
                if open_image:
                    print(".", end="", flush=True)
                time.sleep(0.5 if scanned_seen else 1)
                continue

            raise BilibiliLoginError(f"扫码状态异常（{status}），请重试")

        raise BilibiliLoginError(f"登录超时（{int(timeout)} 秒），请重试")
    finally:
        client.close()
