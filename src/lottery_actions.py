from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, Literal

import httpx

from src.bilibili_auth import get_login_uid, require_login
from src.bilibili_client import BilibiliClient, api_code
from src.lottery_api import fetch_dynamic_detail, fetch_notice_for_interact, fetch_opus_detail_item
from src.sources.common import opus_link
from src.participation_guard import (
    ParticipationGuardBlocked,
    confirm_repost,
    get_guard,
    mark_repost_unknown,
    record_pending,
)

ActionName = Literal["like", "follow", "favorite", "repost", "comment", "reserve"]

DEFAULT_PARTICIPATE_TEXT = "好运连连！"

LIKE_URL = "https://api.bilibili.com/x/dynamic/feed/dyn/thumb"
FOLLOW_URL = "https://api.bilibili.com/x/relation/modify"
RELATION_URL = "https://api.bilibili.com/x/relation"
FAV_LIST_URL = "https://api.bilibili.com/x/v3/fav/folder/created/list-all"
FAV_RESOURCE_LIST_URL = "https://api.bilibili.com/x/v3/fav/resource/list"
FAV_DEAL_URL = "https://api.bilibili.com/x/v3/fav/resource/deal"
COSMO_SIMPLE_ACTION_URL = "https://api.bilibili.com/x/community/cosmo/interface/simple_action"
REPOST_URL = "https://api.vc.bilibili.com/dynamic_repost/v1/dynamic_repost/repost"
COMMENT_URL = "https://api.bilibili.com/x/v2/reply/add"
REPLY_MAIN_URL = "https://api.bilibili.com/x/v2/reply/main"

FAV_CONTENT_TYPE = 24
FOLLOWING_ATTRIBUTES = {2, 6}
ACTION_INTERVAL_SEC = 1.5
ACTION_LABELS: dict[ActionName, str] = {
    "like": "点赞",
    "follow": "关注",
    "favorite": "收藏",
    "repost": "转发",
    "comment": "评论",
    "reserve": "预约",
}
PARTICIPATION_STEPS: tuple[ActionName, ...] = ("like", "follow", "favorite", "repost", "comment")
_UNSET = object()


class ParticipationReadError(RuntimeError):
    """无法可靠确认当前目标的交互状态；本次参与不得继续写操作。"""


class ParticipationReadClient:
    """一次参与的只读快照：相同 GET 只发一次，失败不重试、不降级继续探测。"""

    def __init__(self, client: BilibiliClient) -> None:
        self.raw_client = client
        self._responses: dict[tuple, Any] = {}
        self._failure: Exception | None = None

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def _read(self, kind: str, url: str, params: dict | None, referer: str | None) -> Any:
        self.raise_if_failed()
        key = (kind, url, json.dumps(params or {}, sort_keys=True), referer)
        if key in self._responses:
            return self._responses[key]
        try:
            if kind == "text":
                payload = self.raw_client.get_text(url, referer=referer, retries=0)
            else:
                payload = self.raw_client.request_json(url, params, referer=referer, retries=0)
                if not isinstance(payload, dict) or type(payload.get("code")) is not int:
                    raise ParticipationReadError("交互状态响应缺少有效 code，已停止本次参与")
                if payload["code"] != 0:
                    raise ParticipationReadError(
                        f"交互状态读取失败 code={payload['code']}，已停止本次参与且不重试"
                    )
        except (httpx.HTTPError, RuntimeError, json.JSONDecodeError) as exc:
            if isinstance(exc, RuntimeError) and not isinstance(exc, ParticipationReadError):
                if not isinstance(exc.__cause__, (httpx.HTTPError, json.JSONDecodeError)):
                    self._failure = exc
                    raise
            self._failure = (
                exc if isinstance(exc, ParticipationReadError)
                else ParticipationReadError(f"交互状态读取失败，已停止本次参与：{exc}")
            )
            if self._failure is exc:
                raise
            raise self._failure from exc
        except Exception as exc:
            # 上层旧 API 会捕获异常后尝试降级；保留并重新抛出，不能被其吞掉。
            self._failure = exc
            raise
        self._responses[key] = payload
        return payload

    def request_json(
        self, url: str, params: dict | None = None, *, referer: str | None = None, retries: int = 0
    ) -> dict:
        return self._read("json", url, params, referer)

    def get_json(
        self, url: str, params: dict | None = None, *, referer: str | None = None,
        retries: int = 0, wbi: bool = False,
    ) -> dict:
        if wbi:
            raise ValueError("参与状态快照不支持 WBI 请求")
        return self.request_json(url, params, referer=referer)

    def get_text(self, url: str, *, referer: str | None = None, retries: int = 0) -> str:
        return self._read("text", url, None, referer)

    def post_form(self, *args: Any, **kwargs: Any) -> dict:
        self.raise_if_failed()
        return self.raw_client.post_form(*args, **kwargs)

    def post_json(self, *args: Any, **kwargs: Any) -> dict:
        self.raise_if_failed()
        return self.raw_client.post_json(*args, **kwargs)


def _interaction_flag(value: Any) -> bool | None:
    if type(value) is bool:
        return value
    if type(value) is int and value in (0, 1):
        return value == 1
    return None


def _stat_flag(stat: dict, name: str) -> bool | None:
    entry = stat.get(name)
    return _interaction_flag(entry.get("status")) if isinstance(entry, dict) else None


@dataclass
class ActionResult:
    action: ActionName
    ok: bool
    detail: str = ""


@dataclass
class DynamicContext:
    dynamic_id: str
    sender_uid: int
    referer: str
    comment_rid: str
    comment_type: int
    liked: bool | None
    favorited: bool | None
    favorite_available: bool
    followed: bool
    reposted: bool | None
    commented: bool


def _api_code(payload: dict) -> int:
    code = payload.get("code")
    if code is None:
        return -1
    try:
        return int(code)
    except (TypeError, ValueError):
        return -1


def _api_message(payload: dict) -> str:
    return str(payload.get("message") or payload.get("msg") or "").strip()


def _default_fav_folder_id(client: BilibiliClient, *, uid: int, referer: str) -> str:
    payload = client.get_json(FAV_LIST_URL, params={"up_mid": uid}, referer=referer)
    folders = (payload.get("data") or {}).get("list") or []
    if not folders:
        raise RuntimeError("未找到可用收藏夹")
    folder = folders[0]
    folder_id = folder.get("id") or folder.get("fid")
    if not folder_id:
        raise RuntimeError("收藏夹 ID 解析失败")
    return str(folder_id)


def resolve_sender_uid(item: dict) -> int:
    modules = item.get("modules") or {}
    author = modules.get("module_author") or {}
    for key in ("mid", "uid", "up_mid"):
        value = author.get(key)
        if value:
            return int(value)
    raise RuntimeError("无法从动态详情解析 UP 主 UID")


def _extract_module_stat(item: dict) -> dict:
    modules = item.get("modules") or {}
    if isinstance(modules, dict):
        stat = modules.get("module_stat") or {}
        return stat if isinstance(stat, dict) else {}
    if isinstance(modules, list):
        for module in modules:
            if not isinstance(module, dict):
                continue
            if module.get("module_type") != "MODULE_TYPE_STAT" and not module.get("module_stat"):
                continue
            stat = module.get("module_stat") or {}
            if isinstance(stat, dict):
                return stat
    return {}


def _module_stat_confirms_no_favorite(stat: dict) -> bool:
    """同时有转发与评论统计但无 favorite，对应 B 站页面上无收藏按钮的动态。"""
    if not stat or "favorite" in stat:
        return False
    has_forward = any(key in stat for key in ("forward", "repost", "share"))
    has_comment = "comment" in stat
    return has_forward and has_comment


def favorite_supported(
    item: dict,
    *,
    client: BilibiliClient | None = None,
    dynamic_id: str | None = None,
) -> bool:
    """动态是否支持收藏。不确定时默认尝试收藏，避免 dynamic/detail 缺字段导致误判。"""
    stat = _extract_module_stat(item)
    if stat and "favorite" in stat:
        return True
    if stat and _module_stat_confirms_no_favorite(stat):
        if client is not None and dynamic_id:
            opus_item = fetch_opus_detail_item(client, dynamic_id)
            if opus_item:
                opus_stat = _extract_module_stat(opus_item)
                if opus_stat and "favorite" in opus_stat:
                    return True
        return False
    return True


def build_dynamic_context(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    action_text: str,
    sender_uid: int | None = None,
    detail_item: dict | None = None,
    notice: dict | None | object = _UNSET,
    repost_confirmed: bool = False,
    check_comment: bool = True,
    check_follow: bool = True,
) -> DynamicContext:
    client = client if isinstance(client, ParticipationReadClient) else ParticipationReadClient(client)
    item = detail_item if detail_item is not None else fetch_dynamic_detail(client, dynamic_id)
    client.raise_if_failed()
    if not item:
        raise ParticipationReadError("无法获取动态详情")

    referer = opus_link(dynamic_id)
    basic = item.get("basic") or {}
    comment_rid = str(basic.get("comment_id_str") or dynamic_id)
    comment_type = int(basic.get("comment_type") or 17)
    uid = sender_uid or resolve_sender_uid(item)

    stat = _extract_module_stat(item)
    liked = _stat_flag(stat, "like")
    if liked is None:
        raise ParticipationReadError("当前目标的点赞状态未知，已停止本次参与")

    followed = is_following(client, uid=uid, referer=referer) if check_follow else False
    favorite_available = favorite_supported(item, client=client, dynamic_id=dynamic_id)
    client.raise_if_failed()
    favorited = _stat_flag(stat, "favorite")
    if favorite_available and favorited is None:
        favorited = is_favorited(client, dynamic_id=dynamic_id, referer=referer)
    client.raise_if_failed()
    if favorite_available and favorited is None:
        raise ParticipationReadError("当前目标的收藏状态未知，已停止本次参与")
    reposted = True if repost_confirmed else is_reposted(
        client, dynamic_id=dynamic_id, referer=referer, notice=notice
    )
    client.raise_if_failed()
    commented = False
    if check_comment:
        commented = has_comment(
            client,
            rid=comment_rid,
            comment_type=comment_type,
            action_text=action_text,
            referer=referer,
        )
        client.raise_if_failed()

    return DynamicContext(
        dynamic_id=dynamic_id,
        sender_uid=uid,
        referer=referer,
        comment_rid=comment_rid,
        comment_type=comment_type,
        liked=liked,
        favorited=favorited,
        favorite_available=favorite_available,
        followed=followed,
        reposted=reposted,
        commented=commented,
    )


def is_following(client: BilibiliClient, *, uid: int, referer: str) -> bool:
    try:
        payload = client.request_json(RELATION_URL, params={"fid": uid}, referer=referer)
    except RuntimeError:
        return False
    if api_code(payload) != 0:
        return False
    attribute = int((payload.get("data") or {}).get("attribute") or 0)
    return attribute in FOLLOWING_ATTRIBUTES


def _opus_favorite_status(client: BilibiliClient, *, dynamic_id: str, referer: str) -> bool | None:
    try:
        payload = client.get_json(
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/detail",
            params={
                "id": dynamic_id,
                "features": "htmlNewStyle,ugcDelete,editable,opusPrivateVisible",
            },
            referer=referer,
            retries=0,
        )
    except (RuntimeError, httpx.HTTPError, json.JSONDecodeError):
        return None
    if _api_code(payload) != 0:
        return None
    item = (payload.get("data") or {}).get("item") or {}
    return _stat_flag(_extract_module_stat(item), "favorite")


def is_favorited(client: BilibiliClient, *, dynamic_id: str, referer: str) -> bool | None:
    return _opus_favorite_status(client, dynamic_id=dynamic_id, referer=referer)


def is_reposted(
    client: BilibiliClient, *, dynamic_id: str, referer: str, notice: dict | None | object = _UNSET
) -> bool | None:
    """只接受当前目标的官方抽奖字段；普通动态不扫描账号历史来补偿。"""
    client = client if isinstance(client, ParticipationReadClient) else ParticipationReadClient(client)
    if notice is _UNSET:
        resolved = fetch_notice_for_interact(client, dynamic_id)
        client.raise_if_failed()
        notice = resolved[0] if resolved else None
    if isinstance(notice, dict):
        return _interaction_flag(notice.get("reposted"))
    return None


def has_comment(
    client: BilibiliClient,
    *,
    rid: str,
    comment_type: int,
    action_text: str,
    referer: str,
) -> bool:
    uid = get_login_uid()
    if not uid:
        return False
    try:
        payload = client.request_json(
            REPLY_MAIN_URL,
            params={"oid": rid, "type": comment_type, "mode": 3, "next": 0, "ps": 20},
            referer=referer,
        )
    except RuntimeError:
        return False
    if api_code(payload) != 0:
        return False
    replies = (payload.get("data") or {}).get("replies") or []
    target = action_text.strip()
    for reply in replies:
        member = reply.get("member") or {}
        if str(member.get("mid")) != str(uid):
            continue
        content = reply.get("content") or {}
        message = str(content.get("message") or "")
        if target and target in message:
            return True
    return False


def like_dynamic(client: BilibiliClient, *, dynamic_id: str, csrf: str, referer: str) -> ActionResult:
    payload = client.post_json(
        LIKE_URL,
        {
            "dyn_id_str": dynamic_id,
            "up": 1,
            "spmid": "333.1369.0.0",
            "from_spmid": "333.999.0.0",
        },
        params={"csrf": csrf},
        referer=referer,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("like", True, "")
    if code == 65006:
        return ActionResult("like", True, "已赞过")
    return ActionResult("like", False, f"code={code} {_api_message(payload)}".strip())


def follow_user(client: BilibiliClient, *, uid: int, csrf: str, referer: str) -> ActionResult:
    payload = client.post_form(
        FOLLOW_URL,
        {"fid": uid, "act": 1, "re_src": 11, "csrf": csrf},
        referer=referer,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("follow", True, f"uid={uid}")
    if code == 22014:
        return ActionResult("follow", True, f"uid={uid} 已关注")
    return ActionResult("follow", False, f"code={code} {_api_message(payload)}".strip())


def favorite_dynamic(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    csrf: str,
    referer: str,
    known_status: bool | None | object = _UNSET,
) -> ActionResult:
    client = client.raw_client if isinstance(client, ParticipationReadClient) else client
    status = (
        is_favorited(client, dynamic_id=dynamic_id, referer=referer)
        if known_status is _UNSET else known_status
    )
    if status is True:
        return ActionResult("favorite", True, "已收藏，跳过")
    if status is not False:
        raise ParticipationReadError("当前目标的收藏状态未知，未执行收藏")

    payload = client.post_json(
        COSMO_SIMPLE_ACTION_URL,
        {
            "meta": {
                "spmid": "444.42.0.0",
                "from_spmid": "333.1365.0.0",
                "from": "unknown",
            },
            "entity": {
                "object_id_str": dynamic_id,
                "type": {"biz": 2},
            },
            "action": 3,
        },
        params={"csrf": csrf},
        referer=referer,
        raise_on_code=False,
        retries=0,
    )
    code = _api_code(payload)
    message = _api_message(payload)
    if code == 0:
        if is_favorited(client, dynamic_id=dynamic_id, referer=referer) is True:
            return ActionResult("favorite", True, "")
        return ActionResult("favorite", False, message or "收藏状态未确认，未继续查询")
    if code in (65006, 75008):
        return ActionResult("favorite", True, message or "已收藏")
    return ActionResult("favorite", False, f"code={code} {message}".strip())


def repost_dynamic(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    my_uid: int,
    csrf: str,
    referer: str,
    content: str,
) -> ActionResult:
    guard = get_guard(my_uid, dynamic_id)
    if guard is not None:
        if guard.repost_status == "confirmed":
            return ActionResult("repost", True, "本地已确认转发，跳过")
        return ActionResult("repost", False, "已有转发保护记录，未再次发送")
    try:
        record_pending(my_uid, dynamic_id)
    except ParticipationGuardBlocked:
        return ActionResult("repost", False, "已有转发保护记录，未再次发送")

    client = client.raw_client if isinstance(client, ParticipationReadClient) else client
    try:
        payload = client.post_form(
            REPOST_URL,
            {
                "uid": str(my_uid),
                "dynamic_id": dynamic_id,
                "content": content[:233],
                "ctrl": "[]",
                "csrf": csrf,
            },
            referer=referer,
            raise_on_code=False,
            retries=0,
        )
    except Exception as exc:
        # 调用已开始，异常不能证明服务端没有转发。先持久化不确定结果，
        # 再区分可报告的传输失败与必须继续抛出的未知程序异常。
        mark_repost_unknown(my_uid, dynamic_id)
        if isinstance(exc, (httpx.HTTPError, json.JSONDecodeError)) or (
            isinstance(exc, RuntimeError)
            and isinstance(exc.__cause__, (httpx.HTTPError, json.JSONDecodeError))
        ):
            return ActionResult("repost", False, f"转发结果未知，已阻止自动重试：{exc}")
        raise
    if isinstance(payload, dict) and type(payload.get("code")) is int and payload["code"] == 0:
        confirm_repost(my_uid, dynamic_id)
        return ActionResult("repost", True, content[:80])
    mark_repost_unknown(my_uid, dynamic_id)
    detail = f"code={_api_code(payload)} {_api_message(payload)}" if isinstance(payload, dict) else "无效响应"
    return ActionResult("repost", False, f"转发结果未确认，已阻止自动重试：{detail}".strip())


def comment_dynamic(
    client: BilibiliClient,
    *,
    rid: str,
    comment_type: int,
    message: str,
    csrf: str,
    referer: str,
) -> ActionResult:
    payload = client.post_form(
        COMMENT_URL,
        {"oid": rid, "type": comment_type, "message": message, "csrf": csrf},
        referer=referer,
        raise_on_code=False,
    )
    code = _api_code(payload)
    if code == 0:
        return ActionResult("comment", True, message[:80])
    if code == 12051:
        return ActionResult("comment", True, "已有相同评论")
    return ActionResult("comment", False, f"code={code} {_api_message(payload)}".strip())


def execute_full_participation(
    client: BilibiliClient,
    *,
    dynamic_id: str,
    sender_uid: int | None = None,
    action_text: str = DEFAULT_PARTICIPATE_TEXT,
    dry_run: bool = False,
    on_step: Callable[[int, int, str, ActionName], None] | None = None,
    context: DynamicContext | None = None,
    on_action: Callable[[ActionResult], None] | None = None,
) -> tuple[list[ActionResult], DynamicContext]:
    text = (action_text or DEFAULT_PARTICIPATE_TEXT).strip() or DEFAULT_PARTICIPATE_TEXT
    if context is None:
        context = build_dynamic_context(
            client,
            dynamic_id=dynamic_id,
            action_text=text,
            sender_uid=sender_uid,
        )
    if context.dynamic_id != dynamic_id:
        raise ValueError("参与状态快照与目标动态不一致")
    if context.liked is None or (context.favorite_available and context.favorited is None):
        raise ParticipationReadError("当前目标的交互状态未知，未执行参与操作")
    client = client.raw_client if isinstance(client, ParticipationReadClient) else client

    total_steps = len(PARTICIPATION_STEPS)

    def report_step(step_index: int, action_name: ActionName, detail: str = "") -> None:
        if not on_step:
            return
        label = ACTION_LABELS.get(action_name, action_name)
        message = f"正在{label}（{step_index}/{total_steps}）"
        if detail:
            message = f"{message} · {detail}"
        on_step(step_index, total_steps, message, action_name)

    if dry_run:
        return [
            ActionResult("like", True, "跳过" if context.liked else "将点赞"),
            ActionResult("follow", True, "跳过" if context.followed else f"将关注 uid={context.sender_uid}"),
            ActionResult(
                "favorite",
                True,
                "跳过"
                if context.favorited
                else ("无收藏入口，跳过" if not context.favorite_available else f"将收藏 rid={context.comment_rid}"),
            ),
            ActionResult("repost", True, "跳过" if context.reposted else f"将转发 {text[:40]}"),
            ActionResult(
                "comment",
                True,
                "跳过" if context.commented else f"将评论 type={context.comment_type}",
            ),
        ], context

    csrf, my_uid = require_login()
    actions: list[ActionResult] = []

    def append_action(action: ActionResult) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    report_step(1, "like")
    if context.liked:
        append_action(ActionResult("like", True, "已点赞，跳过"))
    else:
        append_action(like_dynamic(client, dynamic_id=dynamic_id, csrf=csrf, referer=context.referer))
    time.sleep(ACTION_INTERVAL_SEC)

    report_step(2, "follow")
    if context.followed:
        append_action(ActionResult("follow", True, f"uid={context.sender_uid} 已关注，跳过"))
    else:
        append_action(follow_user(client, uid=context.sender_uid, csrf=csrf, referer=context.referer))
    time.sleep(ACTION_INTERVAL_SEC)

    report_step(3, "favorite")
    if not context.favorite_available:
        append_action(ActionResult("favorite", True, "无收藏入口，跳过"))
    elif context.favorited:
        append_action(ActionResult("favorite", True, "已收藏，跳过"))
    else:
        append_action(
            favorite_dynamic(
                client, dynamic_id=dynamic_id, csrf=csrf, referer=context.referer,
                known_status=context.favorited,
            )
        )
    time.sleep(ACTION_INTERVAL_SEC)

    report_step(4, "repost")
    if context.reposted:
        append_action(ActionResult("repost", True, "已转发，跳过"))
    else:
        append_action(
            repost_dynamic(
                client,
                dynamic_id=dynamic_id,
                my_uid=my_uid,
                csrf=csrf,
                referer=context.referer,
                content=text,
            )
        )
    time.sleep(ACTION_INTERVAL_SEC)

    report_step(5, "comment")
    if context.commented:
        append_action(ActionResult("comment", True, "已评论，跳过"))
    else:
        append_action(
            comment_dynamic(
                client,
                rid=context.comment_rid,
                comment_type=context.comment_type,
                message=text,
                csrf=csrf,
                referer=context.referer,
            )
        )

    return actions, context
