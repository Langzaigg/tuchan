# -*- coding: utf-8 -*-
"""昵称 / 群名的展示，对应 ManagerSystem.cpp::getName 与 DiceConsole::printUser。"""

from typing import Dict, Optional

from .constants import get_msg_text

# 最近一次发言者的昵称缓存：uid -> QQ 昵称
NICK_CACHE: Dict[int, str] = {}
# 群名片缓存：(gid, uid) -> 群名片
CARD_CACHE: Dict[tuple, str] = {}


def remember(uid, nick: str = "", gid: int = 0, card: str = ""):
    if uid:
        if nick:
            NICK_CACHE[int(uid)] = nick
        if gid and card:
            CARD_CACHE[(int(gid), int(uid))] = card


def qq_nick(uid) -> Optional[str]:
    if not uid:
        return None
    from .store import store
    nick = store.nick(uid, 0)
    if nick:
        return nick
    return NICK_CACHE.get(int(uid))


def group_card(gid, uid) -> Optional[str]:
    if not gid or not uid:
        return None
    return CARD_CACHE.get((int(gid), int(uid)))


def get_name(uid, gid: int = 0, self_id: int = 0, self_name: str = "") -> str:
    """对应 getName(uid, gid)。"""
    if not uid:
        return ""
    if self_id and int(uid) == int(self_id):
        return self_name
    from .store import store
    nick = store.nick(uid, gid) if gid else None
    if nick:
        return nick
    if gid:
        card = group_card(gid, uid)
        if card:
            return card
    nick = store.nick(uid, 0) or NICK_CACHE.get(int(uid))
    if nick:
        return nick
    if gid:
        card = group_card(gid, uid)
        if card:
            return card
    prefix = get_msg_text("strCallUser") if store.users.get(str(int(uid))) else get_msg_text("stranger")
    return "%s(%s)" % (prefix, uid)


def print_user(uid, self_id: int = 0, self_name: str = "") -> str:
    """对应 printUser：昵称(QQ)。"""
    if not uid:
        return ""
    if self_id and int(uid) == int(self_id):
        return "%s(%s)" % (self_name or get_msg_text("strSelfName"), uid)
    nick = qq_nick(uid)
    if not nick:
        return "%s[%s]" % (get_msg_text("stranger"), uid)
    return "%s(%s)" % (nick, uid)


def print_group(gid, self_id: int = 0, self_name: str = "") -> str:
    if not gid:
        return "私聊"
    from .store import store
    name = store.chat_name(gid)
    if name:
        return "[%s](%s)" % (name, gid)
    return "群(%s)" % gid


def print_chat(gid, uid, self_id: int = 0, self_name: str = "") -> str:
    if gid:
        return print_group(gid, self_id, self_name)
    return print_user(uid, self_id, self_name)


def forward_filter(text: str) -> str:
    """把内部 at 占位符转成 @昵称（对应 forward_filter）。"""
    from .formatter import AT_TEMPLATE
    import re

    def repl(m):
        qq = m.group(1)
        if qq.isdigit():
            return "@" + get_name(int(qq))
        if qq == "all":
            return "@全体成员"
        return m.group(0)

    return re.sub("\x02at:([^\x02]*)\x02", repl, text)


def filter_cqcode(text: str) -> str:
    """对应 filter_CQcode：at 转 @昵称，图片/表情/戳一戳/文件转文字，其余 CQ 码清除。"""
    import re

    def at_repl(m):
        qq = m.group(1)
        if qq.isdigit():
            return "@" + get_name(int(qq))
        if qq == "all":
            return "@全体成员"
        return "@"

    text = re.sub(r"\[CQ:at,qq=([^\]]*)\]", at_repl, text)
    text = re.sub(r"\x02at:([^\x02]*)\x02", at_repl, text)
    text = re.sub(r"\[CQ:image,[^\]]*\]", "图片", text)
    text = re.sub(r"\[CQ:face,[^\]]*\]", "表情", text)
    text = re.sub(r"\[CQ:poke,[^\]]*\]", "戳一戳", text)
    text = re.sub(r"\[CQ:file,[^\]]*\]", "文件", text)
    text = re.sub(r"\[CQ:[^\]]*\]", "", text)
    return text
