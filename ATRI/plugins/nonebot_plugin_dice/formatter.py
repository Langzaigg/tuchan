# -*- coding: utf-8 -*-
"""消息格式化引擎 + ResList / ShowList，对应 DiceFormatter.cpp 与 MsgFormat.h。"""

import random
from typing import Callable, Dict, Iterable, List, Optional

from .constants import GLOBAL_MSG, get_help_text
from . import names

MISSING = object()

# 对应 DiceFormatter.cpp 的 strFuncs：牌堆 / 回复 / 姐妹骰列表。
# 牌堆与 mod 不在复刻范围内，统一返回空串。
STR_FUNCS = {
    "list_extern_deck": "",
    "list_all_deck": "",
    "list_reply_deck": "",
    "list_extern_order": "",
    "list_dice_sister": "",
}

# 回复文本中的 at 占位符，发送前转换成 MessageSegment.at
AT_TEMPLATE = "\x02at:%s\x02"


def at_token(qq) -> str:
    return AT_TEMPLATE % qq


def strip(origin: str) -> str:
    """对应 MsgFormat.cpp::strip —— 去掉开头的 ./!/空白/全角句点。"""
    s = origin
    while s:
        if s[0] in "!. " or s[0].isspace():
            s = s[1:]
        elif s[:3] in ("！", "。", "．"):
            s = s[3:]
        else:
            break
    return s


def wstrlen(s: str) -> int:
    return len(s)


class ShowList:
    def __init__(self):
        self._list: List[str] = []
        self._len = 0

    def add(self, item: str) -> "ShowList":
        self._list.append(item)
        self._len += len(item)
        return self

    def __len__(self):
        return len(self._list)

    def empty(self) -> bool:
        return not self._list

    def show(self, sepa: str = "|") -> str:
        return sepa.join(self._list)


class ResList:
    """对应 MsgFormat.h 的 ResList（含分页逻辑）。"""

    int_page_len = 512

    def __init__(self):
        self._items: List[str] = []
        self._max_len = 0
        self._line_break = False
        self._order = False
        self._line_len = 10
        self._head = ""
        self._dot = " "
        self._long_sep = "\n"

    def add(self, s: str) -> "ResList":
        s = s.lstrip()
        if not s:
            return self
        if self._order:
            s = "%d. %s" % (len(self._items) + 1, s)
        self._items.append(s)
        self._max_len = max(self._max_len, len(s))
        return self

    def __len__(self):
        return len(self._items)

    def empty(self) -> bool:
        return not self._items

    def order(self) -> "ResList":
        self._order = True
        return self

    def head(self, s: str) -> "ResList":
        self._head = s
        return self

    def dot(self, s: str) -> "ResList":
        self._dot = s
        return self

    def linebreak(self) -> "ResList":
        self._line_break = True
        return self

    def line(self, ln: int) -> "ResList":
        self._line_len = ln
        return self

    def set_dot(self, short: str, long: str) -> "ResList":
        self._dot = short
        self._long_sep = long
        return self

    def show(self, lim_page: int = 0) -> str:
        if not self._items:
            return ""
        s = ""
        if self._max_len > self._line_len or self._line_break:
            head = self._head + "\n"
            sepa = self._long_sep
        else:
            head = ""
            sepa = self._dot
        len_page = 0
        cnt_page = 0
        for it in self._items:
            len_item = len(it)
            if len_page + len_item > self.int_page_len and s:
                if lim_page and lim_page <= cnt_page + 1:
                    return s
                if cnt_page == 0:
                    cnt_page += 1
                    s = "\f[第%d页]" % cnt_page + ("" if head else "\n") + s
                cnt_page += 1
                s += "\f[第%d页]\n%s" % (cnt_page, it)
                len_page = 0
            elif it is self._items[0]:
                s = head + it
            else:
                s += sepa + it
            len_page += len_item
        return s


# ---------------------------------------------------------------------------
class _Formatter:
    def __init__(self, ctx: dict, resolver: Optional[Callable[[str], object]],
                 globals_: Optional[Dict[str, str]], is_trust: bool):
        self.ctx = ctx
        self.resolver = resolver
        self.globals = globals_
        self.is_trust = is_trust

    # ------------------------------------------------------------------ 参考值
    def ref(self, key: str):
        if key in self.ctx:
            if key == "res":
                return self.render(str(self.ctx[key]))
            return self.ctx[key]
        if key.lower() == "list_all_deck":
            from .decks import decks
            return "/".join(decks.decks.keys())
        if key.lower() in STR_FUNCS:
            return STR_FUNCS[key.lower()]
        if self.resolver is not None:
            val = self.resolver(key)
            if val is not MISSING:
                return val
        if self.globals is not None and key.lower() in self.globals:
            # 帮助文档字典为大小写不敏感
            for k, v in self.globals.items():
                if k.lower() == key.lower():
                    return self.render(v)
        if key.lower() in GLOBAL_MSG:
            return self.render(GLOBAL_MSG[key.lower()])
        return "{%s}" % key

    # ------------------------------------------------------------------ 渲染
    def render(self, s: str) -> str:
        out: List[str] = []
        i = 0
        n = len(s)
        while i < n:
            c = s[i]
            if c == "\\" and i + 1 < n and s[i + 1] in "{}":
                out.append(s[i + 1])
                i += 2
                continue
            if c != "{":
                out.append(c)
                i += 1
                continue
            j = self._find_close(s, i + 1)
            if j < 0:
                out.append(c)
                i += 1
                continue
            field = s[i + 1:j]
            i = j + 1
            out.append(self._field(field))
        return "".join(out)

    @staticmethod
    def _find_close(s: str, pos: int) -> int:
        diff = 1
        n = len(s)
        while pos < n:
            if s[pos] == "{" and s[pos - 1] != "\\":
                diff += 1
            elif s[pos] == "}" and s[pos - 1] != "\\":
                diff -= 1
                if diff == 0:
                    return pos
            pos += 1
        return -1

    def _field(self, field: str) -> str:
        if field in ("{", "}"):
            return field
        if ":" in field:
            method, _, para = field.partition(":")
            handler = getattr(self, "_m_" + method, None)
            if handler is not None:
                return str(handler(para))
            if method in ("js", "py", "help"):
                # 不支持脚本 / 帮助嵌套时按帮助条目处理
                if method == "help":
                    return self.render(get_help_text(para))
                return ""
        return _as_text(self.ref(field))

    # -------------------------------------------------------------- 各方法实现
    def _m_sample(self, para: str) -> str:
        parts = _split_outside(para, "|")
        return self.render(random.choice(parts)) if parts else ""

    def _m_at(self, para: str) -> str:
        tgt = _as_text(self.render(para))
        if tgt == "self":
            return at_token(self.ctx.get("self_id", ""))
        if tgt:
            return at_token(tgt)
        uid = self.ctx.get("uid")
        return at_token(uid) if uid else "@" + para

    def _m_print(self, para: str) -> str:
        pairs = _split_pairs(para, "=", "&")
        printer = self.ctx.get("_printer")
        if "uid" in pairs:
            val = pairs["uid"]
            if not val:
                return printer("user", self.ctx.get("uid"))
            if self.is_trust:
                return printer("user", val)
        elif "gid" in pairs:
            val = pairs["gid"]
            if not val:
                return printer("group", self.ctx.get("gid"))
            if self.is_trust:
                return printer("group", val)
        elif "master" in pairs:
            master = self.ctx.get("master")
            return printer("user", master) if master else "[无主]"
        elif "self" in pairs:
            return printer("user", self.ctx.get("self_id"))
        return "print:" + para

    def _m_case(self, para: str) -> str:
        field, _, rest = para.partition("?")
        cases = _split_pairs(rest, "=", "&")
        val = _as_text(self.ref(field))
        if val in cases:
            return self.render(cases[val])
        if "else" in cases:
            return self.render(cases["else"])
        return ""

    def _m_grade(self, para: str) -> str:
        field, _, rest = para.partition("?")
        pairs = _split_pairs(rest, "=", "&")
        steps = {}
        other = None
        for k, v in pairs.items():
            if k == "else":
                other = v
            else:
                try:
                    steps[float(k)] = v
                except ValueError:
                    pass
        val = self.ref(field)
        try:
            num = float(_as_text(val))
        except (TypeError, ValueError):
            return self.render(other) if other is not None else ""
        keys = sorted(steps)
        chosen = None
        for k in keys:
            if k <= num:
                chosen = steps[k]
        if chosen is None:
            return self.render(other) if other is not None else ""
        return self.render(chosen)

    def _m_vary(self, para: str) -> str:
        return self._m_var(para)

    def _m_var(self, para: str) -> str:
        if "?" in para and (para.find("?") < para.find("=") or "=" not in para):
            name, _, exp = para.partition("?")
            self.ctx[name] = self.render(exp)
            return ""
        for k, v in _split_pairs(para, "=", "&").items():
            self.ctx[k] = self.render(v)
        return ""

    def _m_ran(self, para: str) -> str:
        left, _, right = para.partition("~")
        try:
            lo = int(float(_as_text(self.render(left))))
            hi = int(float(_as_text(self.render(right))))
        except (TypeError, ValueError):
            return ""
        return str(random.randint(lo, hi)) if lo < hi else ""

    def _m_wait(self, para: str) -> str:
        return ""

    def _m_len(self, para: str) -> str:
        return str(len(_as_text(self.ref(para))))

    def _m_expired(self, para: str) -> str:
        import time
        val = self.ref(para)
        try:
            return "True" if int(float(_as_text(val))) < int(time.time()) else "False"
        except (TypeError, ValueError):
            return "True"


def field_default(field: str) -> str:
    return field


def _as_text(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _split_outside(para: str, sep: str) -> List[str]:
    parts: List[str] = []
    depth = 0
    cur: List[str] = []
    i = 0
    while i < len(para):
        c = para[i]
        if c == "\\" and i + 1 < len(para):
            cur.append(c)
            cur.append(para[i + 1])
            i += 2
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _split_pairs(para: str, kv_sep: str, item_sep: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for item in _split_outside(para, item_sep):
        k, sep, v = item.partition(kv_sep)
        if sep:
            out[k] = v
        else:
            out[item] = ""
    return out


def format_msg(s: str, ctx: dict, resolver: Optional[Callable[[str], object]] = None,
               globals_: Optional[Dict[str, str]] = None, is_trust: bool = True) -> str:
    if not s:
        return s
    if s[0] == "&" and "{" not in s:
        key = s[1:]
        if key in ctx:
            return _as_text(ctx[key])
        if resolver is not None:
            val = resolver(key)
            if val is not MISSING:
                return _as_text(val)
        if globals_:
            for k, v in globals_.items():
                if k.lower() == key.lower():
                    return format_msg(v, ctx, resolver, globals_, is_trust)
        if key.lower() in GLOBAL_MSG:
            return format_msg(GLOBAL_MSG[key.lower()], ctx, resolver, globals_, is_trust)
        return ""
    if "{" not in s:
        return s
    return _Formatter(ctx, resolver, globals_, is_trust).render(s)


# ---------------------------------------------------------------------------
def print_user(uid, self_id: int = 0, self_name: str = "") -> str:
    return names.print_user(uid, self_id, self_name)


def print_group(gid, self_id: int = 0, self_name: str = "") -> str:
    return names.print_group(gid, self_id, self_name)


def get_name(uid, gid: int = 0, self_id: int = 0, self_name: str = "") -> str:
    return names.get_name(uid, gid, self_id, self_name)


def print_chat(gid, uid, self_id: int = 0, self_name: str = "") -> str:
    return names.print_chat(gid, uid, self_id, self_name)
