# -*- coding: utf-8 -*-
"""牌堆：公共牌堆 yml / 原版 PublicDeck 牌堆包 + 牌面展开，对应 CardDeck.cpp。"""

import json
import random
import re
from typing import Dict, List, Optional

import yaml

from .rd import RD
from .store import store

# 默认牌堆文件内容（首次运行自动生成；改完执行 .system load 重载）
DEFAULT_DECKS = {
    "随机姓名": [
        "{随机姓氏}{随机名字}",
    ],
    "随机姓名_cn": [
        "{随机姓氏}{随机名字}",
    ],
    # .name jp / en / enzh 依赖后接类型名找「随机姓名_<类型>」，缺了会静默退回中文名
    "随机姓名_jp": [
        "佐藤健", "铃木结衣", "高桥阳一", "田中实", "伊藤沙耶", "渡边翔太",
        "山本美咲", "中村大树", "小林葵", "加藤莲", "吉田悠斗", "山田结菜",
        "佐佐木凑", "松本光", "井上遥", "木村奏", "林优真", "清水芽依",
        "山口隼人", "森川凛",
    ],
    "随机姓名_en": [
        "James Smith", "Mary Johnson", "Robert Brown", "Patricia Davis",
        "Michael Miller", "Linda Wilson", "William Moore", "Elizabeth Taylor",
        "David Anderson", "Barbara Thomas", "Richard Jackson", "Susan White",
        "Joseph Harris", "Margaret Martin", "Thomas Thompson", "Dorothy Garcia",
        "Charles Martinez", "Nancy Robinson", "Christopher Clark", "Karen Lewis",
    ],
    "随机姓名_enzh": [
        "詹姆斯·史密斯", "玛丽·约翰逊", "罗伯特·布朗", "帕特丽夏·戴维斯",
        "迈克尔·米勒", "琳达·威尔逊", "威廉·摩尔", "伊丽莎白·泰勒",
        "大卫·安德森", "芭芭拉·托马斯", "理查德·杰克逊", "苏珊·怀特",
        "约瑟夫·哈里斯", "玛格丽特·马丁", "托马斯·汤普森", "多萝西·加西亚",
        "查尔斯·马丁内斯", "南希·罗宾逊", "克里斯托弗·克拉克", "凯伦·刘易斯",
    ],
    "随机姓氏": [
        "王", "李", "张", "刘", "陈", "杨", "赵", "黄", "周", "吴",
        "徐", "孙", "胡", "朱", "高", "林", "何", "郭", "马", "罗",
    ],
    "随机名字": [
        "伟", "芳", "娜", "敏", "静", "丽", "强", "磊", "洋", "艳",
        "勇", "军", "杰", "娟", "涛", "明", "超", "秀英", "霞", "平",
    ],
    "性别": ["男", "女"],
    "调查员职业": [
        "会计师", "考古学家", "艺术家", "作家", "医生", "刑警", "记者",
        "律师", "图书馆员", "军官", "牧师", "教授", "科学家", "士兵",
    ],
    "调查员特点": [
        "健谈", "沉默寡言", "乐观", "悲观", "谨慎", "鲁莽", "善良", "冷酷",
    ],
    "个人描述": ["身材高大", "体型瘦削", "面容和善", "神情紧张"],
    "重要之人": ["父母", "恋人", "恩师", "战友", "子女"],
    "思想信念": ["信仰科学", "相信命运", "追求真相", "守护家人"],
    "意义非凡之地": ["故乡", "大学", "战场", "海边小镇"],
    "宝贵之物": ["旧怀表", "亡友的遗物", "日记本", "护身符"],
}


class DeckStore:
    """公共牌堆。

    三个来源，后者覆盖前者：
      1. `DEFAULT_DECKS`（代码内置，保证 .name / .pc 有牌可用）
      2. `data/dice/PublicDeck/*.json`（原版 Dice! 的牌堆包，可直接拷贝过来）
      3. `data/dice/decks.yml`（本移植的本地配置，优先级最高）
    """

    def __init__(self):
        self._decks: Dict[str, List[str]] = {}
        self._path = store.dir / "decks.yml"
        self._public_dir = store.dir / "PublicDeck"

    def load(self):
        """启动时加载一次；改完牌堆后用 .system load 重载。"""
        try:
            self._path.stat()
        except OSError:
            self._write_default()
        decks: Dict[str, List[str]] = {k: list(v) for k, v in DEFAULT_DECKS.items()}
        # 原版牌堆包：同名时覆盖内置，但让位给 decks.yml
        for name, cards in self._load_public().items():
            decks[name] = cards
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            data = {}
        raw = data.get("decks") if isinstance(data, dict) else None
        if isinstance(raw, dict):
            for name, cards in raw.items():
                if isinstance(cards, list):
                    decks[str(name)] = [str(c) for c in cards]
                elif isinstance(cards, str):
                    decks[str(name)] = [line.strip() for line in cards.splitlines() if line.strip()]
        self._decks = decks

    def _load_public(self) -> Dict[str, List[str]]:
        """读取原版格式的牌堆包。

        对应 `Dice.cpp` 的 `loadDir(loadJMap, DiceDir / "PublicDeck", ...)`：
        目录下每个 `.json` 文件都是 `{"牌堆名": ["卡面", ...]}`，多文件合并、
        后读到的覆盖先读到的（这里按文件名排序保证顺序固定）。
        解析不了的文件直接跳过，不影响其他牌堆。
        """
        out: Dict[str, List[str]] = {}
        if not self._public_dir.is_dir():
            return out
        try:
            files = sorted(
                (p for p in self._public_dir.iterdir()
                 if p.is_file() and not p.name.startswith(".")
                 and p.suffix.lower() == ".json"),
                key=lambda p: p.name,
            )
        except OSError:
            return out
        for path in files:
            try:
                with path.open("r", encoding="utf-8-sig") as f:
                    data = json.load(f)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            for name, cards in data.items():
                if isinstance(cards, list):
                    vals = [str(c) for c in cards if c is not None]
                elif isinstance(cards, str):
                    vals = [ln.strip() for ln in cards.splitlines() if ln.strip()]
                else:
                    continue
                if vals:
                    out[str(name)] = vals
        return out

    def _write_default(self):
        try:
            with self._path.open("w", encoding="utf-8") as f:
                f.write("# Dice! 公共牌堆\n")
                f.write("# 格式：decks 下每个键为牌堆名，值为牌面列表；\n")
                f.write("#   - 用 {牌堆名} 可嵌套抽取；用 [1d6] 可内嵌掷骰；\n")
                f.write("#   - 卡面写成 内容::权重::内容 表示抽取权重。\n")
                f.write("# 同名牌堆优先级：本文件 > PublicDeck/*.json > 代码内置。\n")
                f.write("# 原版 Dice! 的牌堆包（{ \"牌堆名\": [\"卡面\"] } 的 json）可直接放进 PublicDeck/ 目录。\n")
                yaml.safe_dump({"decks": DEFAULT_DECKS}, f, allow_unicode=True, sort_keys=False)
        except OSError:
            pass

    @property
    def decks(self) -> Dict[str, List[str]]:
        return self._decks

    def get(self, name: str) -> Optional[List[str]]:
        return self.decks.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self.decks


def split_weighted(card: str):
    """解析 内容::权重::内容，返回 (纯内容, 权重)；非权重项返回 (card, None)。"""
    l = card.find("::")
    if l < 0:
        return card, None
    r = card.find("::", l + 2)
    if r < 0:
        return card, None
    num = draw_text(card[l + 2:r])
    if len(num) > 6:
        return card, None
    try:
        cnt = int(num)
    except ValueError:
        return card, None
    if cnt <= 0:
        return card, None
    return card[r + 2:], cnt


def draw_card(deck: List[str], is_back: bool = False) -> str:
    """对应 CardDeck::drawCard —— 抽一张，不放回时修改传入列表。"""
    if not deck:
        return ""
    parsed: List[str] = []
    bounds = [0]
    for card in deck:
        text, weight = split_weighted(card)
        parsed.append(text)
        bounds.append(bounds[-1] + (weight if weight else 1))
    if len(deck) == 1:
        reply = parsed[0]
        if not is_back:
            if bounds[1] == 1:
                deck.pop(0)
            else:
                deck[0] = "::%d::%s" % (bounds[1] - 1, parsed[0])
        return draw_text(reply)
    ans = random.randint(0, bounds[-1] - 1)
    idx = 0
    for i in range(len(bounds) - 1):
        if bounds[i] <= ans < bounds[i + 1]:
            idx = i
            break
    else:
        idx = len(parsed) - 1
    reply = parsed[idx]
    if not is_back:
        if bounds[idx + 1] - bounds[idx] == 1:
            deck.pop(idx)
        else:
            deck[idx] = "::%d::%s" % (bounds[idx + 1] - bounds[idx] - 1, parsed[idx])
    return draw_text(reply)


def draw_one(deck: List[str]) -> str:
    """对应 CardDeck::drawOne —— 不修改传入列表。"""
    if not deck:
        return ""
    if len(deck) == 1:
        return draw_text(deck[0])
    return draw_card(list(deck), True)


_TEMP_DECKS: Dict[str, List[str]] = {}


def draw_text(exp: str) -> str:
    """对应 CardDeck::draw —— 展开 {牌堆} 与 [掷骰]。"""
    out = exp
    cnt = 0
    while True:
        lq = out.find("{", cnt)
        if lq < 0:
            break
        rq = out.find("}", lq)
        if rq < 0:
            break
        if lq > 0 and out[lq - 1] == "\\":
            out = out[:lq - 1] + out[lq:]
            cnt = rq
            continue
        name = out[lq + 1:rq]
        tmp_back = False
        if name.startswith("%"):
            tmp_back = True
            name = name[1:]
        deck = decks.get(name)
        if deck is None:
            cnt = rq + 1
            continue
        if tmp_back:
            res = draw_card(list(deck), True)
        else:
            if not _TEMP_DECKS.get(name):
                _TEMP_DECKS[name] = list(deck)
            res = draw_card(_TEMP_DECKS[name], False)
        out = out[:lq] + res + out[rq + 1:]
        cnt = lq + len(res)
    cnt = 0
    while True:
        lq = out.find("[", cnt)
        if lq < 0:
            break
        rq = out.find("]", lq)
        if rq < 0:
            break
        if lq > 0 and out[lq - 1] == "\\":
            out = out[:lq - 1] + out[lq:]
            cnt = rq
            continue
        expr = out[lq + 1:rq]
        cnt = rq + 1
        rd = RD(expr)
        if rd.Roll():
            continue
        val = str(rd.intTotal)
        out = out[:lq] + val + out[rq + 1:]
        cnt = lq + len(val)
    return out


decks = DeckStore()
decks.load()
