# -*- coding: utf-8 -*-
"""RD / DicePool —— 对 Dice-newdev/Dice/RD.cpp 的逐行移植。

所有掷骰表达式解析、取整方向、展示格式均与原实现保持一致。
"""

import math
import random
from typing import List, Optional

from .constants import (
    SKILL_DEFAULT_VAL,
    SKILL_NAME_REPLACE,
    LONG_INSANITY,
    STR_FEAR,
    STR_PANIC,
    TEMP_INSANITY,
)

# 错误码（RDConstant.h）
VALUE_ERR = -1
INPUT_ERR = -2
ZERO_DICE_ERR = -3
ZERO_TYPE_ERR = -4
DICE_TOO_BIG_ERR = -5
TYPE_TOO_BIG_ERR = -6
ADD_DICE_VAL_ERR = -7
DICE_CNT_ERR = -8

# 骰型
NORMAL_DICE = 0
B_DICE = 1
P_DICE = 2
FUDGE_DICE = 3
WW_DICE = 4

_OPERA = "+-X/"


def randint(lowest: int, highest: int) -> int:
    return random.randint(lowest, highest)


def idiv(a: int, b: int) -> int:
    """C++ 整数除法：向零取整。"""
    if b == 0:
        return 0
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def to_circled(num: int, c: int = 8) -> str:
    if num < 1 or num > 10:
        return "?"
    if num < c:
        return str(num)
    return chr(0x2460 + num - 1)


class RD:
    """掷骰表达式。"""

    def __init__(self, dice: str, default_dice: int = 100):
        self.strDice = self._normalize(dice, default_dice)
        self.vvintRes: List[List[int]] = []
        self.vintRes: List[int] = []
        self.vboolNegative: List[bool] = []
        self.vintMultiplier: List[int] = []
        self.vintDivider: List[int] = []
        self.vBnP: List[int] = []
        self.intTotal = 0

    @staticmethod
    def _normalize(dice: str, default_dice: int) -> str:
        out: List[str] = []
        ch = "\0"
        for raw in dice:
            i = raw
            if i in ("*", "x", "X"):
                if ch in _OPERA:
                    out.pop()
                    break
                i = "X"
            if i == "/":
                if ch in _OPERA:
                    out.pop()
                    break
            if i == "+":
                if ch in _OPERA:
                    continue
            if i == "-":
                if ch in _OPERA:
                    if ch == "+":
                        out[-1] = "-"
                    elif ch == "-":
                        out[-1] = "+"
                    elif ch in ("X", "/"):
                        out.pop()
                        break
                    continue
            elif i in ("a", "A"):
                i = "a"
            else:
                i = i.upper()
            ch = i
            out.append(i)
        s = "".join(out)
        if not s:
            s = "D" + str(default_dice)
        if s[0] == "a":
            s = "1" + s
        if s[0] == "D" and len(s) > 1 and s[1] == "F":
            s = "4" + s
        if s[0] == "F":
            s = "4D" + s
        while "XX" in s:
            s = s.replace("XX", "X", 1)
        while "//" in s:
            s = s.replace("//", "/", 1)
        idx = 1
        while idx != len(s):
            if s[idx] == "F" and (s[idx - 1].isdigit() or s[idx - 1] in "+-"):
                s = s[:idx] + "D" + s[idx:]
            idx += 1
        while "-a" in s:
            p = s.index("-a")
            s = s[: p + 1] + "1" + s[p + 1:]
        while "+a" in s:
            p = s.index("+a")
            s = s[: p + 1] + "1" + s[p + 1:]
        while "+DF" in s:
            p = s.index("+DF")
            s = s[: p + 1] + "4" + s[p + 1:]
        while "-DF" in s:
            p = s.index("-DF")
            s = s[: p + 1] + "4" + s[p + 1:]
        for sig in ("D+", "D-", "DX", "D/", "DK"):
            while sig in s:
                p = s.index(sig)
                s = s[: p + 1] + str(default_dice) + s[p + 1:]
        for sig in ("K+", "K-"):
            while sig in s:
                p = s.index(sig)
                s = s[: p + 1] + "1" + s[p + 1:]
        if s.endswith("D"):
            s += str(default_dice)
        if s.endswith("K"):
            s += "1"
        if s.startswith("+"):
            s = s[1:]
        return s

    # ------------------------------------------------------------------ roll
    def _roll_dice(self, dice: str, game=None) -> int:
        negative = self.vboolNegative[-1]
        divider = 1
        while "/" in dice:
            p = dice.rfind("/")
            rate = RD(dice[p + 1:])
            if rate.Roll() == 0 and rate.intTotal:
                divider *= rate.intTotal
            else:
                return INPUT_ERR
            dice = dice[:p]
        multiplier = 1
        while "X" in dice:
            p = dice.rfind("X")
            rate = RD(dice[p + 1:])
            if rate.Roll() == 0:
                multiplier *= rate.intTotal
            else:
                return INPUT_ERR
            dice = dice[:p]
        self.vintDivider.append(divider)
        self.vintMultiplier.append(multiplier)

        if dice and dice[-1] == "F":
            self.vBnP.append(FUDGE_DICE)
            num = dice[:-2] if len(dice) >= 2 and dice[-2] == "D" else dice[:-1]
            if any(not c.isdigit() for c in num):
                return VALUE_ERR
            if len(num) > 2:
                return DICE_TOO_BIG_ERR
            cnt = int(num)
            if cnt == 0:
                return ZERO_DICE_ERR
            rolls = [randint(0, 2) - 1 for _ in range(cnt)]
            total = sum(rolls)
            self.vvintRes.append(rolls)
            self.vintRes.append(total)
            self.intTotal += -total if negative else total
            return 0

        if dice and dice[0] == "P":
            self.vBnP.append(P_DICE)
            if len(dice) > 2:
                return DICE_TOO_BIG_ERR
            if any(not c.isdigit() for c in dice[1:]):
                return DICE_CNT_ERR
            cnt = int(dice[1:]) if dice[1:] else 1
            if len(dice) == 1:
                cnt = 1
            if cnt == 0:
                return VALUE_ERR
            rolls = [game.roll(100) if game else randint(1, 100)]
            for _ in range(cnt):
                r = randint(1, 10)
                rolls.append(r if rolls[0] % 10 == 0 else r - 1)
            d100 = rolls[0]
            for i in range(1, len(rolls)):
                if rolls[i] > idiv(d100, 10):
                    d100 = rolls[i] * 10 + d100 % 10
            self.intTotal += -d100 if negative else d100
            self.vintRes.append(d100)
            self.vvintRes.append(rolls)
            return 0

        if dice and dice[0] == "B":
            self.vBnP.append(B_DICE)
            if len(dice) > 2:
                return DICE_TOO_BIG_ERR
            if any(not c.isdigit() for c in dice[1:]):
                return DICE_CNT_ERR
            cnt = int(dice[1:]) if dice[1:] else 1
            if len(dice) == 1:
                cnt = 1
            if cnt == 0:
                return VALUE_ERR
            rolls = [game.roll(100) if game else randint(1, 100)]
            for _ in range(cnt):
                r = randint(1, 10)
                rolls.append(r if rolls[0] % 10 == 0 else r - 1)
            d100 = rolls[0]
            for i in range(1, len(rolls)):
                if rolls[i] < idiv(d100, 10):
                    d100 = rolls[i] * 10 + d100 % 10
            self.intTotal += -d100 if negative else d100
            self.vintRes.append(d100)
            self.vvintRes.append(rolls)
            return 0

        self.vBnP.append(NORMAL_DICE)
        if dice and dice[0] == "X":
            return INPUT_ERR
        contain_d = False
        contain_k = False
        for c in dice:
            c = c.upper()
            if not c.isdigit():
                if c == "D":
                    if contain_d:
                        return INPUT_ERR
                    contain_d = True
                elif c == "K":
                    if not contain_d or contain_k:
                        return INPUT_ERR
                    contain_k = True
                else:
                    return INPUT_ERR

        if not contain_d:
            if len(dice) > 5 or len(dice) == 0:
                return VALUE_ERR
            value = int(dice)
            self.intTotal += -idiv(value * multiplier, divider) if negative else idiv(value * multiplier, divider)
            self.vintRes.append(idiv(value * multiplier, divider))
            self.vvintRes.append([value])
            return 0

        if not contain_k:
            p = dice.index("D")
            if len(dice[:p]) > 3:
                return DICE_TOO_BIG_ERR
            if len(dice[p + 1:]) > 4:
                return TYPE_TOO_BIG_ERR
            cnt = 1 if len(dice[:p]) == 0 else int(dice[:p])
            faces = int(dice[p + 1:])
            if cnt == 0:
                return ZERO_DICE_ERR
            if faces == 0:
                return ZERO_TYPE_ERR
            rolls: List[int] = []
            total = 0
            if game is not None and cnt < 10 and faces in game.roulette:
                rou = game.roulette[faces]
                for _ in range(cnt):
                    r = rou.roll()
                    rolls.append(r)
                    total += r
                game.touch()
            else:
                for _ in range(cnt):
                    r = randint(1, faces)
                    rolls.append(r)
                    total += r
            self.intTotal += -idiv(total * multiplier, divider) if negative else idiv(total * multiplier, divider)
            if len(rolls) > 20:
                rolls.sort()
            self.vvintRes.append(rolls)
            self.vintRes.append(idiv(total * multiplier, divider))
            return 0

        kpos = dice.index("K")
        if len(dice[kpos + 1:]) > 3:
            return VALUE_ERR
        knum = int(dice[kpos + 1:])
        dice = dice[:kpos]
        p = dice.index("D")
        if len(dice[:p]) > 3 or (len(dice[:p]) == 3 and dice[:p] != "100"):
            return DICE_TOO_BIG_ERR
        if len(dice[p + 1:]) > 4:
            return TYPE_TOO_BIG_ERR
        cnt = 1 if len(dice[:p]) == 0 else int(dice[:p])
        faces = int(dice[p + 1:])
        if knum <= 0 or cnt == 0:
            return ZERO_DICE_ERR
        if knum > cnt:
            return VALUE_ERR
        if faces == 0:
            return ZERO_TYPE_ERR
        kept: List[int] = []
        for _ in range(cnt):
            r = randint(1, faces)
            if len(kept) != knum:
                kept.append(r)
            elif r > min(kept):
                kept[kept.index(min(kept))] = r
        total = sum(kept)
        self.intTotal += -idiv(total * multiplier, divider) if negative else idiv(total * multiplier, divider)
        self.vintRes.append(total)
        if len(kept) > 20:
            kept.sort()
        self.vvintRes.append(kept)
        return 0

    def Roll(self, game=None) -> int:
        self.intTotal = 0
        self.vvintRes.clear()
        self.vboolNegative.clear()
        self.vintMultiplier.clear()
        self.vintRes.clear()
        self.vBnP.clear()
        dice = self.strDice
        pos = 0
        if dice and dice[0] == "-":
            self.vboolNegative.append(True)
            pos = 1
        else:
            self.vboolNegative.append(False)
        if dice and dice[-1] in "+-X/":
            return INPUT_ERR
        while True:
            ps = dice.find("+", pos)
            ms = dice.find("-", pos)
            if ps < 0 and ms < 0:
                break
            if ps < 0:
                sym = ms
            elif ms < 0:
                sym = ps
            else:
                sym = min(ps, ms)
            res = self._roll_dice(dice[pos:sym], game)
            if res != 0:
                return res
            pos = sym + 1
            self.vboolNegative.append(dice[sym] == "-")
        return self._roll_dice(dice[pos:], game)

    def Max(self) -> int:
        return self._extreme(True)

    def Min(self) -> int:
        return self._extreme(False)

    def _extreme(self, is_max: bool) -> int:
        self.vboolNegative.clear()
        self.intTotal = 0
        dice = self.strDice
        pos = 0
        if dice and dice[0] == "-":
            self.vboolNegative.append(True)
            pos = 1
        else:
            self.vboolNegative.append(False)
        if dice and dice[-1] in "+-":
            return INPUT_ERR
        while True:
            ps = dice.find("+", pos)
            ms = dice.find("-", pos)
            if ps < 0 and ms < 0:
                break
            if ps < 0:
                sym = ms
            elif ms < 0:
                sym = ps
            else:
                sym = min(ps, ms)
            negative = self.vboolNegative[-1]
            res = self._extreme_one(dice[pos:sym], negative == is_max)
            pos = sym + 1
            self.vboolNegative.append(dice[sym] == "-")
            if res != 0:
                return res
        negative = self.vboolNegative[-1]
        return self._extreme_one(dice[pos:], negative == is_max)

    def _extreme_one(self, dice: str, use_max: bool) -> int:
        negative = self.vboolNegative[-1]
        total = 0
        if "D" in dice:
            p = dice.index("D")
            suffix = dice[p + 1:]
            if any(not c.isdigit() for c in suffix):
                return INPUT_ERR
            if len(suffix) > 3:
                return DICE_TOO_BIG_ERR
            faces = int(suffix)
            prefix = dice[:p]
            if any(not c.isdigit() for c in prefix):
                return INPUT_ERR
            if len(prefix) > 3:
                return DICE_TOO_BIG_ERR
            cnt = int(prefix) if prefix else 1
            total = cnt * faces if use_max else cnt
        else:
            if any(not c.isdigit() for c in dice):
                return INPUT_ERR
            if len(dice) > 3:
                return DICE_TOO_BIG_ERR
            total = int(dice)
        self.intTotal += -total if negative else total
        return 0

    # ------------------------------------------------------------- formatting
    def FormStringSeparate(self) -> str:
        out: List[str] = []
        for idx, group in enumerate(self.vvintRes):
            out.append("-" if self.vboolNegative[idx] else ("+" if idx else ""))
            kind = self.vBnP[idx] if idx < len(self.vBnP) else NORMAL_DICE
            if kind == NORMAL_DICE:
                wrap = len(group) != 1 and (
                    len(self.vvintRes) != 1
                    or self.vintMultiplier[idx] != 1
                    or self.vintDivider[idx] != 1
                    or self.vboolNegative[idx]
                )
                if wrap:
                    out.append("(")
                out.append("+".join(str(v) for v in group))
                if wrap:
                    out.append(")")
                if self.vintMultiplier[idx] != 1:
                    out.append("×%d" % self.vintMultiplier[idx])
                if self.vintDivider[idx] != 1:
                    out.append("/%d" % self.vintDivider[idx])
            elif kind == FUDGE_DICE:
                out.append("[")
                out.append(" ".join("+" if v == 1 else ("0" if v == 0 else "-") for v in group))
                out.append("]")
            elif kind == WW_DICE:
                out.append(self._pool_separate(group))
            else:
                out.append(str(group[0]))
                out.append("[奖励骰:" if kind == B_DICE else "[惩罚骰:")
                out.append(" ".join(str(v) for v in group[1:]))
                out.append("]")
        return "".join(out)

    def _pool_separate(self, group: List[int]) -> str:
        out: List[str] = []
        is_cnt = len(group) >= 100
        if len(group) != group[0] + 1:
            out.append("\\{")
        pos = 0
        while True:
            if pos:
                out.append("\n加骰%d：" % group[pos] if is_cnt else "+")
            out.append("\\{")
            if is_cnt:
                cnt = [0] * 11
                for a in range(pos + 1, pos + group[pos] + 1):
                    cnt[group[a]] += 1
                c = 0
                for i2 in range(1, 11):
                    if cnt[i2]:
                        out.append("(%s:%d)," % (to_circled(i2, self.nTarget), cnt[i2]))
                        c += 1
                        if c % 3 == 1:
                            out.append("\n")
                if out[-1] == "\n":
                    out.pop()
                if out[-1] == ",":
                    out.pop()
            else:
                for a in range(pos + 1, pos + group[pos] + 1):
                    out.append(to_circled(group[a], self.nTarget))
                    if a != pos + group[pos]:
                        out.append(",")
            out.append("}")
            pos = pos + group[pos] + 1
            if pos == len(group):
                break
        if len(group) != group[0] + 1:
            out.append(" }")
        return "".join(out)

    def FormStringCombined(self) -> str:
        out: List[str] = []
        for idx, val in enumerate(self.vintRes):
            out.append("-" if self.vboolNegative[idx] else ("" if idx == 0 else "+"))
            out.append(str(val))
        return "".join(out)

    def FormCompleteString(self) -> str:
        res = self.strDice
        separated = self.FormStringSeparate()
        if self.strDice == separated and len(self.vintRes) == 1:
            return self.strDice
        if len(separated) > 256:
            return self.FormShortString()
        if self.strDice != separated:
            res += "=" + separated
        combined = self.FormStringCombined()
        if separated != combined:
            res += "=" + combined
        if combined != str(self.intTotal):
            res += "=" + str(self.intTotal)
        return res

    def FormShortString(self) -> str:
        res = self.strDice
        combined = self.FormStringCombined()
        if self.strDice != combined:
            res += "=" + combined
        if combined != str(self.intTotal):
            res += "=" + str(self.intTotal)
        return res


class DicePool(RD):
    """骰池（.ww / .w）。"""

    def __init__(self, expr: str, target: int = 8):
        super().__init__(expr, 10)
        self.nDiceCnt = 0
        self.nDiceAdd = 10
        self.nTarget = target
        self.nExtraVal = 0
        self.err = 0
        self.nSuccess = 0
        pos = 0
        neg = [self.strDice.startswith("-")]
        if neg[0]:
            pos = 1
        while True:
            ps = self.strDice.find("+", pos)
            ms = self.strDice.find("-", pos)
            if ps < 0 and ms < 0:
                break
            sym = ms if ps < 0 else (ps if ms < 0 else min(ps, ms))
            res = self._cnt_dice(self.strDice[pos:sym], neg[-1])
            if res != 0:
                self.err = res
                break
            pos = sym + 1
            neg.append(self.strDice[sym] == "-")
        else:
            sym = None
        if self.err == 0:
            tail = self.strDice[pos:] if pos else self.strDice
            res = self._cnt_dice(tail, neg[-1] if neg else False)
            if res != 0:
                self.err = res

    def _cnt_dice(self, dice: str, negative: bool) -> int:
        i = 0
        while i < len(dice) and dice[i].isdigit():
            i += 1
        cnt_str = dice[:i]
        if any(not c.isdigit() for c in cnt_str):
            return DICE_CNT_ERR
        if len(cnt_str) > 4:
            return DICE_TOO_BIG_ERR
        cnt = int(cnt_str) if cnt_str else 0
        if len(dice) == len(cnt_str):
            if negative:
                self.nExtraVal -= cnt
            else:
                self.nExtraVal += cnt
        else:
            if cnt == 0:
                return ZERO_DICE_ERR
            if negative:
                self.nDiceCnt -= cnt
            else:
                self.nDiceCnt += cnt
            add = dice[len(cnt_str) + 1:]
            if len(add) > 2:
                return ADD_DICE_VAL_ERR
            if any(not c.isdigit() for c in add):
                return INPUT_ERR
            if add:
                self.nDiceAdd = int(add)
                if self.nDiceAdd < 5 or self.nDiceAdd > 11:
                    return ADD_DICE_VAL_ERR
        return 0

    def roll(self, game=None) -> int:
        if self.err:
            return self.err
        if not self.nDiceCnt:
            return ZERO_DICE_ERR
        # 原版 roll() 会把 nDiceCnt 一路消费到 0，于是 .ww 3#10a8 从第二遍起直接
        # 返回 ZeroDice_Err（调用方不看返回值），三行输出完全一样。此处用完即还。
        total_dice = self.nDiceCnt
        self.vvintRes.clear()
        self.vintRes.clear()
        self.vboolNegative.clear()
        self.intTotal = self.nExtraVal
        while self.nDiceCnt != 0:
            group = [self.nDiceCnt]
            hits = 0
            add_num = 0
            is_pos = self.nDiceCnt > 0
            cnt = self.nDiceCnt if is_pos else -self.nDiceCnt
            for _ in range(cnt):
                r = randint(1, 10)
                group.append(r)
                if r >= self.nTarget:
                    hits += 1
                if r >= self.nDiceAdd:
                    add_num += 1
            if self.nDiceCnt > 10:
                group[1:] = sorted(group[1:])
            if hits or not self.vintRes:
                self.vboolNegative.append(not is_pos)
                self.vvintRes.append(group)
                self.vintRes.append(hits)
            self.nDiceCnt = add_num if is_pos else -add_num
            self.intTotal += hits
        if self.nExtraVal:
            self.vintRes.append(abs(self.nExtraVal))
            self.vboolNegative.append(self.nExtraVal < 0)
        self.vBnP = [WW_DICE] * len(self.vvintRes)
        self.nDiceCnt = total_dice
        return 0

    def FormStringSeparate(self) -> str:
        out: List[str] = []
        for idx, group in enumerate(self.vvintRes):
            out.append("-" if idx < len(self.vboolNegative) and self.vboolNegative[idx] else ("+" if idx else ""))
            out.append(self._pool_separate(group))
        return "".join(out)


# --------------------------------------------------------------- 人物作成文本
def coc7d() -> str:
    rd3 = RD("3D6")
    rd2 = RD("2D6+6")
    parts = []
    for name, rd, expr in (
        ("力量STR", rd3, "3D6*5="),
        ("体质CON", rd3, "3D6*5="),
        ("体型SIZ", rd2, "(2D6+6)*5="),
        ("敏捷DEX", rd3, "3D6*5="),
        ("外貌APP", rd3, "3D6*5="),
        ("智力INT", rd2, "(2D6+6)*5="),
        ("意志POW", rd3, "3D6*5="),
        ("教育EDU", rd2, "(2D6+6)*5="),
    ):
        rd.Roll()
        parts.append((name, expr, rd.intTotal * 5))
    rd3.Roll()
    luck = rd3.intTotal * 5
    out = ["\n"]
    for name, expr, val in parts:
        out.append("%s=%s%d/%d/%d\n" % (name, expr, val, val // 2, val // 5))
    out.append("幸运LUCK=3D6*5=%d\n" % luck)
    vals = {name: val for name, _, val in parts}
    total8 = sum(val for _, _, val in parts)
    out.append("共计:%d/%d\n" % (total8, total8 + luck))
    powv = vals["意志POW"]
    siz = vals["体型SIZ"]
    con = vals["体质CON"]
    strv = vals["力量STR"]
    dex = vals["敏捷DEX"]
    out.append("理智SAN=POW=%d\n" % powv)
    out.append("生命值HP=(SIZ+CON)/10=%d\n" % ((siz + con) // 10))
    out.append("魔法值MP=POW/5=%d\n" % (powv // 5))
    s = strv + siz
    if 2 <= s <= 64:
        db, build = "-2", -2
    elif 65 <= s <= 84:
        db, build = "-1", -1
    elif 85 <= s <= 124:
        db, build = "0", 0
    elif 125 <= s <= 164:
        db, build = "1D4", 1
    elif 165 <= s <= 204:
        db, build = "1d6", 2
    else:
        db, build = "计算错误!", -10
    out.append("伤害奖励DB=%s\n体格=%s\n" % (db, "计算错误" if build == -10 else build))
    if dex < siz and strv < siz:
        mov = 7
    elif dex > siz and strv > siz:
        mov = 9
    else:
        mov = 8
    out.append("移动力MOV=%d" % mov)
    return "".join(out)


def coc6d() -> str:
    rd3 = RD("3D6")
    rd2 = RD("2D6+6")
    rd33 = RD("3D6+3")
    rd1 = RD("1D10")
    out = ["\n"]
    vals = {}
    for name, rd, expr in (
        ("力量STR", rd3, "3D6"),
        ("体质CON", rd3, "3D6"),
        ("体型SIZ", rd2, "2D6+6"),
        ("敏捷DEX", rd3, "3D6"),
        ("外貌APP", rd3, "3D6"),
        ("智力INT", rd2, "2D6+6"),
        ("意志POW", rd3, "3D6"),
        ("教育EDU", rd33, "3D6+3"),
    ):
        rd.Roll()
        vals[name] = rd.intTotal
        out.append("%s=%s=%d\n" % (name, expr, rd.intTotal))
    total = sum(vals.values())
    out.append("共计:%d\n" % total)
    powv = vals["意志POW"]
    intv = vals["智力INT"]
    eduv = vals["教育EDU"]
    out.append("理智SAN=POW*5=%d\n" % (powv * 5))
    out.append("灵感IDEA=INT*5=%d\n" % (intv * 5))
    out.append("幸运LUCK=POW*5=%d\n" % (powv * 5))
    out.append("知识KNOW=EDU*5=%d\n" % (eduv * 5))
    out.append("生命值HP=(CON+SIZ)/2=%d\n" % math.ceil((vals["体质CON"] + vals["体型SIZ"]) / 2.0))
    out.append("魔法值MP=POW=%d\n" % powv)
    s = vals["力量STR"] + vals["体型SIZ"]
    if 2 <= s <= 12:
        db = "-1D6"
    elif 13 <= s <= 16:
        db = "-1D4"
    elif 17 <= s <= 24:
        db = "0"
    elif 25 <= s <= 32:
        db = "1D4"
    elif 33 <= s <= 40:
        db = "1D6"
    else:
        db = "计算错误!"
    out.append("伤害奖励DB=%s\n" % db)
    rd1.Roll()
    out.append("资产=1D10=%d" % rd1.intTotal)
    return "".join(out)


def coc7(num: int) -> str:
    props = ["力量", "体质", "体型", "敏捷", "外貌", "智力", "意志", "教育", "幸运"]
    exps = ["3D6", "3D6", "2D6+6", "3D6", "3D6", "2D6+6", "3D6", "2D6+6", "3D6"]
    out = []
    for _ in range(num):
        out.append("\n")
        all_total = 0
        luck = 0
        for i in range(9):
            rd = RD(exps[i])
            rd.Roll()
            val = rd.intTotal * 5
            out.append("%s:%d " % (props[i], val))
            all_total += val
            luck = val
        out.append("共计:%d/%d" % (all_total - luck, all_total))
    return "".join(out)


def coc6(num: int) -> str:
    props = ["力量", "体质", "体型", "敏捷", "外貌", "智力", "意志", "教育", "资产"]
    exps = ["3D6", "3D6", "2D6+6", "3D6", "3D6", "2D6+6", "3D6", "3D6+3", "1D10"]
    add_space = num != 1
    out = []
    for _ in range(num):
        out.append("\n")
        all_total = 0
        for i in range(8):
            rd = RD(exps[i])
            rd.Roll()
            out.append("%s:%d " % (props[i], rd.intTotal))
            if add_space and rd.intTotal < 10:
                out.append("  ")
            all_total += rd.intTotal
        out.append("共计:%d" % all_total)
        rd = RD(exps[8])
        rd.Roll()
        out.append(" %s:%d" % (props[8], rd.intTotal))
    return "".join(out)


def dnd(num: int) -> str:
    rd = RD("4D6K3")
    out = []
    for _ in range(num):
        out.append("\n")
        pool = []
        all_total = 0
        for _ in range(6):
            rd.Roll()
            pool.append(rd.intTotal)
            all_total += rd.intTotal
        for v in sorted(pool, reverse=True):
            if v < 10:
                out.append(" ")
            out.append(str(v) + " /")
        out.append("总计%d" % all_total)
    return "".join(out)


# ------------------------------------------------------------------ 疯狂症状
def temp_insane() -> dict:
    sym = randint(1, 10)
    res = {"res": "1D10=%d\n症状: %s" % (sym, TEMP_INSANITY[sym]),
           "dur": "1D10=%d" % randint(1, 10)}
    if sym == 9:
        d = randint(1, 100)
        res["detail_roll"] = "1D100=%d" % d
        res["detail"] = STR_FEAR[d]
    elif sym == 10:
        d = randint(1, 100)
        res["detail_roll"] = "1D100=%d" % d
        res["detail"] = STR_PANIC[d]
    return res


def long_insane() -> dict:
    sym = randint(1, 10)
    res = {"res": "1D10=%d\n症状: %s" % (sym, LONG_INSANITY[sym]),
           "dur": "1D10=%d" % randint(1, 10)}
    if sym == 9:
        d = randint(1, 100)
        res["detail_roll"] = "1D100=%d" % d
        res["detail"] = STR_FEAR[d]
    elif sym == 10:
        d = randint(1, 100)
        res["detail_roll"] = "1D100=%d" % d
        res["detail"] = STR_PANIC[d]
    return res


# ------------------------------------------------------------------ 成功等级
class SuccessLevel:
    Fumble = 0
    Failure = 1
    RegularSuccess = 2
    HardSuccess = 3
    ExtremeSuccess = 4
    Critical = 5


def roll_success_level(res: int, rate: int, rule: int) -> int:
    if rule == 0:
        if res == 100:
            return SuccessLevel.Fumble
        if res == 1:
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        if rate >= 50 or res < 96:
            return SuccessLevel.Failure
        return SuccessLevel.Fumble
    if rule == 1:
        if res == 100:
            return SuccessLevel.Fumble
        if res == 1 or (res <= 5 and rate >= 50):
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        if rate >= 50 or res < 96:
            return SuccessLevel.Failure
        return SuccessLevel.Fumble
    if rule == 2:
        if res == 100:
            return SuccessLevel.Fumble
        if res <= 5 and res <= rate:
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        if res < 96:
            return SuccessLevel.Failure
        return SuccessLevel.Fumble
    if rule == 3:
        if res >= 96:
            return SuccessLevel.Fumble
        if res <= 5:
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        return SuccessLevel.Failure
    if rule == 4:
        if res == 100:
            return SuccessLevel.Fumble
        if res <= 5 and res <= idiv(rate, 10):
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        if rate >= 50 or res < 96 + idiv(rate, 10):
            return SuccessLevel.Failure
        return SuccessLevel.Fumble
    if rule == 5:
        if res >= 99:
            return SuccessLevel.Fumble
        if res <= 2 and res < idiv(rate, 10):
            return SuccessLevel.Critical
        if res <= idiv(rate, 5):
            return SuccessLevel.ExtremeSuccess
        if res <= idiv(rate, 2):
            return SuccessLevel.HardSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        if rate >= 50 or res < 96:
            return SuccessLevel.Failure
        return SuccessLevel.Fumble
    if rule == 6:
        if res > rate:
            if res == 100 or res % 11 == 0:
                return SuccessLevel.Fumble
            return SuccessLevel.Failure
        if res == 1 or res % 11 == 0:
            return SuccessLevel.Critical
        return SuccessLevel.RegularSuccess
    if rule == 7:
        if res >= 100:
            return SuccessLevel.Fumble
        if res >= 96:
            if idiv(90 - rate, 20) + res >= 100:
                return SuccessLevel.Fumble
            return SuccessLevel.Failure
        if res == 1 or res <= round(idiv(rate, 5)):
            return SuccessLevel.ExtremeSuccess
        if res <= rate:
            return SuccessLevel.RegularSuccess
        return SuccessLevel.Failure
    return SuccessLevel.Failure


def skill_name(key: str) -> str:
    """大小写不敏感的技能名替换（对应 dict_ci<SkillNameReplace>）。"""
    low = key.lower()
    for k, v in SKILL_NAME_REPLACE.items():
        if k.lower() == low:
            return v
    return key


def skill_default(key: str) -> Optional[int]:
    low = key.lower()
    for k, v in SKILL_DEFAULT_VAL.items():
        if k.lower() == low:
            return v
    return None
