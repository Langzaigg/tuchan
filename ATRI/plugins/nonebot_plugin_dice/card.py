# -*- coding: utf-8 -*-
"""角色卡（CharaCard / Player / COC7 模板），对应 CharacterCard.cpp。"""

import time
from typing import Dict, List, Optional

from .constants import (
    BASIC_COC7,
    EXPRESSION_COC7,
    PLAYER_ERRORS,
    SKILL_DEFAULT_VAL,
    SKILL_NAME_REPLACE,
    VARIABLE_COC7,
)
from .formatter import format_msg
from .rd import RD

TEXT_PLAIN = 0
TEXT_DICEEXP = 1
TEXT_JS = 2


class AttrShape:
    __slots__ = ("def_val", "text_type")

    def __init__(self, def_val=None, text_type: int = TEXT_PLAIN):
        self.def_val = def_val
        self.text_type = text_type

    def init(self, card: "CharaCard"):
        if self.text_type == TEXT_JS:
            return self.def_val
        if self.text_type == TEXT_DICEEXP and self.def_val is not None:
            exp = format_msg(str(self.def_val), card.as_ctx(), card.resolve_formatter)
            return card.cal(exp)
        return self.def_val

    def equal_default(self, val) -> bool:
        return self.text_type == TEXT_PLAIN and val == self.def_val


class CardTemp:
    def __init__(self, type_name: str, replace: Dict[str, str], basic: List[List[str]],
                 dynamic: Dict[str, str], exps: Dict[str, str],
                 def_skill: Dict[str, int], presets: Optional[Dict[str, Dict[str, str]]] = None):
        self.type = type_name
        self.replace_name = {k.lower(): v for k, v in replace.items()}
        self.v_basic_list = basic
        self.attr_shapes: Dict[str, AttrShape] = {}
        self.attr_keys: List[str] = []
        for attr, exp in dynamic.items():
            self.attr_shapes[attr.lower()] = AttrShape(exp, TEXT_DICEEXP)
            self.attr_keys.append(attr)
        for attr, exp in exps.items():
            key = "&" + attr
            self.attr_shapes[key.lower()] = AttrShape(exp, TEXT_PLAIN)
            self.attr_keys.append(key)
        for attr, val in def_skill.items():
            self.attr_shapes[attr.lower()] = AttrShape(val, TEXT_PLAIN)
            self.attr_keys.append(attr)
        self.presets = presets or {}

    def shape(self, key: str) -> Optional[AttrShape]:
        return self.attr_shapes.get((key or "").lower())

    def can_get(self, key: str) -> bool:
        s = self.shape(key)
        return s is not None and s.def_val is not None

    def equal_default(self, key: str, val) -> bool:
        s = self.shape(key)
        return s is not None and s.equal_default(val)


BuildCOC7 = [
    ("__Name", "{随机姓名}"),
    ("力量", "3D6*5"),
    ("体质", "3D6*5"),
    ("体型", "2D6*5+30"),
    ("敏捷", "3D6*5"),
    ("外貌", "3D6*5"),
    ("智力", "2D6*5+30"),
    ("意志", "3D6*5"),
    ("教育", "2D6*5+30"),
    ("幸运", "3D6*5"),
]

COC7_BG = [
    ("性别", "{性别}"),
    ("年龄", "7D6+8"),
    ("职业", "{调查员职业}"),
    ("个人描述", "{个人描述}"),
    ("重要之人", "{重要之人}"),
    ("思想信念", "{思想信念}"),
    ("意义非凡之地", "{意义非凡之地}"),
    ("宝贵之物", "{宝贵之物}"),
    ("特质", "{调查员特点}"),
]

MODEL_COC7 = CardTemp(
    "COC7", SKILL_NAME_REPLACE, BASIC_COC7, VARIABLE_COC7, EXPRESSION_COC7,
    SKILL_DEFAULT_VAL,
    {
        "pc": dict(BuildCOC7),
        "bg": dict(COC7_BG),
    },
)

CARD_MODELS = {"COC7": MODEL_COC7}


class CharaCard:
    """角色卡。dict 中以下划线开头的键为内部字段，不参与展示。"""

    def __init__(self, name: str = "", card_id: int = 0, type_name: str = "COC7"):
        self.id = card_id
        self.data: Dict[str, object] = {"__Name": name, "__Type": type_name,
                                        "__Update": int(time.time())}
        self.locks: set = set()

    # ------------------------------------------------------------- 基础访问
    @property
    def name(self) -> str:
        return str(self.data.get("__Name", ""))

    def set_name(self, name: str):
        self.data["__Name"] = name

    def set_type(self, type_name: str):
        self.data["__Type"] = type_name

    def touch(self):
        self.data["__Update"] = int(time.time())

    def templet(self) -> CardTemp:
        t = str(self.data.get("__Type", ""))
        return CARD_MODELS.get(t, CARD_MODELS["COC7"])

    def standard(self, key: str) -> str:
        if not key:
            return key
        rep = self.templet().replace_name
        return rep.get(key.lower(), key)

    def has(self, key: str) -> bool:
        if key in self.data:
            return True
        attr = self.standard(key)
        if attr in self.data:
            return self.data[attr] is not None
        return self.templet().can_get(attr)

    def has_attr(self, key: str) -> bool:
        return key in self.data or self.standard(key) in self.data

    def get(self, key: str, default=None):
        if key in self.data:
            return self.data[key]
        temp = self.templet()
        attr = self.standard(key)
        if attr in self.data:
            return self.data[attr]
        if temp.can_get(attr):
            return temp.shape(attr).init(self)
        return default

    def get_int(self, key: str, default: int = 0) -> int:
        val = self.get(key, default)
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def set(self, key: str, val) -> int:
        if not key:
            return -1
        if key == "__Name":
            return -8
        if isinstance(val, str) and len(val) > 256:
            return -11
        key = self.standard(key)
        if self.templet().equal_default(key, val):
            if key in self.data:
                self.data.pop(key, None)
            else:
                return -1
        else:
            self.data[key] = val
        self.touch()
        return 0

    def erase(self, key: str) -> bool:
        if key in self.data:
            self.data.pop(key)
        elif self.standard(key) in self.data:
            self.data.pop(self.standard(key))
        elif ("&" + key) in self.data:
            self.data.pop("&" + key)
        else:
            return False
        self.touch()
        return True

    def clear(self):
        type_name = self.data.get("__Type", "COC7")
        self.data = {"__Name": self.name, "__Type": type_name}
        self.touch()

    def show_one(self, key: str) -> Optional[str]:
        std = self.standard(key)
        if key in self.data or std in self.data or self.has(std):
            val = self.get(key)
            if val is None:
                return None
            return _print_val(val)
        return None

    def show_all(self, whole: bool) -> str:
        from .formatter import ResList

        used = set()
        res = ResList()
        for group in self.templet().v_basic_list:
            sub = ResList()
            for item in group:
                val = self.show_one(item)
                if val is not None:
                    used.add(item)
                    if item.startswith("&"):
                        sub.add(item + "=" + val)
                    else:
                        sub.add(item + ":" + val)
            res.add(sub.show())
        rest = []
        for key, val in self.data.items():
            if key in used or key.startswith("_"):
                continue
            if whole and isinstance(val, int) and not isinstance(val, bool):
                continue
            sep = " " if isinstance(val, int) and not isinstance(val, bool) else (
                "\t" if isinstance(val, str) else "\n")
            rest.append(key + ":" + _print_val(val) + sep)
        res.add("".join(rest))
        return res.show()

    # ---------------------------------------------------------------- 表达式
    def get_exp(self, key: str, refs: Optional[set] = None) -> str:
        refs = refs or set()
        key = self.standard(key)
        refs.add(key)
        temp = self.templet()
        if ("&" + key) in self.data:
            return self.escape(str(self.data["&" + key]), refs)
        if key in self.data:
            return self.escape(str(self.data[key]), refs)
        shape = temp.shape("&" + key)
        if shape is not None:
            val = shape.init(self)
            return self.escape(_print_val(val), refs)
        shape = temp.shape(key)
        if shape is not None:
            val = shape.init(self)
            return self.escape(_print_val(val), refs)
        return "0"

    def escape(self, exp: str, refs: set) -> str:
        if not exp:
            return exp
        if exp[0] == "&":
            key = self.standard(exp[1:])
            if key in refs:
                return ""
            return self.get_exp(key, refs)
        out = exp
        cnt = 0
        while True:
            lp = out.find("{", cnt)
            if lp < 0:
                break
            rp = out.find("}", lp)
            if rp < 0:
                break
            prop = out[lp + 1:rp]
            if prop in refs:
                return ""
            val = self.get_exp(prop, refs)
            out = out[:lp] + val + out[rp + 1:]
            cnt = lp + len(val)
        return out

    def count_exp(self, key: str) -> bool:
        if key.startswith("&"):
            return self.has(key) or self.templet().can_get(key)
        return self.has("&" + key) or self.templet().can_get("&" + key)

    def cal(self, exp: str):
        if not exp:
            return None
        if exp[0] == "&":
            val = self.get(exp[1:])
            try:
                return int(val)
            except (TypeError, ValueError):
                return None
        cur = exp
        while True:
            r = cur.find(")")
            if r < 0:
                break
            l = cur.rfind("(", 0, r)
            if l < 0:
                break
            res = self.cal(cur[l + 1:r])
            if res is None:
                return None
            cur = cur[:l] + str(res) + cur[r + 1:]
        rd = RD(cur)
        if rd.Roll() == 0:
            return rd.intTotal
        return None

    # ---------------------------------------------------------------- 生成
    def build(self, para: str = ""):
        temp = self.templet()
        preset = temp.presets.get(para)
        if not preset:
            return
        for attr, exp in preset.items():
            if attr not in self.data or self.data[attr] is None:
                self.set(attr, AttrShape(exp, TEXT_DICEEXP).init(self))

    def buildv(self, para: str = ""):
        options = []
        while ":" in para:
            idx = para.rfind(":")
            options.append(para[idx + 1:])
            para = para[:idx]
        if para:
            options.append(para)
        options.reverse()
        if not options:
            self.build("pc")
        for opt in options:
            self.build(opt)
        self.touch()

    # -------------------------------------------------------------- 统计
    def cnt_roll_stat(self, die: int, face: int):
        if face <= 0 or die <= 0:
            return
        self.inc("__StatD%dCnt" % face)
        self.inc("__StatD%dSum" % face, die)
        self.inc("__StatD%dSqrSum" % face, die * die)
        self.touch()

    def cnt_rc_stat(self, die: int, rate: int):
        if rate <= 0 or rate >= 100 or die <= 0 or die > 100:
            return
        self.inc("__StatRcCnt")
        if die <= rate:
            self.inc("__StatRcSumSuc")
        if die == 1:
            self.inc("__StatRcCnt1")
        if die <= 5:
            self.inc("__StatRcCnt5")
        if die >= 96:
            self.inc("__StatRcCnt96")
        if die == 100:
            self.inc("__StatRcCnt100")
        self.data["__StatRcSumRate"] = self.get_int("__StatRcSumRate") + rate
        self.touch()

    def inc(self, key: str, delta: int = 1):
        self.data[key] = self.get_int(key) + delta

    # ------------------------------------------------------- 供 formatter 使用
    def as_ctx(self) -> dict:
        return self.data

    def resolve_formatter(self, key: str):
        from .formatter import MISSING
        if key in self.data:
            return self.data[key]
        if self.has_attr(key):
            return self.get(key)
        return MISSING

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "data": _jsonable(self.data),
                "locks": sorted(self.locks)}

    @classmethod
    def from_json(cls, obj: dict) -> "CharaCard":
        card = cls(obj.get("name", ""), int(obj.get("id", 0)))
        card.data = dict(obj.get("data", {}))
        card.locks = set(obj.get("locks", []))
        return card


def _print_val(val) -> str:
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float) and val.is_integer():
        return str(int(val))
    return str(val)


def _jsonable(obj):
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


class Player:
    """单个用户持有的全部角色卡。"""

    def __init__(self):
        self.index_max = 0
        self.cards: Dict[int, CharaCard] = {}
        self.name_list: Dict[str, CharaCard] = {}
        self.group_card: Dict[int, CharaCard] = {}
        card = CharaCard("角色卡", 0)
        self.cards[0] = card
        self.group_card[0] = card

    # ------------------------------------------------------------------
    def size(self) -> int:
        return len(self.cards)

    def count(self, key) -> bool:
        if isinstance(key, str):
            return key in self.name_list
        return int(key) in self.group_card

    def __getitem__(self, key) -> CharaCard:
        if isinstance(key, str):
            if key in self.name_list:
                return self.name_list[key]
            return self.get_card("", 0)
        return self.get_card_by_id(int(key))

    def new_card(self, s: str, group: int = 0, type_name: str = "COC7"):
        if len(self.cards) > 32:
            return -1
        s = s.strip()
        if ":" in s:
            idx = s.rfind(":")
            type_name = s[:idx]
            s = s[idx + 1:]
        elif s in CARD_MODELS:
            type_name = s
            s = ""
        options = []
        while ":" in type_name:
            idx = type_name.rfind(":")
            options.append(type_name[idx + 1:])
            type_name = type_name[:idx]
        options.reverse()
        if s in self.name_list:
            return -4
        if "=" in s:
            return -6
        self.index_max += 1
        card = CharaCard(s, self.index_max, type_name)
        self.cards[self.index_max] = card
        temp = card.templet()
        for para in options:
            card.build(para)
            if not card.name and para in temp.presets and "__Name" in temp.presets[para]:
                name = AttrShape(temp.presets[para]["__Name"], TEXT_DICEEXP).init(card)
                if name and str(name) not in self.name_list:
                    card.set_name(str(name))
        if not card.name:
            pc_preset = temp.presets.get("pc", {})
            if "__Name" in pc_preset:
                name = AttrShape(pc_preset["__Name"], TEXT_DICEEXP).init(card)
                if name and str(name) not in self.name_list:
                    card.set_name(str(name))
            if not card.name:
                card.set_name(str(self.index_max))
        s = card.name
        self.group_card[group] = card
        self.name_list[s] = card
        return 0

    def empty_card(self, s: str, group: int, type_name: str):
        if len(self.cards) > 32:
            return -1
        if s in self.name_list:
            return -4
        if "=" in s:
            return -6
        self.index_max += 1
        card = CharaCard(s, self.index_max, type_name)
        self.cards[self.index_max] = card
        self.name_list[s] = card
        return 0

    def build_card(self, name: str, is_clear: bool, group: int = 0):
        str_name = name
        str_type = ""
        if ":" in name:
            str_name = name[name.rfind(":") + 1:].strip()
            str_type = name[:name.rfind(":")]
        if str_name and str_name not in self.name_list:
            res = self.new_card(name, group)
            if res:
                return res
            self.get_card(str_name, group).buildv()
        else:
            card = self.get_card(str_name, group)
            if is_clear:
                card.clear()
            card.buildv(str_type)
            if self.name_list.get(card.name) is not self.group_card.get(group):
                self.group_card[group] = card
        return 0

    def change_card(self, name: str, group: int) -> int:
        if not name:
            self.group_card.pop(group, None)
            return 1
        if name not in self.name_list:
            return -5
        self.group_card[group] = self.name_list[name]
        return 0

    def remove_card(self, name: str) -> int:
        if name not in self.name_list:
            return -5
        card = self.name_list[name]
        if card.id == 0:
            return -7
        if "q" in card.locks:
            return -21
        self.name_list.pop(name, None)
        self.cards.pop(card.id, None)
        for gid in [g for g, c in self.group_card.items() if c.id == card.id]:
            self.group_card.pop(gid, None)
        while self.index_max not in self.cards and self.index_max > 0:
            self.index_max -= 1
        return 0

    def rename_card(self, card: CharaCard, new_name: str) -> int:
        if not new_name:
            return -3
        if new_name in self.name_list:
            return -4
        if ":" in new_name:
            return -6
        if "n" in card.locks:
            return -22
        self.name_list.pop(card.name, None)
        card.set_name(new_name)
        self.name_list[new_name] = card
        return 0

    def copy_card(self, name1: str, name2: str, group: int = 0) -> int:
        if not name1 or not name2:
            return -3
        if name2 not in self.name_list:
            return -5
        if name1 not in self.name_list:
            if len(self.cards) > 32:
                return -1
            if ":" in name1:
                return -6
            self.index_max += 1
            card = CharaCard(name1, self.index_max)
            self.cards[self.index_max] = card
            self.name_list[name1] = card
        src = self.get_card(name2)
        dst = self.get_card(name1)
        keep = dst.name
        dst.data = dict(src.data)
        dst.data["__Name"] = keep
        return 0

    def get_card(self, name: str = "", group: int = 0) -> CharaCard:
        if name and name in self.name_list:
            return self.name_list[name]
        if group in self.group_card:
            return self.group_card[group]
        if 0 in self.group_card:
            return self.group_card[0]
        return self.cards[min(self.cards)]

    def get_card_by_id(self, cid: int) -> CharaCard:
        if cid in self.group_card:
            return self.group_card[cid]
        if cid in self.cards:
            return self.cards[cid]
        if 0 in self.group_card:
            return self.group_card[0]
        return self.cards[min(self.cards)]

    def list_card(self) -> str:
        from .formatter import ResList
        res = ResList()
        for idx in sorted(self.cards):
            card = self.cards[idx]
            res.add("[%d]<%s>%s" % (idx, card.data.get("__Type", ""), card.name))
        res.add("default:" + self.get_card_by_id(0).name)
        return res.show()

    def list_map(self) -> str:
        from .formatter import ResList
        res = ResList()
        for gid, card in self.group_card.items():
            if not gid:
                res.add("default:" + card.name)
            else:
                res.add("(%d)%s" % (gid, card.name))
        return res.show()

    # ------------------------------------------------------------------
    def to_json(self) -> dict:
        return {
            "index_max": self.index_max,
            "cards": [self.cards[k].to_json() for k in sorted(self.cards)],
            "group_card": {str(g): c.id for g, c in self.group_card.items()},
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Player":
        pl = cls()
        pl.cards.clear()
        pl.name_list.clear()
        pl.group_card.clear()
        pl.index_max = int(obj.get("index_max", 0))
        for cobj in obj.get("cards", []):
            card = CharaCard.from_json(cobj)
            pl.cards[card.id] = card
            pl.name_list[card.name] = card
        for gid, cid in obj.get("group_card", {}).items():
            card = pl.cards.get(int(cid))
            if card is not None:
                pl.group_card[int(gid)] = card
        if not pl.cards:
            return cls()
        return pl


def idx_pc(ctx: dict) -> str:
    if ctx.get("pc"):
        return str(ctx["pc"])
    if not ctx.get("uid"):
        return ""
    from .store import store
    pl = store.player(int(ctx["uid"]))
    gid = int(ctx.get("gid") or 0)
    card = pl.get_card("", gid)
    if card.name and card.name != "角色卡":
        ctx["pc"] = card.name
        return card.name
    return str(ctx.get("nick") or "")
