# -*- coding: utf-8 -*-
"""Dice! 骰子插件（NoneBot2 / OneBot v11）

一比一复刻 Dice-newdev 的指令层：掷骰 / 检定 / 人物卡 / 规则速查 / jrrp /
先攻 / 骰池 / 游戏桌 / 旁观 / 安科 等。
"""

import asyncio
import re
import time
from typing import Optional, Tuple

from nonebot import get_plugin_config, on_message
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
    PrivateMessageEvent,
)
from nonebot.exception import StopPropagation
from nonebot.plugin import PluginMetadata
from nonebot.rule import Rule

from .config import Config

__plugin_meta__ = PluginMetadata(
    name="Dice!",
    description="TRPG 骰子机器人（Dice-newdev 指令层一比一复刻）",
    usage="发送 .help 查看指令列表；掷骰用 .r",
    type="application",
    config=Config,
    extra={"author": "langzaigg", "version": "2.7.1beta1(668)"},
)

plugin_config = get_plugin_config(Config)

AT_RE = re.compile("\x02at:([^\x02]*)\x02")


def _self_name() -> str:
    """骰娘自称：config.yml → DICE_SELF_NAME → NoneBot 的 NICKNAME → 兜底。

    不写死名字。NoneBot 把 `NICKNAME` 解析成 **set**（无顺序），所以这里按名字排序取
    第一个，多个昵称时结果稳定。
    """
    from .settings import settings
    if settings.self_name:
        return settings.self_name
    if plugin_config.dice_self_name:
        return plugin_config.dice_self_name
    try:
        from nonebot import get_driver
        raw = getattr(get_driver().config, "nickname", None)
        if isinstance(raw, str):
            if raw:
                return raw
        elif raw:
            names = sorted(str(n) for n in raw if str(n))
            if names:
                return names[0]
    except Exception:
        pass
    return "骰娘"


def _extract(event: MessageEvent) -> Tuple[str, bool, bool]:
    """拆出正文与 at 状态，返回 (文本, 是否点名自己, 是否点了别人)。

    正文保留 CQ 码形式（图片 / 表情 / 戳一戳 / 文件 / 行内 at），
    与 C++ 版拿到的原始消息串一致，供指令解析与日志记录共用。
    """
    self_id = str(event.self_id)
    summoned = False
    other = False
    idx = 0
    message = event.message
    while idx < len(message):
        seg = message[idx]
        if seg.type == "at":
            qq = str(seg.data.get("qq", ""))
            if qq == "all" or qq == self_id:
                summoned = True
            else:
                other = True
            idx += 1
            continue
        if seg.type == "reply":
            idx += 1
            continue
        if seg.type == "text" and not str(seg.data.get("text", "")).strip() \
                and idx + 1 < len(message) and message[idx + 1].type in ("at", "reply"):
            idx += 1
            continue
        break
    parts = []
    for seg in message[idx:]:
        if seg.type == "text":
            parts.append(str(seg.data.get("text", "")))
        elif seg.type == "at":
            parts.append("[CQ:at,qq=%s]" % seg.data.get("qq", ""))
        elif seg.type == "image":
            parts.append("[CQ:image,file=%s]" % seg.data.get("file", ""))
        elif seg.type == "face":
            parts.append("[CQ:face,id=%s]" % seg.data.get("id", ""))
        elif seg.type == "poke":
            parts.append("[CQ:poke,qq=%s]" % seg.data.get("qq", ""))
        elif seg.type == "file":
            parts.append("[CQ:file,file=%s]" % seg.data.get("name", seg.data.get("file", "")))
    return "".join(parts), summoned, other


def _has_prefix(text: str) -> bool:
    s = text.lstrip()
    return bool(s) and s[0] in plugin_config.dice_prefix


async def _rule(event: MessageEvent) -> bool:
    if not isinstance(event, (GroupMessageEvent, PrivateMessageEvent)):
        return False
    text, summoned, _ = _extract(event)
    if _has_prefix(text):
        if isinstance(event, GroupMessageEvent) and not summoned and not plugin_config.dice_listen_at:
            return False
        return True
    # 日志记录期间需要旁听普通聊天
    from .session import sessions
    if isinstance(event, GroupMessageEvent):
        game = sessions.get_if(int(event.group_id), int(event.user_id))
    else:
        game = sessions.get_if(0, int(event.user_id))
    return game is not None and game.is_logging()


dice_matcher = on_message(rule=Rule(_rule), priority=1, block=False)


def _superusers() -> set:
    """NoneBot 全局主人（`SUPERUSERS`），对应原版的 Master / 高信任用户。"""
    global _SUPERUSERS
    if _SUPERUSERS is None:
        users = set()
        try:
            from nonebot import get_driver
            raw = getattr(get_driver().config, "superusers", None) or set()
            users = {int(u) for u in raw if str(u).lstrip("-").isdigit()}
        except Exception:
            users = set()
        _SUPERUSERS = users
    return _SUPERUSERS


_SUPERUSERS = None
_ROLE_CACHE: dict = {}
_ROLE_TTL = 300


async def _group_role(bot: Bot, event: GroupMessageEvent) -> str:
    """取发言者在群里的身份；事件没带 role 时查一次成员信息并缓存。"""
    role = getattr(getattr(event, "sender", None), "role", "") or ""
    if role:
        return role
    key = (int(event.group_id), int(event.user_id))
    hit = _ROLE_CACHE.get(key)
    now = time.time()
    if hit and now - hit[1] < _ROLE_TTL:
        return hit[0]
    try:
        info = await bot.get_group_member_info(
            group_id=int(event.group_id), user_id=int(event.user_id), no_cache=False)
        role = str((info or {}).get("role", "") or "")
    except Exception:
        role = ""
    _ROLE_CACHE[key] = (role, now)
    return role


async def _can_host(bot: Bot, event: MessageEvent, is_master: bool) -> bool:
    """对应原版 `DiceEvent::canRoomHost()`。

    原式：`trusted > 3 || isChannel() || isPrivate() || isGroupAdmin() || inviter == uid`。
    本移植：主人（NoneBot `SUPERUSERS`）或群主/管理员放行，私聊恒真；
    不引入信任分级，也不记邀请人（原版靠它给拉骰娘进群的人临时权限）。
    """
    if is_master:
        return True
    if isinstance(event, PrivateMessageEvent):
        return True
    if not isinstance(event, GroupMessageEvent):
        return False
    return await _group_role(bot, event) in ("owner", "admin")


def _to_message(text: str) -> Message:
    """把内部 at 占位符还原成真正的 at 段。"""
    msg = Message()
    pos = 0
    for m in AT_RE.finditer(text):
        if m.start() > pos:
            msg.append(MessageSegment.text(text[pos:m.start()]))
        qq = m.group(1)
        if qq.isdigit():
            msg.append(MessageSegment.at(int(qq)))
        elif qq == "all":
            msg.append(MessageSegment.at("all"))
        else:
            msg.append(MessageSegment.text("@"))
        pos = m.end()
    if pos < len(text):
        msg.append(MessageSegment.text(text[pos:]))
    return msg


@dice_matcher.handle()
async def _handle(bot: Bot, event: MessageEvent):
    from . import names
    from .event import DiceEvent, normalize_message
    from .orders import basic_order, inner_order
    from .session import sessions
    from .settings import settings
    from .store import store

    text, summoned, other = _extract(event)
    is_private = isinstance(event, PrivateMessageEvent)
    gid = 0 if is_private else int(event.group_id)
    uid = int(event.user_id)

    sender = event.sender
    nick = ""
    card = ""
    if sender:
        card = getattr(sender, "card", "") or ""
        nick = getattr(sender, "nickname", "") or ""
    names.remember(uid, nick, gid, card)
    if gid:
        group_name = getattr(event, "group_name", None)
        if group_name is None:
            group_name = getattr(getattr(event, "group", None), "name", "") or ""
        if group_name:
            store.set_chat_name(gid, str(group_name))

    e = DiceEvent(
        msg=normalize_message(text),
        uid=uid,
        gid=gid,
        nick=(store.nick(uid, gid) or card or nick or str(uid)),
        self_id=plugin_config.dice_maid_qq or int(event.self_id),
        self_name=_self_name(),
        can_host=False,
        is_private=is_private,
        is_master=uid in _superusers(),
    )
    e.ctx["nick"] = e.nick
    # 展示用的 Master：config.yml 指定优先，留空则取 NoneBot 的 SUPERUSERS
    masters = sorted(_superusers())
    e.ctx["master"] = settings.master or (masters[0] if masters else 0)

    # 全局静默：只有 .bot 仍可查看状态（开关本身在 config.yml 里）
    if settings.disabled_global and not e.msg.lower().startswith(".bot"):
        store.flush()
        return

    has_prefix = e.msg.startswith(".")
    game = sessions.get_if(gid, uid) if gid else sessions.get_if(0, uid)
    # 日志旁听：普通聊天也记录（对应 DiceEvent::fwdMsg）
    if not (has_prefix and e.msg.lower().startswith(".log")):
        _log_message(e, game, e.msg)

    if not has_prefix:
        store.flush()
        return

    if (not is_private) and other and not summoned:
        return

    try:
        e.can_host = await _can_host(bot, event, e.is_master)
    except Exception:
        e.can_host = e.is_master or is_private

    handled = 0
    try:
        handled = await basic_order(e, bot)
        if not handled:
            if gid and store.group_opt(gid, "停用指令") and not summoned:
                handled = 0
            else:
                handled = await inner_order(e, bot)
    except StopPropagation:
        raise
    except Exception as exc:  # 保证异常不打断其它插件
        import traceback
        traceback.print_exc()
        e.replies.append("出错了：%s: %s" % (type(exc).__name__, exc))
        handled = 1

    await _dispatch(bot, event, e)

    if handled:
        store.flush(force=True)
        raise StopPropagation()


def _log_message(e, game, text: str):
    """对应 DiceEvent::fwdMsg —— 记录玩家发言。"""
    from .card import idx_pc
    from .names import filter_cqcode

    if game is None or not game.is_logging() or not game.is_part(e.uid):
        return
    speaker = idx_pc(e.ctx) or e.nick
    game.log_append("%s(%s) %s\n%s\n\n" % (
        speaker, e.uid, time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        filter_cqcode(text)))


def _log_replies(e, game, replies):
    """对应 DiceEvent::logEcho —— 记录骰娘回复。"""
    from .names import filter_cqcode

    if game is None or not game.is_logging() or not game.is_part(e.uid):
        return
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    for reply in replies:
        game.log_append("%s(%s) %s\n%s\n\n" % (
            e.self_name, e.self_id, stamp, filter_cqcode(reply)))


async def _dispatch(bot: Bot, event: MessageEvent, e):
    from .session import sessions

    game = sessions.get_if(e.gid, e.uid) if e.gid else sessions.get_if(0, e.uid)
    if not e.msg.lower().startswith(".log"):
        _log_replies(e, game, e.replies)

    for reply in list(e.replies):
        text = reply.strip("\n")
        if not text.strip():
            continue
        try:
            await bot.send(event, _to_message(text))
        except Exception:
            pass
        await asyncio.sleep(0.12)

    if e.hidden_replies:
        if not e.msg.lower().startswith(".log"):
            _log_replies(e, game, [t.split("中 ", 1)[-1] for t in e.hidden_replies])
        targets = [e.uid]
        if game is not None:
            for uid in game.obs:
                if uid != e.uid:
                    targets.append(uid)
        for text in e.hidden_replies:
            for uid in targets:
                try:
                    await bot.send_private_msg(user_id=uid, message=_to_message(text))
                except Exception:
                    pass
            await asyncio.sleep(0.1)
