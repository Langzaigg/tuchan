"""QQ 开放平台直连及进程内 OneBot 消息分发。"""

import base64
import hashlib
import re
import time
from collections import OrderedDict
from contextvars import ContextVar
from datetime import datetime
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import url2pathname

import httpx
from nonebot.adapters import Adapter
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageSegment, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import (
    FriendAddNoticeEvent, GroupDecreaseNoticeEvent, GroupIncreaseNoticeEvent, GroupRequestEvent, Reply, Sender,
)
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.adapters.qq import Adapter as QQAdapter, Bot as QQBot, Message as QQMessage, MessageSegment as QQSegment
from nonebot.adapters.qq.event import (
    C2CMessageCreateEvent, Event as QQEvent, FriendAddEvent, GroupAddRobotEvent, GroupAtMessageCreateEvent, GroupDelRobotEvent, GroupJoinRequestEvent,
    GroupMemberAddEvent, GroupMemberRemoveEvent, GroupMessageCreateEvent, NoticeEvent, QQMessageEvent,
)
from nonebot.adapters.qq.models import QQReplyMessage
from nonebot.compat import type_validate_python
from nonebot.exception import AdapterException, IgnoredException
from nonebot.message import event_preprocessor


_CHAT_RECORD_TYPE = 102
_QUOTE_TYPE = 103
_RECORD_HEADER = re.compile(r"^===[ \t]*消息[ \t]+(\d+)[ \t]*===[ \t]*\r?$", re.MULTILINE)
_MAX_LOCAL_IMAGE_BYTES = 20 * 1024 * 1024


def _is_local_image_host(host: str) -> bool:
    host = host.rstrip(".").lower()
    try:
        return not ip_address(host).is_global
    except ValueError:
        return host == "localhost" or host.endswith((".localhost", ".local"))


async def _download_local_image(url: str) -> bytes:
    content = bytearray()
    try:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                if not response.headers.get("content-type", "").lower().startswith("image/"):
                    raise ActionFailed(message="本地图片地址返回了非图片内容")
                async for chunk in response.aiter_bytes():
                    if len(content) + len(chunk) > _MAX_LOCAL_IMAGE_BYTES:
                        raise ActionFailed(message="本地图片超过 20 MiB")
                    content.extend(chunk)
    except httpx.HTTPError as exc:
        raise ActionFailed(message=f"读取本地图片失败（{type(exc).__name__}）") from exc
    if not content:
        raise ActionFailed(message="本地图片内容为空")
    return bytes(content)


class _MessageElement(QQReplyMessage):
    content: str = ""
    msg_elements: list["_MessageElement"] | None = None


class _C2CMessage(C2CMessageCreateEvent):
    msg_elements: list[_MessageElement] | None = None


class _GroupMessage(GroupMessageCreateEvent):
    msg_elements: list[_MessageElement] | None = None


class _GroupAtMessage(GroupAtMessageCreateEvent):
    msg_elements: list[_MessageElement] | None = None


_MESSAGE_EVENTS = {
    "C2C_MESSAGE_CREATE": _C2CMessage,
    "GROUP_MESSAGE_CREATE": _GroupMessage,
    "GROUP_AT_MESSAGE_CREATE": _GroupAtMessage,
}


def _is_chat_record(message) -> bool:
    return message.message_type == _CHAT_RECORD_TYPE or bool(_RECORD_HEADER.match(message.content.lstrip()))


def _from_chat_record(message) -> MessageSegment | None:
    nodes = []
    seen_images = set()
    for element in message.msg_elements or [message]:
        content = element.content.strip()
        author = getattr(element, "author", None)
        nickname = getattr(author, "username", None)
        if _RECORD_HEADER.match(content):
            parts = _RECORD_HEADER.split(content)[1:]
            for number, body in zip(parts[::2], parts[1::2]):
                if text := body.strip().removeprefix("[消息内容]").lstrip():
                    nodes.append({"nickname": f"消息 {number}", "content": [MessageSegment.text(text)]})
            segments = []
        elif element is not message and element.message_type == _CHAT_RECORD_TYPE and element.msg_elements:
            segments = [MessageSegment("forward", {})]
        else:
            segments = [MessageSegment.text(content)] if content else []
        for attachment in element.attachments or ():
            if attachment.content_type.startswith("image/") and attachment.url:
                segments.append(MessageSegment.image(attachment.url))
                seen_images.add(attachment.url)
        if segments:
            nodes.append({"nickname": nickname, "content": segments})

    if message.msg_elements:
        attachments = [
            MessageSegment.image(attachment.url)
            for attachment in message.attachments or ()
            if attachment.content_type.startswith("image/") and attachment.url and attachment.url not in seen_images
        ]
        if attachments:
            nodes.append({"content": attachments})
    return MessageSegment("forward", {"content": nodes}) if nodes else None


def _quoted_chat_record(event: QQMessageEvent):
    if event.reply is not None:
        return event.reply if _is_chat_record(event.reply) else None
    if event.message_type != _QUOTE_TYPE or len(event.msg_elements or ()) != 1:
        return None
    element = event.msg_elements[0]
    if not element.msg_idx and _is_chat_record(element):
        # QQ 的合并转发引用示例只有 TMP_ 引用索引，正文元素没有 msg_idx。
        return element
    return None


def _message_index(event: QQMessageEvent, key: str) -> str:
    prefix = f"{key}="
    return next(
        (item.removeprefix(prefix) for item in (event.message_scene.ext if event.message_scene else ())
         if item.startswith(prefix)),
        "",
    )


def virtual_id(kind: str, value: str) -> int:
    digest = hashlib.sha256(f"{kind}:{value}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") >> 2) | (1 << 62)


def superuser_id(value: str) -> str:
    if value.startswith("onebot:") or (value.isascii() and value.isdecimal()):
        return value
    return str(virtual_id("user", value.removeprefix("qq:")))


def _timestamp(event: QQMessageEvent) -> int:
    try:
        return int(datetime.fromisoformat(event.timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return int(time.time())


def _destination(event: QQMessageEvent) -> tuple[str, int]:
    if isinstance(event, GroupMessageCreateEvent):
        return "group", virtual_id("group", event.group_openid)
    return "private", virtual_id("user", event.get_user_id())


class OfficialQQAdapter(QQAdapter):
    @staticmethod
    def payload_to_event(payload):
        data = payload.data
        if isinstance(data, dict) and payload.type in ("GROUP_AT_MESSAGE_CREATE", "GROUP_MESSAGE_CREATE"):
            if isinstance(author := data.get("author"), dict):
                # Older apps may receive only member_openid. Missing role grants no privileges.
                author.setdefault("bot", False)
                author.setdefault("member_role", "member")
                author.setdefault("id", author.get("member_openid"))
            data.setdefault("group_id", data.get("group_openid"))
        if isinstance(data, dict) and (model := _MESSAGE_EVENTS.get(payload.type)):
            return type_validate_python(model, {"event_id": payload.id, **data})
        return QQAdapter.payload_to_event(payload)


class QQOneBotDispatcher(Adapter):
    @classmethod
    def get_name(cls) -> str:
        return "OneBot V11"

    def __init__(self, driver, app_id: str):
        super().__init__(driver)
        self.app_id = app_id
        self.bot = Bot(self, str(virtual_id("bot", app_id)))
        self._active_event: ContextVar[QQMessageEvent | None] = ContextVar("qq_event", default=None)
        self._latest: OrderedDict[tuple[str, int], QQMessageEvent] = OrderedDict()
        self._members: OrderedDict[tuple[int, int], Sender] = OrderedDict()
        self._mention_ids: OrderedDict[tuple[int, int], str] = OrderedDict()
        self._groups: OrderedDict[int, str] = OrderedDict()
        self._messages: OrderedDict[int, dict] = OrderedDict()
        self._forward_refs: OrderedDict[tuple[str, int, str], int] = OrderedDict()
        self._join_requests: OrderedDict[str, GroupJoinRequestEvent] = OrderedDict()

        driver.on_startup(self._connect)
        driver.on_shutdown(self._disconnect)
        event_preprocessor(self._route_native_event)

    async def _connect(self) -> None:
        self.bot_connect(self.bot)

    async def _disconnect(self) -> None:
        if self.bot.self_id in self.bots:
            self.bot_disconnect(self.bot)

    async def _route_native_event(self, bot: QQBot, event: QQEvent) -> None:
        if isinstance(event, (GroupMessageCreateEvent, C2CMessageCreateEvent)):
            await self.dispatch(bot, event)
        elif isinstance(event, NoticeEvent):
            await self.dispatch_notice(bot, event)
        raise IgnoredException("QQ 原始事件已由接入层处理")

    @staticmethod
    def _remember(cache, key, value) -> None:
        cache[key] = value
        cache.move_to_end(key)
        if len(cache) > 2048:
            cache.popitem(last=False)

    def _from_qq(self, message, group_id: int | None) -> Message:
        converted = Message()
        for segment in message or ():
            data = segment.data
            if segment.type == "text":
                converted.append(MessageSegment.text(data["text"]))
            elif segment.type == "image":
                converted.append(MessageSegment.image(data["url"]))
            elif segment.type == "mention_everyone":
                converted.append(MessageSegment.at("all"))
            elif segment.type == "mention_user" and group_id is not None:
                mention = data["user_id"]
                if data.get("is_bot"):
                    converted.append(MessageSegment.at(self.bot.self_id))
                    continue
                user_id = virtual_id("user", mention)
                self._remember(self._mention_ids, (group_id, user_id), mention)
                if name := data.get("username"):
                    self._remember(self._members, (group_id, user_id), Sender(user_id=user_id, nickname=name))
                converted.append(MessageSegment.at(user_id))
        return converted

    def convert_event(self, event: QQMessageEvent):
        chat_type, chat_id = _destination(event)
        group_id = chat_id if chat_type == "group" else None
        user_id = virtual_id("user", event.get_user_id())
        sender = Sender(user_id=user_id, nickname=event.author.username or str(user_id),
                        role=getattr(event.author, "member_role", None))
        if group_id is not None:
            self._remember(self._groups, group_id, event.group_openid)
            self._remember(self._members, (group_id, user_id), sender)
            self._remember(self._mention_ids, (group_id, user_id), event.get_user_id())
        message = self._from_qq(event.get_message(), group_id)
        if event.message_type == _CHAT_RECORD_TYPE and (forward := _from_chat_record(event)):
            message = Message(forward)
        time_sent = _timestamp(event)
        reply = None
        record = _quoted_chat_record(event)
        quoted = record or event.reply
        if quoted is not None:
            author = quoted.author
            openid = (author.member_openid or author.user_openid or author.id) if author else None
            reply_id = virtual_id("message", quoted.msg_idx or event.id)
            reply_sender = Sender(
                user_id=virtual_id("user", openid) if openid else None,
                nickname=author.username if author else None,
            )
            quoted_message = self._from_qq(event.get_reply_message(), group_id)
            if record is not None and (forward := _from_chat_record(record)):
                quoted_message = Message(forward)
            reply = Reply(time=time_sent, message_type=chat_type, message_id=reply_id, real_id=reply_id,
                          sender=reply_sender, message=quoted_message)
        if (reply is None or not reply.message["forward"]) and event.message_type == _QUOTE_TYPE:
            ref = _message_index(event, "ref_msg_idx")
            record_id = self._forward_refs.get((chat_type, chat_id, ref))
            if record_id is not None and (cached := self._messages.get(record_id)):
                reply = Reply(**cached)
        if reply is not None and reply.message["forward"] and not message.extract_plain_text() and not message["image"]:
            message.append(MessageSegment.text(" "))
        common = dict(time=time_sent, self_id=int(self.bot.self_id), post_type="message",
                      message_type=chat_type, message_id=virtual_id("message", event.id),
                      user_id=user_id, sender=sender, message=message, original_message=message,
                      raw_message=str(message), font=0, to_me=event.is_tome(), reply=reply)
        if group_id is not None:
            return GroupMessageEvent(sub_type="normal", group_id=group_id, **common)
        return PrivateMessageEvent(sub_type="friend", **common)

    async def dispatch(self, bot: QQBot, event: QQMessageEvent) -> None:
        if bot.self_id != self.app_id or getattr(event.author, "bot", False):
            return
        destination = _destination(event)
        self._remember(self._latest, destination, event)
        converted = self.convert_event(event)
        self._remember(self._messages, converted.message_id, {
            "time": converted.time, "message_id": converted.message_id, "real_id": converted.message_id,
            "message_type": converted.message_type, "sender": converted.sender.dict(),
            "message": converted.original_message,
        })
        if converted.original_message["forward"] and (index := _message_index(event, "msg_idx")):
            self._remember(self._forward_refs, (*destination, index), converted.message_id)
        token = self._active_event.set(event)
        try:
            await self.bot.handle_event(converted)
        finally:
            self._active_event.reset(token)

    async def dispatch_notice(self, bot: QQBot, event: NoticeEvent) -> None:
        if bot.self_id != self.app_id:
            return
        if isinstance(event, FriendAddEvent):
            notice = FriendAddNoticeEvent(time=int(event.timestamp.timestamp()), self_id=int(self.bot.self_id),
                                          post_type="notice", notice_type="friend_add",
                                          user_id=virtual_id("user", event.get_user_id()))
            await self.bot.handle_event(notice)
            return
        if not isinstance(event, (GroupAddRobotEvent, GroupDelRobotEvent, GroupMemberAddEvent,
                                  GroupMemberRemoveEvent, GroupJoinRequestEvent)):
            return
        group_id = virtual_id("group", event.group_openid)
        if isinstance(event, GroupDelRobotEvent):
            self._groups.pop(group_id, None)
            self._latest.pop(("group", group_id), None)
            return
        self._remember(self._groups, group_id, event.group_openid)
        if isinstance(event, GroupAddRobotEvent):
            return
        user_id = virtual_id("user", event.get_user_id())
        if isinstance(event, GroupJoinRequestEvent):
            if event.auto_approved or event.bot:
                return
            self._remember(self._join_requests, event.join_request_id, event)
            notice = GroupRequestEvent(time=int(event.apply_at.timestamp()), self_id=int(self.bot.self_id),
                                       post_type="request", request_type="group", sub_type="add",
                                       user_id=user_id, group_id=group_id, flag=event.join_request_id, comment="")
        else:
            arrived = isinstance(event, GroupMemberAddEvent)
            cls = GroupIncreaseNoticeEvent if arrived else GroupDecreaseNoticeEvent
            notice = cls(time=int(event.timestamp.timestamp()), self_id=int(self.bot.self_id),
                         post_type="notice", notice_type="group_increase" if arrived else "group_decrease",
                         sub_type="approve" if arrived else "leave", group_id=group_id,
                         user_id=user_id, operator_id=user_id)
        await self.bot.handle_event(notice)

    def _reply_event(self, destination: tuple[str, int]) -> QQMessageEvent:
        current = self._active_event.get()
        max_age = 300 if destination[0] == "group" else 3600
        for event in (current, self._latest.get(destination)):
            if (event is not None and _destination(event) == destination
                    and event._reply_seq < 5 and time.time() - _timestamp(event) < max_age):
                return event
        raise ActionFailed(message="没有可用的 QQ 被动回复事件")

    async def _to_qq(self, message, group_id: int | None) -> QQMessage:
        outgoing = QQMessage()
        for segment in Message(message):
            data = segment.data
            if segment.type == "text":
                outgoing.append(QQSegment.text(data["text"]))
            elif segment.type == "image":
                source = data["file"]
                if source.startswith(("http://", "https://")):
                    host = urlsplit(source).hostname or ""
                    if _is_local_image_host(host):
                        outgoing.append(QQSegment.file_image(await _download_local_image(source)))
                    else:
                        outgoing.append(QQSegment.image(source))
                elif source.startswith("base64://"):
                    outgoing.append(QQSegment.file_image(base64.b64decode(source.removeprefix("base64://"))))
                else:
                    uri = urlsplit(source)
                    path = url2pathname(f"//{uri.netloc}{uri.path}" if uri.netloc else uri.path) if uri.scheme == "file" else source
                    outgoing.append(QQSegment.file_image(Path(path)))
            elif segment.type == "at" and group_id is not None:
                target = str(data["qq"])
                if target == "all":
                    outgoing.append(QQSegment.mention_everyone())
                elif target.isdecimal() and (openid := self._mention_ids.get((group_id, int(target)))):
                    outgoing.append(QQSegment.mention_user(openid))
        if not outgoing:
            raise ApiNotAvailable
        return outgoing

    async def _call_api(self, bot: Bot, api: str, **data):
        if api == "get_login_info":
            return {"user_id": int(bot.self_id), "nickname": next(iter(sorted(bot.config.nickname)), "")}
        if api == "get_msg":
            if result := self._messages.get(int(data["message_id"])):
                return result
            raise ActionFailed(message="消息不在本地事件缓存中")
        if api == "get_group_list":
            return [{"group_id": group_id, "group_name": str(group_id)} for group_id in self._groups]
        if api == "get_group_member_info":
            user_id = int(data["user_id"])
            sender = self._members.get((int(data["group_id"]), user_id))
            return {"user_id": user_id, "nickname": sender.nickname if sender else str(user_id),
                    "role": sender.role if sender and sender.role else "member", "card": ""}

        native = self.driver.bots.get(self.app_id)
        if not isinstance(native, QQBot):
            raise NetworkError("QQ 官方机器人未连接")
        try:
            return await self._call_qq(native, api, data)
        except AdapterException as exc:
            if isinstance(exc, (ApiNotAvailable, ActionFailed)):
                raise
            raise ActionFailed(message=str(exc)) from exc

    async def _call_qq(self, native: QQBot, api: str, data: dict):
        if api == "get_group_info":
            group_id = int(data["group_id"])
            if not (openid := self._groups.get(group_id)):
                raise ActionFailed(message="未收到该群的 QQ 官方事件")
            info = await native.get_group_info(group_id=openid)
            return {"group_id": group_id, "group_name": info.group_name, "member_count": info.group_member_num}
        if api == "get_group_member_list":
            group_id = int(data["group_id"])
            if not (openid := self._groups.get(group_id)):
                raise ActionFailed(message="未收到该群的 QQ 官方事件")
            members, cursor = [], None
            while True:
                page = await native.get_group_members(group_openid=openid, cursor=cursor)
                members.extend({"group_id": group_id, "user_id": virtual_id("user", member.member_openid),
                                "nickname": member.username, "card": "", "role": member.member_role or "member"}
                               for member in page.members)
                if not page.next_cursor or page.next_cursor == cursor:
                    return members
                cursor = page.next_cursor
        if api == "set_group_add_request" and (event := self._join_requests.get(data["flag"])):
            return await native.approval_join_request(
                group_id=event.group_openid, member_openid=event.member_openid,
                join_request_id=event.join_request_id, op="approve" if data.get("approve") else "decline",
                reject_reason=data.get("reason") or None,
            )
        if api not in ("send_msg", "send_group_msg", "send_private_msg", "upload_group_file", "upload_private_file"):
            raise ApiNotAvailable
        group = api in ("send_group_msg", "upload_group_file") or (
            api == "send_msg" and data.get("message_type", "group" if data.get("group_id") is not None else "private") == "group"
        )
        target = int(data["group_id"] if group else data["user_id"])
        if api.startswith("upload_"):
            message = QQSegment.file_file(Path(data["file"]), file_name=data.get("name"))
        else:
            message = await self._to_qq(data["message"], target if group else None)
        try:
            event = self._reply_event(("group" if group else "private", target))
        except ActionFailed:
            openid = self._groups.get(target) if group else None
            if not openid or not (await native.get_group_bot_state(group_id=openid)).allow_proactive_msg:
                raise
            response = await native.send_to_group(group_openid=openid, message=message)
            response_id = getattr(response, "id", None)
            return {"message_id": virtual_id("message", response_id)} if response_id else {}
        response = await native.send(event, message)
        response_id = getattr(response, "id", None) or f"{event.id}:{event._reply_seq}"
        return {"message_id": virtual_id("message", response_id)}
