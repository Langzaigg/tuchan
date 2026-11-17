# -*- coding: utf-8 -*-
"""事件上下文与消息游标，对应 DiceEvent.h 的读取器 / reply 系列。"""

import re
import time
from typing import List, Optional

from .constants import GLOBAL_MSG, get_help_text, get_msg_text
from .formatter import AT_TEMPLATE, format_msg
from . import names

PREFIX_CHARS = (".", "!", "。", "！", "．")
_WS = " \t\r\n\v\f"


class DiceEvent:
    def __init__(self, msg: str, uid: int = 0, gid: int = 0, nick: str = "",
                 self_id: int = 0, self_name: str = "", trusted: int = 0,
                 can_host: bool = False, is_private: bool = True,
                 is_master: bool = False):
        self.msg = msg
        self.lower = msg.lower()
        self.cnt = 0
        self.uid = int(uid or 0)
        self.gid = int(gid or 0)
        self.chid = 0
        self.nick = nick
        self.self_id = int(self_id or 0)
        self.self_name = self_name or get_msg_text("strSelfName") or "骰娘"
        self.trusted = trusted
        self.can_host = can_host
        self.is_master = is_master
        self.is_private = is_private or not gid
        self.hidden = False
        self.replies: List[str] = []
        self.hidden_replies: List[str] = []
        self.ctx = {
            "uid": self.uid,
            "gid": self.gid,
            "nick": nick,
            "self_id": self.self_id,
            "strSelfName": self.self_name,
            "strSelfCall": self.self_name,
            "strSelfNick": self.self_name,
            "self": self.self_name,
            "master": 0,
            "_printer": self._printer,
            "self.Private": False,
            "self.CheckGroupLicense": 0,
            "self.AllowStanger": 1,
            "self.ListenGroupKicked": 0,
            "self.ListenGroupBanned": 0,
            "self.ListenSpam": 0,
        }
        self.game = None  # 由 orders 延迟填充

    # ------------------------------------------------------------------ 输出
    def _printer(self, kind: str, val) -> str:
        if kind == "user":
            return names.print_user(val, self.self_id, self.self_name)
        return names.print_group(val, self.self_id, self.self_name)

    def format(self, text: str, is_trust: bool = True) -> str:
        return format_msg(text, self.ctx, self.resolve, None, is_trust)

    def resolve(self, key: str):
        from .formatter import MISSING
        if key in self.ctx:
            return self.ctx[key]
        if key in ("nick", "nickname"):
            return self.nick or names.get_name(self.uid, self.gid, self.self_id, self.self_name)
        if key in ("pc",):
            from .card import idx_pc
            return idx_pc(self.ctx)
        if key in ("at", "@"):
            return AT_TEMPLATE % (self.uid or "")
        if key in ("fromUser", "fromQQ"):
            return str(self.uid)
        if key == "fromGroup":
            return str(self.gid)
        if key in ("user",):
            return names.print_user(self.uid, self.self_id, self.self_name)
        if key in ("char",):
            from .store import store
            return store.player(self.uid).get_card("", self.gid).name
        if key in ("game", "grp", "group", "grpAuth", "gender"):
            return MISSING
        if key.startswith("user."):
            from .store import store
            return store.get_nick(self.uid, 0) or self.nick
        if key.startswith("game."):
            from .session import sessions
            game = sessions.get_if(self.gid, self.uid) if self.gid else sessions.get_if(0, self.uid)
            if game is not None:
                return game.get(key[5:])
            return MISSING
        if key.startswith("grp.") or key.startswith("group."):
            sub = key.split(".", 1)[1]
            if sub == "name":
                from .store import store
                return store.chat_name(self.gid)
            return MISSING
        return MISSING

    def reply(self, text: str, is_format: bool = True):
        if not text:
            return
        s = text.lstrip()
        if not s:
            return
        if is_format:
            s = self.format(s)
        self.replies.append(s)

    def reply_msg(self, key: str):
        self.reply(get_msg_text(key))

    def reply_help(self, key: str):
        doc = get_help_text(key)
        if doc:
            self.reply(format_msg(doc, self.ctx, self.resolve,
                                  {k: v for k, v in GLOBAL_MSG.items()}, True), False)

    def reply_hidden(self, text: str):
        if not text:
            return
        s = text.lstrip()
        if not s:
            return
        s = self.format(s)
        self.push_hidden(s)

    def push_hidden(self, formatted: str):
        """已经过格式化的暗骰文本，补上「在X中 」后私聊发送。"""
        s = names.forward_filter(formatted)
        where = names.print_chat(self.gid, self.uid, self.self_id, self.self_name)
        self.hidden_replies.append("在%s中 %s" % (where, s))

    def note(self, text: str):
        self.reply(text)

    # ------------------------------------------------------------------ 游标
    def at_end(self) -> bool:
        return self.cnt >= len(self.msg)

    def peek(self, offset: int = 0) -> str:
        i = self.cnt + offset
        return self.msg[i] if 0 <= i < len(self.msg) else ""

    def peek_lower(self, offset: int = 0) -> str:
        i = self.cnt + offset
        return self.lower[i] if 0 <= i < len(self.lower) else ""

    def skip_space(self):
        while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
            self.cnt += 1

    def skip_colon(self):
        self.skip_space()
        while self.cnt < len(self.msg) and self.msg[self.cnt] in ":=":
            self.cnt += 1

    def read_until_space(self) -> str:
        self.skip_space()
        start = self.cnt
        while self.cnt < len(self.msg) and self.msg[self.cnt] not in _WS:
            self.cnt += 1
        return self.msg[start:self.cnt]

    def read_until_tab(self) -> str:
        while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
            self.cnt += 1
        start = self.cnt
        end = start
        ln = len(self.msg)
        while self.cnt < ln and (self.msg[self.cnt] not in _WS or self.msg[self.cnt] == " "):
            if self.msg[self.cnt] != " " or self.msg[end] != " ":
                end = self.cnt
            self.cnt += 1
        if end < ln and self.msg[end] == " ":
            self.cnt = end
        return self.msg[start:self.cnt]

    def read_rest(self) -> str:
        self.skip_space()
        return self.msg[self.cnt:]

    def read_para(self) -> str:
        self.skip_space()
        start = self.cnt
        while self.cnt < len(self.lower) and self.lower[self.cnt] not in _WS \
                and not self.lower[self.cnt].isdigit() \
                and self.lower[self.cnt] not in "-+[]=:#":
            self.cnt += 1
        return self.lower[start:self.cnt]

    def read_digit(self, force: bool = True) -> str:
        if force:
            while self.cnt < len(self.msg) and not self.msg[self.cnt].isdigit():
                self.cnt += 1
        else:
            while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
                self.cnt += 1
        start = self.cnt
        while self.cnt < len(self.msg) and self.msg[self.cnt].isdigit():
            self.cnt += 1
        if self.cnt < len(self.msg) and self.msg[self.cnt] == "]":
            self.cnt += 1
        return self.msg[start:self.cnt]

    def read_num(self, default: int = 0):
        """返回 (状态, 值)。状态：0 成功，-1 无数字，-2 超长，-3 非法。"""
        num = default
        while self.cnt < len(self.msg) and not self.msg[self.cnt].isdigit() and self.msg[self.cnt] != "-":
            self.cnt += 1
        pos = self.cnt
        if self.cnt < len(self.msg) and self.msg[self.cnt] == "-":
            self.cnt += 1
        if self.cnt >= len(self.msg):
            return -1, num
        while self.cnt < len(self.msg) and self.msg[self.cnt].isdigit():
            self.cnt += 1
        if self.cnt - pos > 9:
            return -2, num
        if self.cnt == pos or self.msg[pos] == "-":
            return -3, num
        try:
            num = int(self.msg[pos:self.cnt])
        except ValueError:
            return -3, num
        return 0, num

    def read_id(self) -> int:
        s = self.read_digit()
        if not s or len(s) > 18:
            return 0
        return int(s)

    def is_roll_dice(self) -> bool:
        self.skip_space()
        c = self.peek_lower()
        return bool(c) and (c.isdigit() or c in "dkpbf+-ax*")

    def read_xdy(self) -> str:
        while self.cnt < len(self.lower) and self.lower[self.cnt] in _WS:
            self.cnt += 1
        start = self.cnt
        while self.cnt < len(self.lower) and (self.lower[self.cnt].isdigit()
                                              or self.lower[self.cnt] in "dkpb+-x*/"):
            self.cnt += 1
        s = self.msg[start:self.cnt]
        if not _is_numeric(s) or (self.cnt < len(self.lower) and self.lower[self.cnt] in _WS):
            return s
        self.cnt -= len(s)
        return ""

    def read_dice(self) -> str:
        while self.cnt < len(self.lower) and (self.lower[self.cnt] in _WS
                                              or self.lower[self.cnt] in "=:"):
            self.cnt += 1
        start = self.cnt
        while self.cnt < len(self.lower) and (self.lower[self.cnt].isdigit()
                                              or self.lower[self.cnt] in "dkpbf+-ax*/#"):
            self.cnt += 1
        return self.msg[start:self.cnt]

    def read_exp(self) -> str:
        in_bracket = False
        while self.cnt < len(self.lower) and (self.lower[self.cnt] in _WS
                                              or self.lower[self.cnt] in "=:"):
            self.cnt += 1
        start = self.cnt
        while self.cnt != len(self.msg):
            if in_bracket:
                if self.msg[self.cnt] == "]":
                    in_bracket = False
                self.cnt += 1
                continue
            c = self.lower[self.cnt]
            if c.isdigit() or c in "dkpbf+-ax*/":
                self.cnt += 1
            elif self.msg[self.cnt] == "[":
                in_bracket = True
                self.cnt += 1
            else:
                break
        while self.cnt > 0 and self.cnt - 1 < len(self.lower) \
                and self.lower[self.cnt - 1].isalpha() and self.cnt < len(self.lower) \
                and self.lower[self.cnt].isalpha():
            self.cnt -= 1
        return self.msg[start:self.cnt]

    def read_to_colon(self) -> str:
        while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
            self.cnt += 1
        start = self.cnt
        end = start
        ln = len(self.msg)
        while self.cnt < ln and self.msg[self.cnt] not in "=:":
            if self.msg[self.cnt] not in _WS or self.msg[end] not in _WS:
                end = self.cnt
            self.cnt += 1
        if end < ln and self.msg[end] in _WS:
            self.cnt = end
        return self.msg[start:self.cnt]

    def read_attr_name(self) -> str:
        while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
            self.cnt += 1
        begin = self.cnt
        end = begin
        ln = len(self.msg)
        while self.cnt < ln and not self.msg[self.cnt].isdigit() \
                and self.msg[self.cnt] not in "=:+-*/":
            if self.msg[self.cnt] not in _WS or self.msg[end] not in _WS:
                end = self.cnt
            self.cnt += 1
        if self.cnt == len(self.lower) and " " in self.lower[begin:]:
            self.cnt = self.lower.find(" ", begin)
        elif end < ln and self.msg[end] in _WS:
            self.cnt = end
        return self.msg[begin:self.cnt]

    def read_file_name(self) -> str:
        while self.cnt < len(self.msg) and self.msg[self.cnt] in _WS:
            self.cnt += 1
        begin = self.cnt
        ln = len(self.msg)
        while self.cnt < ln and self.msg[self.cnt] not in '<>:"*|?/\\':
            self.cnt += 1
        return self.msg[begin:self.cnt]

    def read_item(self) -> str:
        ln = len(self.msg)
        if self.cnt >= ln:
            return ""
        while self.cnt < ln and (self.msg[self.cnt] in _WS or self.msg[self.cnt] == "|"):
            self.cnt += 1
        begin = self.cnt
        end = self.msg.find("|", self.cnt)
        if end < 0:
            self.cnt = end = ln
        else:
            self.cnt = end + 1
        # 去掉尾部空白（C++ 用 find_last_not_of(" \\t\\r\\n")）
        trimmed = len(self.msg[:end].rstrip(" \t\r\n"))
        return self.msg[begin:trimmed] if trimmed >= begin else ""

    def read_items(self) -> List[str]:
        out = []
        while self.cnt < len(self.msg):
            item = self.read_item()
            if not item:
                break
            out.append(item)
        return out


def _is_numeric(s: str) -> bool:
    if not s:
        return False
    has_dot = False
    for i, c in enumerate(s):
        if c.isdigit():
            continue
        if c in "+-" and i == 0:
            continue
        if c == "." and not has_dot:
            has_dot = True
            continue
        return False
    return True


def normalize_message(msg: str) -> str:
    """对应 RD.cpp::init2：全角句点归一 + 首字符 ! 转 . + 去首尾空白。"""
    s = msg.strip()
    if s[:1] in ("。", "！", "．"):
        s = "." + s[1:]
    if s.startswith("!"):
        s = "." + s[1:]
    return s.replace("\f", "\v")


_AT_RE = re.compile(AT_TEMPLATE.replace("%s", "([^\x02]*)"))
