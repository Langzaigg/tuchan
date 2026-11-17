# -*- coding: utf-8 -*-
"""指令调度：BasicOrder / InnerOrder，对应 DiceEvent.cpp。"""

import asyncio
import math
import random
import time
from typing import List, Optional

from .card import (
    CharaCard,
    MODEL_COC7,
    PLAYER_ERRORS,
    TEXT_DICEEXP,
    AttrShape,
    idx_pc,
)
from .cloud import fetch_jrrp, fetch_rule, local_jrrp
from .decks import decks
from .decks import draw_card as deck_draw_card
from .settings import settings
from .constants import (
    CHAT_CONF,
    DICE_SHORT_VER,
    GLOBAL_HELP,
    GLOBAL_MSG,
    get_help_text,
    get_msg_text,
)
from .event import DiceEvent, normalize_message
from .formatter import ResList, ShowList, at_token, strip
from .names import filter_cqcode, get_name, print_group, print_user
from .rd import (
    ADD_DICE_VAL_ERR,
    DICE_TOO_BIG_ERR,
    DicePool,
    RD,
    SuccessLevel,
    coc6,
    coc6d,
    coc7,
    coc7d,
    dnd,
    idiv,
    long_insane,
    roll_success_level,
    skill_default,
    skill_name,
    temp_insane,
)
from .session import DeckInfo, sessions
from .store import store

ROLL_DICE_ERR = {
    -1: "strValueErr",
    -2: "strInputErr",
    -3: "strZeroDiceErr",
    -4: "strZeroTypeErr",
    -5: "strDiceTooBigErr",
    -6: "strTypeTooBigErr",
    -7: "strAddDiceValErr",
    -8: "strDiceCntErr",
}


# --------------------------------------------------------------------- 工具
def player_of(e: DiceEvent):
    return store.player(e.uid)


def card_of(e: DiceEvent) -> CharaCard:
    return player_of(e).get_card("", e.gid)


def game_of(e: DiceEvent):
    if e.game is None:
        e.game = sessions.get_if(e.gid, e.uid)
    return e.game


def reply_roll_err(e: DiceEvent, err: int, rd: RD):
    if not err:
        return
    if err == -2:
        e.ctx["dice_exp"] = rd.strDice
    key = ROLL_DICE_ERR.get(err)
    if key is None:
        e.reply_msg("strUnknownErr")
        return
    e.reply_msg(key)


def _sub(text: str, **kw) -> str:
    """只替换指定的 {key}，避免与 Dice 模板里的 {self} 等冲突。"""
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text


def _int_or_text(val: str):
    try:
        return int(val)
    except (TypeError, ValueError):
        return val


def _read_ini(text: str):
    if "=" not in text:
        return text.strip(), ""
    k, _, v = text.partition("=")
    return k.strip(), v.strip()


def _search_help(word: str) -> List[str]:
    if not word:
        return []
    low = word.lower()
    if low in GLOBAL_HELP:
        return [word]
    prefix = [k for k in GLOBAL_HELP if k.startswith(low)]
    if prefix:
        return prefix
    return [k for k in GLOBAL_HELP if low in k]


# ------------------------------------------------------------------ 帮助文档
def do_help(e: DiceEvent):
    word = e.read_rest()
    e.ctx["help_word"] = word
    e.ctx["option"] = "禁用help"
    if not e.is_private:
        if e.ctx.get("help_word") in ("on", "off"):
            if not e.can_host:
                e.reply_msg("strPermissionDeniedErr")
                return 1
        if word == "off":
            if store.group_opt(e.gid, "禁用help"):
                e.reply_msg("strGroupSetOnAlready")
            else:
                store.set_group_opt(e.gid, "禁用help")
                e.reply_msg("strGroupSetOn")
            return 1
        if word == "on":
            if store.group_opt(e.gid, "禁用help"):
                store.reset_group_opt(e.gid, "禁用help")
                e.reply_msg("strGroupSetOff")
            else:
                e.reply_msg("strGroupSetOffAlready")
            return 1
        if store.group_opt(e.gid, "禁用help"):
            e.reply_msg("strHELPDisabledErr")
            return 1
    if not word:
        e.reply(get_msg_text("strBotHeader") + DICE_SHORT_VER + "\n"
                + get_msg_text("strHlpMsg"))
        return 1
    hit = _search_help(word)
    if len(hit) == 1 and hit[0].lower() == word.lower():
        e.reply(_fmt_help(e, hit[0]), False)
        return 1
    if len(hit) == 1:
        key = hit[0]
        e.ctx["redirect_key"] = key
        e.ctx["redirect_res"] = _fmt_help(e, key)
        e.reply_msg("strHelpRedirect")
        return 1
    if hit:
        res = ShowList()
        for key in sorted(hit, key=lambda k: (-len(k), k))[:21]:
            res.add(".help " + key)
        e.ctx["res"] = res.show("\n")
        e.reply_msg("strHelpSuggestion")
        return 1
    e.reply_msg("strHelpNotFound")
    return 1


def _fmt_help(e: DiceEvent, key: str) -> str:
    from .formatter import format_msg
    doc = get_help_text(key)
    if not doc:
        return ""
    return format_msg(doc, e.ctx, e.resolve, GLOBAL_HELP, True)


# ------------------------------------------------------------------ 基础指令
async def basic_order(e: DiceEvent, bot) -> int:
    if not e.msg.startswith("."):
        return 0
    e.cnt = 1
    e.skip_space()
    lower = e.lower
    n = e.cnt

    if lower[n:n + 9] == "authorize":
        return 0  # 权限/授权类：不复刻
    if lower[n:n + 7] == "dismiss":
        return 0  # 退群：不复刻
    if lower[n:n + 7] == "warning":
        return 0  # 云黑名单：不复刻
    if lower[n:n + 6] == "master":
        return 0  # 认主：不复刻
    if lower[n:n + 3] == "bot":
        return await _bot(e, bot)
    if lower[n:n + 5] == "reply":
        return 0  # 自定义回复依赖 mod：不复刻
    if lower[n:n + 4] == "chan":
        return 0
    if lower[n:n + 7] == "helpdoc":
        return 0
    if lower[n:n + 4] == "help":
        e.cnt += 4
        while e.peek_lower() == " ":
            e.cnt += 1
        return do_help(e)
    if lower[n:n + 4] == "game":
        e.cnt += 4
        return await _game(e, bot)
    return 0


async def _bot(e: DiceEvent, bot) -> int:
    e.cnt += 3
    command = e.read_para()
    qq = e.read_digit()
    self_id = str(e.self_id)
    if not qq or qq == self_id or (len(qq) == 4 and self_id.isdigit()
                                   and int(qq) == e.self_id % 10000):
        if command == "on" and not e.is_private:
            if e.can_host:
                if store.group_opt(e.gid, "停用指令"):
                    store.reset_group_opt(e.gid, "停用指令")
                    e.reply_msg("strBotOn")
                else:
                    e.reply_msg("strBotOnAlready")
            else:
                e.reply_msg("strPermissionDeniedErr")
        elif command == "off" and not e.is_private:
            if e.can_host:
                if store.group_opt(e.gid, "停用指令"):
                    e.reply_msg("strBotOffAlready")
                else:
                    store.set_group_opt(e.gid, "停用指令")
                    e.reply_msg("strBotOff")
            else:
                e.reply_msg("strPermissionDeniedErr")
        else:
            e.reply(get_msg_text("strBotHeader") + _full_ver(e) + get_msg_text("strBotMsg"))
        return 1
    return 1


def _full_ver(e: DiceEvent) -> str:
    return "%s on\nNoneBot2(OneBot v11)" % DICE_SHORT_VER


# -------------------------------------------------------------------- .game
async def _game(e: DiceEvent, bot) -> int:
    action = e.read_para()
    if not action:
        e.reply_help("game")
        return 1
    if action == "new":
        if e.can_host:
            name = e.read_file_name()
            s = sessions.new_game(name, e.gid, e.uid)
            e.ctx["game_id"] = s.name
            e.reply_msg("strGameNew")
        else:
            e.reply_msg("strGameMasterDenied")
        return 1
    if action == "open":
        name = e.read_file_name()
        game = sessions.get_by_name(name)
        e.ctx["game_id"] = name
        if game is None:
            e.reply_msg("strGameNotExist")
        elif game.is_gm(e.uid) or (e.is_private and game.is_pl(e.uid)):
            sessions.open(game, e.gid, e.uid)
            e.game = game
            e.reply_msg("strGameAreaOpen")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "master":
        game = sessions.get(e.gid, e.uid)
        e.game = game
        if not game.is_gm(e.uid):
            if e.can_host:
                game.add_gm(e.uid)
                e.reply_msg("strGameMastered")
            else:
                e.reply_msg("strGameMasterDenied")
        else:
            res = ShowList()
            for uid in game.master:
                res.add(print_user(uid, e.self_id, e.self_name))
            e.ctx["items"] = res.show("\n")
            e.reply_msg("strGameMasterList")
        return 1

    game = game_of(e)
    if game is None:
        e.reply_msg("strGameVoidHere")
        return 1

    if action == "state":
        e.reply(game.show(), False)
        return 1
    if action == "set":
        if game.is_gm(e.uid):
            item, val = _read_ini(e.msg[e.cnt:])
            if item:
                e.ctx["set_item"] = item
                if val:
                    tv = _int_or_text(val)
                    e.ctx["set_val"] = tv
                    game.set(item, tv)
                    e.reply_msg("strGameItemSet")
                else:
                    e.ctx["set_val"] = game.get(item)
                    e.reply_msg("strGameItemShow")
            else:
                e.reply_msg("strGameItemEmpty")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "join":
        if game.add_pl(e.uid):
            e.reply_msg("strGameJoined")
        else:
            e.reply_msg("strGamePlayerAlready")
        return 1
    if action == "call":
        if game.is_gm(e.uid):
            if game.player:
                e.ctx["items"] = "\n".join(at_token(u) for u in game.player)
                e.reply_msg("strGamePlayerCall")
            else:
                e.reply_msg("strGamePlayerEmpty")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "close":
        if game.is_gm(e.uid):
            e.ctx["game_id"] = game.name
            sessions.close(e.gid, e.uid)
            e.reply_msg("strGameAreaClosed")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "over":
        if game.is_gm(e.uid):
            e.ctx["game_id"] = game.name
            sessions.over(e.gid, e.uid)
            e.reply_msg("strGameOver")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "exit":
        if game.del_pl(e.uid) or game.del_gm(e.uid):
            e.reply_msg("strGameExited")
        else:
            e.reply_msg("strGameNotJoined")
        return 1
    if action == "kick":
        if game.is_gm(e.uid):
            tid = e.read_id()
            e.ctx["tid"] = tid if tid else ""
            if game.del_pl(tid) or game.del_ob(tid) or game.del_gm(tid):
                e.reply_msg("strGameKicked")
            else:
                e.reply_msg("strGameKickNotPlayer")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if action == "rou":
        e.skip_space()
        if e.peek().isdigit():
            if game.is_gm(e.uid):
                st, nface = e.read_num(100)
                if st == 0 and nface <= 100:
                    e.ctx["face"] = nface
                    ncopy = 1
                    if e.peek() == "*":
                        st2, ncopy = e.read_num(1)
                        if st2 == 0:
                            if ncopy <= 0:
                                e.reply_msg("strZeroDiceErr")
                                return 1
                            if nface * ncopy > 100:
                                e.reply_msg("strGameRouletteTooBig")
                                return 1
                    from .session import DiceRoulette
                    game.roulette[nface] = DiceRoulette(nface, ncopy)
                    game.touch()
                    e.reply_msg("strGameRouletteSet")
                else:
                    e.reply_msg("strGameRouletteTooBig")
            else:
                e.reply_msg("strGameNotMaster")
            return 1
        sub = e.read_para()
        if sub == "hist":
            if game.roulette:
                hist = ShowList()
                for face, rou in game.roulette.items():
                    hist.add("D%d=%s" % (face, rou.hist()))
                e.ctx["hist"] = hist.show("\n")
                e.reply_msg("strGameRouletteHistory")
            else:
                e.reply_msg("strGameRouletteEmpty")
        elif sub == "reset":
            if game.is_gm(e.uid):
                for rou in game.roulette.values():
                    rou.reset()
                e.reply_msg("strGameRouletteReset")
            else:
                e.reply_msg("strGameNotMaster")
        elif sub == "clr":
            if game.is_gm(e.uid):
                game.roulette.clear()
                game.touch()
                e.reply_msg("strGameRouletteClear")
            else:
                e.reply_msg("strGameNotMaster")
        else:
            e.reply_help("roulette")
        return 1
    e.reply_help("game")
    return 1


# ------------------------------------------------------------------ 主调度
async def inner_order(e: DiceEvent, bot) -> int:
    if not e.msg.startswith("."):
        return 0
    lower = e.lower
    n = e.cnt

    if lower[n:n + 6] == "setcoc":
        e.cnt += 6
        return _setcoc(e)
    if lower[n:n + 6] == "system":
        e.cnt += 6
        return _system(e)
    if lower[n:n + 6] == "groups" or lower[n:n + 5] in ("admin", "cloud"):
        return 0  # 群管 / 权限 / 云黑：不复刻
    if lower[n:n + 5] in ("coc7d", "coc6d") or lower[n:n + 4] == "cocd":
        if lower[n:n + 4] == "cocd":
            e.cnt += 4
            e.ctx["res"] = coc7d()
        elif lower[n:n + 5] == "coc7d":
            e.cnt += 5
            e.ctx["res"] = coc7d()
        else:
            e.cnt += 5
            e.ctx["res"] = coc6d()
        e.reply_msg("strCOCBuild")
        return 1
    if lower[n:n + 5] == "group":
        return 0  # 群管：不复刻
    if lower[n:n + 4] == "coc6":
        e.cnt += 4
        if e.peek_lower() == "s":
            e.cnt += 1
        e.skip_space()
        start = e.cnt
        while e.peek().isdigit():
            e.cnt += 1
        num = e.msg[start:e.cnt]
        if len(num) > 2:
            e.reply_msg("strBuildTooMany")
            return 1
        count = int(num) if num else 1
        if count > 10:
            e.reply_msg("strBuildTooMany")
            return 1
        if count == 0:
            e.reply_msg("strCharacterCannotBeZero")
            return 1
        e.ctx["res"] = coc6(count)
        e.reply_msg("strCOCBuild")
        return 1
    if lower[n:n + 4] == "deck":
        e.cnt += 4
        return await _deck(e, bot)
    if lower[n:n + 4] == "draw":
        e.cnt += 4
        return _draw(e)
    if lower[n:n + 4] == "name":
        e.cnt += 4
        return _name(e)
    if lower[n:n + 4] == "init":
        e.cnt += 4
        return _init(e)
    if lower[n:n + 4] == "jrrp":
        e.cnt += 4
        return await _jrrp(e)
    if lower[n:n + 4] == "link":
        return 0
    if lower[n:n + 4] == "rule":
        e.cnt += 4
        return await _rule(e)
    if lower[n:n + 4] == "send":
        return 0
    if lower[n:n + 4] == "user":
        return 0
    if lower[n:n + 3] == "coc":
        e.cnt += 3
        if e.peek_lower() == "7":
            e.cnt += 1
        if e.peek_lower() == "s":
            e.cnt += 1
        e.skip_space()
        start = e.cnt
        while e.peek_lower().isdigit():
            e.cnt += 1
        num = lower[start:e.cnt]
        if len(num) > 1 and num != "10":
            e.reply_msg("strBuildTooMany")
            return 1
        count = int(num) if num else 1
        if count == 0:
            e.reply_msg("strCharacterCannotBeZero")
            return 1
        e.ctx["res"] = coc7(count)
        e.reply_msg("strCOCBuild")
        return 1
    if lower[n:n + 3] == "dnd":
        e.cnt += 3
        e.skip_space()
        start = e.cnt
        while e.peek_lower().isdigit():
            e.cnt += 1
        num = lower[start:e.cnt]
        if len(num) > 1 and num != "10":
            e.reply_msg("strBuildTooMany")
            return 1
        count = int(num) if num else 1
        if count == 0:
            e.reply_msg("strCharacterCannotBeZero")
            return 1
        e.ctx["res"] = dnd(count)
        e.reply_msg("strDNDBuild")
        return 1
    if lower[n:n + 3] == "log":
        e.cnt += 3
        return await run_log(e, bot)
    if lower[n:n + 3] == "mod":
        return 0
    if lower[n:n + 3] == "nnn":
        e.cnt += 3
        return _nnn(e)
    if lower[n:n + 3] == "set":
        e.cnt += 3
        return _set(e)
    if lower[n:n + 3] == "str":
        return 0
    if lower[n:n + 2] == "ak":
        e.cnt += 2
        return _ak(e)
    if lower[n:n + 2] == "en":
        e.cnt += 2
        return _en(e)
    if lower[n:n + 2] == "li":
        e.cnt += 2
        return _li(e)
    if lower[n:n + 2] == "me":
        e.cnt += 2
        return await _me(e, bot)
    if lower[n:n + 2] == "nn":
        e.cnt += 2
        return _nn(e)
    if lower[n:n + 2] == "ob":
        e.cnt += 2
        return _ob(e)
    if lower[n:n + 2] == "pc":
        e.cnt += 2
        return _pc(e)
    if lower[n:n + 2] in ("ra", "rc"):
        e.cnt += 2
        return _rc(e)
    if lower[n:n + 2] == "ri":
        e.cnt += 2
        return _ri(e)
    if lower[n:n + 2] == "sc":
        e.cnt += 2
        return _sc(e)
    if lower[n:n + 2] == "st":
        e.cnt += 2
        return _st(e)
    if lower[n:n + 2] == "ti":
        e.cnt += 2
        return _ti(e)
    if lower[n:n + 1] == "w":
        e.cnt += 1
        return _ww(e)
    if lower[n:n + 1] in ("r", "h"):
        if lower[n:n + 1] == "h":
            e.hidden = True
        e.cnt += 1
        return _r(e)
    return 0


# ------------------------------------------------------------------- .setcoc
SETCOC_TEXT = {
    0: "0 规则书\n出1大成功\n不满50出96-100大失败，满50出100大失败",
    1: "1\n不满50出1大成功，满50出1-5大成功\n不满50出96-100大失败，满50出100大失败",
    2: "2\n出1-5且<=成功率大成功\n出100或出96-99且>成功率大失败",
    3: "3\n出1-5大成功\n出96-100大失败",
    4: "4\n出1-5且<=十分之一大成功\n不满50出>=96+十分之一大失败，满50出100大失败",
    5: "5\n出1-2且<五分之一大成功\n不满50出96-100大失败，满50出99-100大失败",
    6: "6-绿色三角洲\n出1或出个位十位相同且<=成功率大成功\n出100或出个位十位相同且>成功率大失败",
    7: "7-BRP\n简单难度成功率翻倍，五分之一成功率特殊成功\n出96-100必定失败且视成功率判大失败",
}


def _setcoc(e: DiceEvent) -> int:
    if not e.can_host:
        e.reply_msg("strPermissionDeniedErr")
        return 1
    action = e.read_para()
    game = game_of(e)
    if action == "show":
        rule = None
        if game is not None and game.has("rr_rc"):
            rule = game.get("rr_rc")
        elif e.is_private:
            rule = store.user_conf(e.uid, "rc房规")
        else:
            rule = store.group_rule(e.gid)
        if rule is None:
            e.ctx["rule"] = settings.default_coc_rule
            e.reply_msg("strDefaultCOCShowDefault")
        else:
            e.ctx["rule"] = rule
            e.reply_msg("strDefaultCOCShow")
        return 1
    if action == "clr":
        if game is not None and game.has("rr_rc"):
            game.reset("rr_rc")
        elif e.is_private:
            store.rm_user_conf(e.uid, "rc房规")
        else:
            store.rm_group_rule(e.gid)
        e.reply_msg("strDefaultCOCClr")
        return 1
    rule_str = e.read_digit()
    if not rule_str:
        e.reply_help("setcoc")
        return 1
    if len(rule_str) > 1:
        e.reply_msg("strDefaultCOCNotFound")
        return 1
    rule = int(rule_str)
    if rule not in SETCOC_TEXT:
        e.reply_msg("strDefaultCOCNotFound")
        return 1
    e.reply(get_msg_text("strDefaultCOCSet") + SETCOC_TEXT[rule])
    if e.is_private:
        store.set_user_conf(e.uid, "rc房规", rule)
    elif game is not None:
        game.set("rr_rc", rule)
    else:
        store.set_group_rule(e.gid, rule)
    return 1


# ------------------------------------------------------------------- .system
def _system(e: DiceEvent) -> int:
    """控制台指令的本地版：只保留配置重载与强制存盘（对应原版 .system load / save）。"""
    if not e.is_master:
        e.reply_msg("strPermissionDeniedErr")
        return 1
    opt = e.read_para()
    if opt == "save":
        store.flush_all()
        e.reply("已手动保存{self}的数据√")
        return 1
    if opt == "load":
        settings.load()
        decks.load()
        e.reply("已手动加载{self}的配置√")
        return 1
    return 0


# -------------------------------------------------------------------- .init
def _init(e: DiceEvent) -> int:
    e.ctx["table_name"] = "先攻"
    opt = e.read_para()
    if not opt or e.is_private:
        e.reply_help("init")
        return 1
    game = game_of(e)
    if game is None or not game.has("先攻"):
        e.reply_msg("strGMTableNotExist")
    elif opt in ("show", "list"):
        e.ctx["res"] = game.table_prior_show("先攻")
        e.reply_msg("strGMTableShow")
    elif opt == "del":
        item = e.read_rest()
        e.ctx["table_item"] = item
        if not item:
            e.reply_msg("strGMTableItemEmpty")
        elif game.table_del("先攻", item):
            game.table_del("init_exp", item)
            e.reply_msg("strGMTableItemDel")
        else:
            e.reply_msg("strGMTableItemNotFound")
    elif opt == "clr":
        game.reset("先攻")
        game.reset("init_exp")
        e.reply_msg("strGMTableClr")
    return 1


# -------------------------------------------------------------------- .jrrp
async def _jrrp(e: DiceEvent) -> int:
    if settings.disabled_jrrp:
        e.reply("&strDisabledJrrpGlobal")
        return 1
    e.skip_space()
    command = e.lower[e.cnt:e.msg.find(" ", e.cnt) if e.msg.find(" ", e.cnt) >= 0 else len(e.msg)]
    if not e.is_private:
        if command == "on":
            if e.can_host:
                if store.group_opt(e.gid, "禁用jrrp"):
                    store.reset_group_opt(e.gid, "禁用jrrp")
                    e.reply("成功在本群中启用JRRP!")
                else:
                    e.reply("在本群中JRRP没有被禁用!")
            else:
                e.reply_msg("strPermissionDeniedErr")
            return 1
        if command == "off":
            if e.can_host:
                if store.group_opt(e.gid, "禁用jrrp"):
                    e.reply("在本群中JRRP没有被启用!")
                else:
                    store.set_group_opt(e.gid, "禁用jrrp")
                    e.reply("成功在本群中禁用JRRP!")
            else:
                e.reply_msg("strPermissionDeniedErr")
            return 1
        if store.group_opt(e.gid, "禁用jrrp"):
            e.reply("在本群中JRRP功能已被禁用")
            return 1
    cached = store.jrrp_cache(e.uid)
    if cached is not None:
        e.ctx["res"] = cached
        e.reply_msg("strJrrp")
        return 1
    val, err = await fetch_jrrp(e.self_id, e.uid)
    if val is None:
        val = store.user_conf(e.uid, "jrrp_local")
        if val is None:
            val = local_jrrp(e.uid)
            store.set_user_conf(e.uid, "jrrp_local", val)
    else:
        store.set_jrrp(e.uid, val)
    e.ctx["res"] = val
    e.reply_msg("strJrrp")
    return 1


# -------------------------------------------------------------------- .rule
async def _rule(e: DiceEvent) -> int:
    while e.peek().isspace():
        e.cnt += 1
    if e.cnt >= len(e.msg):
        e.reply_help("rule")
        return 1
    if e.lower[e.cnt:e.cnt + 3] == "set":
        e.cnt += 3
        while e.peek().isspace() or e.peek() == ":":
            e.cnt += 1
        rule = e.msg[e.cnt:]
        game = game_of(e)
        if not rule:
            if game is not None:
                game.reset("rule")
            else:
                store.rm_user_conf(e.uid, "默认规则")
            e.reply_msg("strRuleReset")
        else:
            e.ctx["rule"] = rule
            if game is not None:
                game.set("rule", rule)
            else:
                store.set_user_conf(e.uid, "默认规则", rule)
            e.reply_msg("strRuleSet")
        return 1
    if e.lower[e.cnt] == "s":
        e.cnt += 1
        search = e.read_rest().upper()
        game = game_of(e)
        rule_name = ""
        if game is not None and game.has("rule"):
            rule_name = str(game.get("rule"))
        doc, err = await _lookup_rule(e, rule_name, search)
        if doc is not None:
            e.reply(doc, False)
            return 1
        default_rule = store.user_conf(e.uid, "默认规则")
        if default_rule and ":" not in search:
            doc, err = await _lookup_rule(e, default_rule, search)
            if doc is not None:
                e.reply(doc, False)
                return 1
        if ":" in search:
            rn, _, item = search.partition(":")
            if item:
                doc, err = await _lookup_rule(e, rn, item)
                if doc is not None:
                    e.reply(doc, False)
                    return 1
        msg = err or _sub(get_msg_text("strRuleNotFound"), item=search)
        e.reply(get_msg_text("strRuleErr") + msg)
        return 1
    e.reply_help("rule")
    return 1


async def _lookup_rule(e: DiceEvent, rule: str, name: str):
    if not name:
        return None, ""
    doc, err = await fetch_rule(e.self_id, rule, name)
    if doc is not None:
        return doc, ""
    if err == get_msg_text("strRequestNoResponse"):
        return None, _sub(get_msg_text("strRuleNotFound"), item=name)
    if err:
        return None, err
    return None, _sub(get_msg_text("strRuleNotFound"), item=name)


# --------------------------------------------------------------------- .log
async def run_log(e: DiceEvent, bot) -> int:
    from .cloud import upload_log

    para = e.read_para()
    if not para:
        e.reply_help("log")
        return 1
    game = sessions.get(e.gid, e.uid)
    e.game = game
    if para == "new":
        return _log_new(e, game, e.read_file_name())
    if para == "on":
        if not game.log.get("start"):
            return _log_new(e, game, e.read_file_name())
        name_log = e.read_file_name()
        if name_log and name_log != game.log.get("name"):
            game.log["start"] = int(time.time())
            game.log["name"] = name_log
            game.log["file"] = "%s_%s.txt" % (game.name, name_log)
        elif game.is_logging():
            e.reply_msg("strLogOnAlready")
            return 1
        e.ctx["log_name"] = game.log.get("name")
        game.log["logging"] = True
        game.save()
        e.reply_msg("strLogOn")
        return 1
    if para == "off":
        if not game.log.get("start"):
            e.reply_msg("strLogNullErr")
            return 1
        if not game.is_logging():
            e.reply_msg("strLogOffAlready")
            return 1
        game.log["logging"] = False
        game.save()
        e.reply_msg("strLogOff")
        return 1
    if para == "end":
        if not game.log.get("start"):
            e.reply_msg("strLogNullErr")
            return 1
        game.log["logging"] = False
        game.log["start"] = 0
        path = game.log_path()
        if not path.exists():
            e.reply_msg("strLogEndEmpty")
            game.save()
            return 1
        file_name = str(game.log.get("file") or path.name)
        e.ctx["log_file"] = file_name
        e.reply_msg("strLogEnd")
        game.save()

        # 1) 先把文件直接发给群 / 发起人
        await _send_log_file(bot, e, path, file_name)

        # 2) 上传到 dicelogger，再由 logpainter 渲染
        content = path.read_bytes()
        ret = ""
        for attempt in range(3):
            url, ret = await upload_log(file_name, content)
            if url:
                e.ctx["log_url"] = url
                e.reply_msg("strLogUpSuccess")
                break
            if attempt >= 2:
                e.ctx["ret"] = ret
                e.reply_msg("strLogUpFailureEnd")
                break
            e.ctx["retry"] = attempt + 1
            e.ctx["ret"] = ret
            e.reply_msg("strLogUpFailure")
            await asyncio.sleep(3)
        return 1
    e.reply_help("log")
    return 1


def _log_new(e: DiceEvent, game, name_log: str) -> int:
    if not name_log:
        name_log = str(int(time.time()))
    e.ctx["log_name"] = name_log
    game.log = {"logging": True, "start": int(time.time()),
                "name": name_log, "file": "%s_%s.txt" % (game.name, name_log)}
    game.save()
    e.reply_msg("strLogNew")
    return 1


async def _send_log_file(bot, e: DiceEvent, path, file_name: str):
    """对应 DD::uploadGroupFile / DD::sendFriendFile。"""
    try:
        if e.gid:
            await bot.call_api("upload_group_file", group_id=e.gid,
                               file=str(path), name=file_name)
        elif e.uid:
            await bot.call_api("upload_private_file", user_id=e.uid,
                               file=str(path), name=file_name)
    except Exception:
        pass


# ------------------------------------------------------------ .nn / .nnn
def _nnn(e: DiceEvent) -> int:
    e.skip_space()
    type_name = e.read_para()
    name = decks.get("随机姓名_" + type_name) if type_name else None
    deck_name = "随机姓名_" + type_name if name else "随机姓名"
    deck = decks.get(deck_name)
    if not deck:
        e.reply_msg("strDeckNotFound")
        return 1
    e.ctx["old_nick"] = e.resolve("nick")
    new_nick = deck_draw_card(list(deck), True).strip()
    e.ctx["new_nick"] = new_nick
    store.set_nick(e.uid, e.gid, new_nick)
    e.reply_msg("strNameSet")
    e.nick = new_nick
    e.ctx["nick"] = new_nick
    return 1


# ------------------------------------------------------------ .deck / .draw
async def _deck(e: DiceEvent, bot) -> int:
    if not e.is_master and settings.disabled_deck:
        e.reply_msg("strDisabledDeckGlobal")
        return 1
    if e.cnt >= len(e.msg):
        e.reply_help("deck")
        return 1
    para = e.read_para()
    if para == "show":
        game = game_of(e)
        if game is not None:
            _deck_show(e, game)
        else:
            e.reply_msg("strDeckListEmpty")
        return 1
    if not e.can_host:
        e.reply_msg("strWhiteQQDenied")
        return 1
    if para == "new":
        game = game_of(e) or sessions.get(e.gid, e.uid)
        e.game = game
        _deck_new(e, game)
        return 1
    if para == "set":
        game = game_of(e) or sessions.get(e.gid, e.uid)
        e.game = game
        await _deck_set(e, game, bot)
        return 1
    game = game_of(e)
    if game is None:
        e.reply_msg("strDeckListEmpty")
        return 1
    if para == "reset":
        _deck_reset(e, game)
    elif para == "del":
        _deck_del(e, game)
    elif para == "clr":
        game.decks.clear()
        game.touch()
        e.reply_msg("strDeckListClr")
    return 1


def _deck_show(e: DiceEvent, game):
    name = e.read_attr_name()
    e.ctx["deck_name"] = name
    if not name:
        if not game.decks:
            e.reply_msg("strDeckListEmpty")
            return
        res = ResList()
        for key, deck in game.decks.items():
            res.add("%s[%d/%d]" % (key, deck.siz_res, len(deck.idxs)))
        e.ctx["res"] = res.show()
        e.reply_msg("strDeckListShow")
    elif name in game.decks:
        deck = game.decks[name]
        res = ResList()
        for idx in range(deck.siz_res):
            res.add(deck.meta[deck.idxs[idx]])
        e.ctx["deck_rest"] = res.dot(" | ").show()
        e.reply_msg("strDeckRestShow")
    else:
        e.reply_msg("strDeckNotFound")


def _deck_new(e: DiceEvent, game):
    pos = e.msg.find("=", e.cnt)
    if pos < 0:
        key = "new"
    else:
        key = e.read_attr_name()
        e.cnt = pos + 1
    e.ctx["deck_name"] = key
    if len(game.decks) > 9 and key not in game.decks:
        e.reply_msg("strDeckListFull")
        return
    items = e.read_items()
    if not items:
        e.reply_msg("strDeckNewEmpty")
        return
    if len(items) > 256:
        e.reply_msg("strDeckOversize")
        return
    deck = DeckInfo(items)
    if len(deck.idxs) > 1024:
        e.reply_msg("strDeckOversize")
        return
    game.decks[key] = deck
    game.touch()
    e.reply_msg("strDeckNew")


async def _deck_set(e: DiceEvent, game, bot):
    key = e.read_attr_name()
    e.ctx["deck_name"] = key
    pos = e.msg.find("=", e.cnt)
    if pos < 0:
        cite = key
        e.ctx["deck_cited"] = cite
    else:
        e.cnt += 1
        cite = e.read_attr_name()
        e.ctx["deck_cited"] = cite
    if not key:
        e.reply_msg("strDeckNameEmpty")
        return
    if len(game.decks) > 9 and key not in game.decks:
        e.reply_msg("strDeckListFull")
        return
    deck_set = []
    if cite in ("群成员", "member") and e.gid:
        cite = "群成员"
        e.ctx["deck_cited"] = cite
        try:
            members = await bot.call_api("get_group_member_list", group_id=e.gid)
        except Exception:
            members = None
        if not members:
            e.reply("群成员列表获取失败×")
            return
        for m in members:
            deck_set.append(print_user(m.get("user_id"), e.self_id, e.self_name))
        game.decks[key] = DeckInfo(deck_set)
    elif cite == "range":
        left = e.read_digit()
        right = e.read_digit()
        step = e.read_digit()
        if not left:
            e.reply_msg("strRangeEmpty")
            return
        if len(left) > 16 or len(right) > 16:
            e.reply_msg("strOutRange")
            return
        begin = int(left)
        end = int(right) if right else 0
        stride = int(step) if step else 1
        if begin == end:
            e.reply_msg("strRangeEmpty")
            return
        if begin > end:
            begin, end = end, begin
        if (end - begin) > 1000 * stride:
            e.reply_msg("strOutRange")
            return
        card = begin
        while card <= end:
            deck_set.append(str(card))
            card += stride
        game.decks[key] = DeckInfo(deck_set)
        e.ctx["deck_cited"] = "%s%d~%s" % (cite, begin, deck_set[-1])
    elif cite not in decks or cite.startswith("_"):
        e.reply_msg("strDeckCiteNotFound")
        return
    else:
        game.decks[key] = DeckInfo(list(decks.get(cite) or []))
    game.touch()
    if key == cite:
        e.reply_msg("strDeckSet")
    else:
        e.reply_msg("strDeckSetRename")


def _deck_reset(e: DiceEvent, game):
    key = e.read_attr_name()
    if not key:
        key = e.read_digit()
    e.ctx["deck_name"] = key
    if not key:
        e.reply_msg("strDeckNameEmpty")
    elif key not in game.decks:
        e.reply_msg("strDeckNotFound")
    else:
        game.decks[key].reset()
        game.touch()
        e.reply_msg("strDeckRestReset")


def _deck_del(e: DiceEvent, game):
    key = e.read_attr_name()
    if not key:
        key = e.read_digit()
    e.ctx["deck_name"] = key
    if not key:
        e.reply_msg("strDeckNameEmpty")
    elif key not in game.decks:
        e.reply_msg("strDeckNotFound")
    else:
        game.decks.pop(key, None)
        game.touch()
        e.reply_msg("strDeckDelete")


def _draw(e: DiceEvent) -> int:
    if not e.is_master and settings.disabled_draw:
        e.reply_msg("strDisabledDrawGlobal")
        return 1
    e.ctx["option"] = "禁用draw"
    if not e.is_private and store.group_opt(e.gid, "禁用draw"):
        e.reply_msg("strGroupSetOnAlready")
        return 1
    if e.peek() == "h" and e.peek(1) in (" ", "\t", "\u3000"):
        e.hidden = True
        e.cnt += 1
    e.skip_space()
    key = e.read_attr_name()
    while key.startswith("_"):
        e.hidden = True
        key = key[1:]
    e.ctx["deck_name"] = key
    if not key:
        e.reply_help("draw")
        return 1
    game = game_of(e)
    if game is not None and key in game.decks:
        return _draw_session(e, game, key)
    if key not in decks:
        e.ctx["deck_name"] = key
        e.reply_msg("strDeckNotFound")
        return 1
    temp = list(decks.get(key) or [])
    count = 1
    st, count = e.read_num(count)
    if st == 0 and count == 0:
        e.reply_msg("strNumCannotBeZero")
        return 1
    if st == -2:
        e.reply_msg("strParaIllegal")
        return 1
    res = ResList()
    left = count
    while left > 0:
        res.add(deck_draw_card(temp, False))
        left -= 1
        if not temp:
            break
    e.ctx["res"] = res.dot("|").show()
    e.ctx["cnt"] = str(len(res))
    if e.hidden:
        e.reply_msg("strDrawHidden")
        e.reply_hidden(get_msg_text("strDrawCard"))
    else:
        e.reply_msg("strDrawCard")
    if left > 0:
        e.reply_msg("strDeckEmpty")
    return 1


def _draw_session(e: DiceEvent, game, key: str) -> int:
    deck = game.decks[key]
    count = 1
    st, count = e.read_num(count)
    if st == 0 and count == 0:
        e.reply_msg("strNumCannotBeZero")
        return 1
    if st == -2:
        e.reply_msg("strParaIllegal")
        return 1
    res = ResList()
    while deck.siz_res and count > 0:
        res.add(deck.draw())
        count -= 1
        if not deck.siz_res:
            break
    game.touch()
    if len(res):
        e.ctx["res"] = res.dot("|").show()
        e.ctx["cnt"] = str(len(res))
        if e.hidden:
            e.reply_msg("strDrawHidden")
            e.reply_hidden(get_msg_text("strDrawCard"))
        else:
            e.reply_msg("strDrawCard")
    if not deck.siz_res:
        e.ctx["deck_name"] = key
        e.reply_msg("strDeckRestEmpty")
    return 1


def _name(e: DiceEvent) -> int:
    e.skip_space()
    type_name = e.read_para()
    num_str = e.read_digit()
    if len(num_str) > 1 and num_str != "10":
        e.reply_msg("strNameNumTooBig")
        return 1
    count = int(num_str) if num_str else 1
    if count == 0:
        e.reply_msg("strNameNumCannotBeZero")
        return 1
    deck_name = "随机姓名_" + type_name if type_name and ("随机姓名_" + type_name) in decks else "随机姓名"
    deck = decks.get(deck_name)
    if not deck:
        e.reply_msg("strDeckNotFound")
        return 1
    res = ShowList()
    for _ in range(count):
        res.add(deck_draw_card(list(deck), True))
    e.ctx["res"] = res.show("、")
    e.reply_msg("strNameGenerator")
    return 1


def _nn(e: DiceEvent) -> int:
    e.skip_space()
    if e.cnt >= len(e.msg):
        e.reply_help("nn")
        return 1
    e.ctx["old_nick"] = e.resolve("nick")
    new_nick = strip(filter_cqcode(e.msg[e.cnt:]))
    e.ctx["new_nick"] = new_nick
    if len(new_nick) > 50:
        e.reply_msg("strNameTooLongErr")
        return 1
    if not new_nick:
        e.reply_help("nn")
    elif new_nick == "del":
        if store.rm_nick(e.uid, e.gid):
            e.reply_msg("strNameDel")
        else:
            e.reply_msg("strNameDelEmpty")
    elif new_nick == "clr":
        store.clr_nick(e.uid)
        e.reply_msg("strNameClr")
    else:
        store.set_nick(e.uid, e.gid, new_nick)
        e.reply_msg("strNameSet")
        e.nick = new_nick
        e.ctx["nick"] = new_nick
    return 1


# ---------------------------------------------------------------------- .set
def _set(e: DiceEvent) -> int:
    e.skip_space()
    default = e.read_digit()
    default = default.lstrip("0")
    if not default:
        default = "100"
    e.ctx["default"] = default
    if len(default) > 4:
        e.reply_msg("strSetTooBig")
        return 1
    value = int(default)
    pl = player_of(e)
    if str(e.uid) in store.players:
        pl.get_card("", e.gid).data["__DefaultDice"] = value
        pl.get_card("", e.gid).touch()
        store.mark("players")
        e.reply_msg("strSetDefaultDice")
        return 1
    if value == 100:
        store.rm_user_conf(e.uid, "默认骰")
    else:
        store.set_user_conf(e.uid, "默认骰", value)
    e.reply_msg("strSetDefaultDice")
    return 1


def _default_dice(e: DiceEvent, pc: Optional[CharaCard]) -> int:
    if pc is not None and pc.has("__DefaultDice"):
        return pc.get_int("__DefaultDice", 100)
    return int(store.user_conf(e.uid, "默认骰", 100) or 100)


# ----------------------------------------------------------------------- .ak
def _ak(e: DiceEvent) -> int:
    e.skip_space()
    if e.cnt >= len(e.msg):
        e.reply_help("ak")
        return 1
    s = sessions.get(e.gid, e.uid)
    e.game = s
    sign = e.peek()
    action = e.read_para()
    deck = s.dict.setdefault("__Ank", [])
    if isinstance(deck, dict):
        deck = []
        s.dict["__Ank"] = deck

    if sign == "#" or action == "new":
        if sign == "#":
            e.cnt += 1
        deck.clear()
        plus = e.msg.find("+", e.cnt)
        title = e.msg[e.cnt:] if plus < 0 else e.msg[e.cnt:plus]
        if title:
            e.cnt += len(title)
            s.dict["AkFork"] = title
            e.ctx["fork"] = title
        if e.cnt >= len(e.msg):
            e.reply_msg("strAkForkNew")
            return 1
        sign = e.peek()

    if sign == "+" or action == "add":
        if sign == "+":
            e.cnt += 1
        items = e.read_items()
        if not items:
            e.reply_msg("strAkAddEmpty")
            return 1
        deck.extend(items)
        e.ctx["fork"] = s.dict.get("AkFork", "")
        e.ctx["li"] = _ak_list(deck)
        e.reply_msg("strAkAdd")
        s.touch()
        return 1
    if sign == "-" or action == "del":
        if sign == "-":
            e.cnt += 1
        st, no = e.read_num(0)
        if st != 0 or no <= 0 or no > len(deck):
            e.reply_msg("strAkNumErr")
            return 1
        deck.pop(no - 1)
        e.ctx["fork"] = s.dict.get("AkFork", "")
        e.ctx["li"] = _ak_list(deck)
        e.reply_msg("strAkDel")
        s.touch()
        return 1
    if sign == "=" or action == "get":
        if deck:
            e.ctx["fork"] = s.dict.get("AkFork", "")
            s.dict.pop("AkFork", None)
            idx = random.randint(0, len(deck) - 1)
            e.ctx["get"] = "%d. %s" % (idx + 1, deck[idx])
            e.ctx["li"] = _ak_list(deck)
            deck.clear()
            e.reply_msg("strAkGet")
        else:
            e.reply_msg("strAkOptEmptyErr")
        s.touch()
        return 1
    if action == "show":
        e.ctx["fork"] = s.dict.get("AkFork", "")
        e.ctx["li"] = _ak_list(deck)
        e.reply_msg("strAkShow")
        return 1
    if action == "clr":
        deck.clear()
        e.ctx["fork"] = s.dict.get("AkFork", "")
        s.dict.pop("AkFork", None)
        e.reply_msg("strAkClr")
        s.touch()
        return 1
    e.reply_help("ak")
    return 1


def _ak_list(deck: List[str]) -> str:
    out = ResList()
    out.order()
    out.linebreak()
    for item in deck:
        out.add(item)
    return out.show()


# ----------------------------------------------------------------------- .en
def _en(e: DiceEvent) -> int:
    e.skip_space()
    if e.cnt >= len(e.msg):
        e.reply_help("en")
        return 1
    attr = e.read_attr_name()
    e.ctx["attr"] = attr
    current = e.read_digit(False)
    pl = player_of(e) if str(e.uid) in store.players else None
    pc = pl.get_card("", e.gid) if pl else None
    if not current:
        if pc is not None and attr and pc.has(attr):
            value = pc.get_int(attr)
        else:
            e.ctx["attr"] = attr
            e.reply_msg("strEnValEmpty")
            return 1
    else:
        if len(current) > 3:
            e.reply_msg("strEnValInvalid")
            return 1
        value = int(current)
    e.skip_space()
    change_exp = ""
    fail_exp = ""
    suc_exp = "1D10"
    if e.peek_lower() in ("+", "-"):
        end = e.msg.find(" ", e.cnt)
        if end < 0:
            end = len(e.msg)
        change_exp = e.lower[e.cnt:end]
        if "/" in change_exp:
            fail_exp, _, suc_exp = change_exp.partition("/")
        else:
            suc_exp = change_exp
    if not attr:
        attr = get_msg_text("strEnDefaultName")
    e.ctx["attr"] = attr
    game = game_of(e)
    roll_res = game.roll(100) if (game is not None and game.is_part(e.uid)) else random.randint(1, 100)
    if pc is not None:
        pc.cnt_roll_stat(roll_res, 100)
        store.mark("players")
    res = "1D100=%d/%d " % (roll_res, value)
    if roll_res <= value and roll_res <= 95:
        if not fail_exp:
            res += get_msg_text("strFailure")
            e.ctx["strEnRoll"] = res
            e.reply_msg("strEnRollNotChange")
            return 1
        res += get_msg_text("strFailure")
        rd = RD(fail_exp)
        if rd.Roll():
            e.reply_msg("strValueErr")
            return 1
        value += rd.intTotal
        e.ctx["strEnRoll"] = res
        e.ctx["change"] = rd.FormCompleteString()
        e.ctx["final"] = value
        e.reply_msg("strEnRollFailure")
    else:
        res += get_msg_text("strSuccess")
        rd = RD(suc_exp)
        if rd.Roll():
            e.reply_msg("strValueErr")
            return 1
        value += rd.intTotal
        e.ctx["strEnRoll"] = res
        e.ctx["change"] = rd.FormCompleteString()
        e.ctx["final"] = value
        e.reply_msg("strEnRollSuccess")
    if pc is not None:
        pc.set(attr, value)
        store.mark("players")
    return 1


# ------------------------------------------------------------------ .ti / .li
def _ti(e: DiceEvent) -> int:
    pc_name = idx_pc(e.ctx) or "调查员"
    vals = temp_insane()
    vals["pc"] = pc_name
    e.ctx.update(vals)
    e.reply_msg("strTempInsane")
    return 1


def _li(e: DiceEvent) -> int:
    pc_name = idx_pc(e.ctx) or "调查员"
    vals = long_insane()
    vals["pc"] = pc_name
    vals["nick"] = e.nick or pc_name
    e.ctx.update(vals)
    e.reply_msg("strLongInsane")
    return 1


# ---------------------------------------------------------------------- .me
async def _me(e: DiceEvent, bot) -> int:
    if settings.disabled_me and not e.is_master:
        e.reply_msg("strDisabledMeGlobal")
        return 1
    e.skip_space()
    if e.is_private:
        gid_str = e.read_digit()
        if not gid_str:
            e.reply_msg("strGroupIDEmpty")
            return 1
        gid = int(gid_str)
        if (store.group_opt(gid, "停用指令") or store.group_opt(gid, "禁用me")) and not e.is_master:
            e.reply_msg("strMEDisabledErr")
            return 1
        e.skip_space()
        action = strip(e.read_rest())
        if not action:
            e.reply_msg("strActionEmpty")
            return 1
        text = (get_name(e.uid, gid, e.self_id, e.self_name) if e.is_master else "") + action
        try:
            await bot.send_group_msg(group_id=gid, message=text)
        except Exception:
            pass
        e.reply_msg("strSendSuccess")
        return 1
    action = e.lower[e.cnt:]
    if not e.can_host and action in ("on", "off"):
        e.reply_msg("strPermissionDeniedErr")
        return 1
    if action == "off":
        if store.group_opt(e.gid, "禁用me"):
            e.reply_msg("strMeOffAlready")
        else:
            store.set_group_opt(e.gid, "禁用me")
            e.reply_msg("strMeOff")
        return 1
    if action == "on":
        if store.group_opt(e.gid, "禁用me"):
            store.reset_group_opt(e.gid, "禁用me")
            e.reply_msg("strMeOn")
        else:
            e.reply_msg("strMeOnAlready")
        return 1
    if store.group_opt(e.gid, "禁用me"):
        e.reply_msg("strMEDisabledErr")
        return 1
    action = strip(e.read_rest())
    if not action:
        e.reply_msg("strActionEmpty")
        return 1
    if e.is_master:
        e.reply(action, False)
    else:
        e.reply(idx_pc(e.ctx) + action, False)
    return 1


# ---------------------------------------------------------------------- .ob
def _ob(e: DiceEvent) -> int:
    if e.is_private:
        e.reply_help("ob")
        return 1
    e.skip_space()
    pos = e.msg.find(" ", e.cnt)
    option = e.lower[e.cnt:] if pos < 0 else e.lower[e.cnt:pos]
    if not option:
        e.reply_help("ob")
        return 1
    if not e.can_host and option in ("on", "off"):
        e.reply_msg("strPermissionDeniedErr")
        return 1
    game = game_of(e)
    if option == "join":
        if game is None or not game.is_("no_ob"):
            s = sessions.get(e.gid, e.uid)
            e.game = s
            if e.uid in s.obs:
                e.reply_msg("strObEnterAlready")
            else:
                s.obs.append(e.uid)
                s.touch()
                e.reply_msg("strObEnter")
        else:
            e.reply_msg("strObOffAlready")
        return 1
    if game is None:
        e.reply_msg("strGameVoidHere")
        return 1
    if option == "off":
        if not game.is_("no_ob"):
            game.set("no_ob", True)
            e.reply_msg("strObOff")
        else:
            e.reply_msg("strObOffAlready")
        return 1
    if option == "on":
        if game.is_("no_ob"):
            game.reset("no_ob")
            e.reply_msg("strObOn")
        else:
            e.reply_msg("strObOnAlready")
        return 1
    if option == "list":
        if not game.obs:
            e.reply_msg("strObListEmpty")
        else:
            res = ResList()
            for uid in game.obs:
                res.add(print_user(uid, e.self_id, e.self_name))
            e.reply(get_msg_text("strObList") + res.linebreak().show())
        return 1
    if option == "clr":
        if e.can_host or game.is_gm(e.uid):
            if not game.obs:
                e.reply_msg("strObListEmpty")
            else:
                game.obs.clear()
                game.touch()
                e.reply_msg("strObListClr")
        else:
            e.reply_msg("strGameNotMaster")
        return 1
    if option == "exit":
        if game.del_ob(e.uid):
            e.reply_msg("strObExit")
        else:
            e.reply_msg("strObExitAlready")
        return 1
    e.reply_help("ob")
    return 1


# ---------------------------------------------------------------------- .pc
def _pc(e: DiceEvent) -> int:
    option = e.read_para()
    if not option:
        e.reply_help("pc")
        return 1
    pl = player_of(e)
    if option == "clr":
        if str(e.uid) in store.players:
            store.drop_player(e.uid)
            e.reply_msg("strPcClr")
        else:
            e.reply_msg("strPcNotExistErr")
        return 1
    resno = 0
    if option == "tag":
        tag = e.read_rest()
        resno = pl.change_card(tag, e.gid)
        if resno == 1:
            store.save_player(e.uid)
            e.reply_msg("strPcCardReset")
            return 1
        if resno == -5:
            e.ctx["char"] = tag
        elif resno == 0:
            store.save_player(e.uid)
            e.reply_msg("strPcCardSet")
    elif option == "show":
        name = e.read_rest()
        pc = pl.get_card(name, e.gid)
        e.ctx["char"] = pc.name
        e.ctx["type"] = pc.data.get("__Type", "")
        e.ctx["show"] = pc.show_all(True)
        e.reply_msg("strPcCardShow")
        return 1
    elif option == "new":
        text = strip(filter_cqcode(e.read_rest()))
        resno = pl.new_card(text, e.gid)
        if resno == 0:
            store.save_player(e.uid)
            pc = pl.get_card("", e.gid)
            e.ctx["type"] = pc.data.get("__Type", "")
            e.ctx["show"] = pc.show_all(True)
            e.ctx["char"] = pc.name
            if not e.ctx["show"]:
                e.reply_msg("strPcNewEmptyCard")
            else:
                e.reply_msg("strPcNewCardShow")
        else:
            e.ctx["char"] = text
    elif option == "build":
        text = strip(filter_cqcode(e.read_rest()))
        resno = pl.build_card(text, False, e.gid)
        if resno == 0:
            store.save_player(e.uid)
            pc = pl.get_card("", e.gid)
            e.ctx["show"] = pc.show_all(True)
            e.ctx["char"] = text or pc.name
            e.reply_msg("strPcCardBuild")
        else:
            e.ctx["char"] = text
    elif option == "list":
        e.ctx["show"] = pl.list_card()
        e.reply_msg("strPcCardList")
        return 1
    elif option == "nn":
        new_name = strip(filter_cqcode(e.read_rest()))
        e.ctx["new_name"] = new_name
        pc = pl.get_card("", e.gid)
        e.ctx["old_name"] = pc.name
        resno = pl.rename_card(pc, new_name)
        if resno == 0:
            store.save_player(e.uid)
            e.reply_msg("strPcCardRename")
        else:
            e.ctx["char"] = new_name
    elif option == "del":
        name = strip(e.read_rest())
        e.ctx["char"] = name
        resno = pl.remove_card(name)
        if resno == 0:
            store.save_player(e.uid)
            e.reply_msg("strPcCardDel")
    elif option == "redo":
        text = strip(filter_cqcode(e.read_rest()))
        pl.build_card(text, True, e.gid)
        store.save_player(e.uid)
        pc = pl.get_card("", e.gid)
        e.ctx["char"] = pc.name
        e.ctx["show"] = pc.show_all(True)
        e.reply_msg("strPcCardRedo")
        return 1
    elif option == "grp":
        e.ctx["show"] = pl.list_map()
        e.reply_msg("strPcGroupList")
        return 1
    elif option == "cpy":
        text = strip(filter_cqcode(e.read_rest()))
        name1, _, name2 = text.partition("=")
        name1 = name1.strip()
        name2 = name2.strip() or pl.get_card("", e.gid).name
        e.ctx["char1"] = name1
        e.ctx["char2"] = name2
        resno = pl.copy_card(name1, name2, e.gid)
        if resno == 0:
            store.save_player(e.uid)
            e.reply_msg("strPcCardCpy")
        elif resno == -5:
            e.ctx["char"] = name2
    elif option == "stat":
        return _pc_stat(e, pl)
    elif option == "type":
        new_type = strip(e.read_rest())
        e.ctx["new_type"] = new_type
        if not new_type:
            e.ctx["attr"] = "模板类"
            e.ctx["val"] = pl.get_card("", e.gid).data.get("__Type", "")
            e.reply_msg("strProp")
        else:
            pl.get_card("", e.gid).set_type(new_type)
            store.save_player(e.uid)
            e.reply_msg("strPcTempChange")
        return 1
    elif option == "temp":
        e.reply(_temp_show(pl.get_card("", e.gid)), False)
        return 1
    elif option == "tojson":
        name = e.read_rest()
        pc = pl.get_card(name, e.gid)
        e.ctx["char"] = pc.name
        e.ctx["type"] = pc.data.get("__Type", "")
        e.ctx["show"] = str(pc.data)
        e.reply_msg("strPcCardShow")
        return 1
    else:
        e.reply_help("pc")
    if resno:
        key = PLAYER_ERRORS.get(resno, "strUnknownErr")
        e.reply_msg(key)
    return 1


def _temp_show(pc: CharaCard) -> str:
    names = "/".join(k for k in pc.templet().attr_keys if not k.startswith("&"))
    return "pc模板:%s预设属性:%s" % (pc.data.get("__Type", ""), names)


def _pc_stat(e: DiceEvent, pl) -> int:
    pc = pl.get_card("", e.gid)
    face = pc.get_int("__DefaultDice") if pc.has("__DefaultDice") else int(
        store.user_conf(e.uid, "默认骰", 100) or 100)
    res = ResList()
    empty = True
    key_cnt = "__StatD%dCnt" % face
    if face <= 100 and pc.has(key_cnt):
        cnt = pc.get_int(key_cnt)
        if cnt > 0:
            est = DiceEst(face, cnt, pc.get_int("__StatD%dSum" % face),
                          pc.get_int("__StatD%dSqrSum" % face))
            if est.est_mean > 0:
                empty = False
                res.add("D%d统计次数: %d" % (face, cnt))
                res.add("均值: %s [期望 %s]" % (_num(est.est_mean, 2, True),
                                                _num(est.exp_mean)))
                if est.p_norm_dist:
                    if est.p_norm_dist < 0.5:
                        res.add("低于%s%%的用户" % _num(100 - est.p_norm_dist * 100, 2))
                    else:
                        res.add("高于%s%%的用户" % _num(est.p_norm_dist * 100, 2))
                if est.est_std > 0:
                    res.add("标准差: %s [期望 %s]" % (_num(est.est_std, 2),
                                                      _num(est.exp_std)))
    if pc.has("__StatRcCnt"):
        cnt = pc.get_int("__StatRcCnt")
        if cnt > 0:
            empty = False
            suc = pc.get_int("__StatRcSumSuc")
            rate = pc.get_int("__StatRcSumRate")
            res.add("检定成功率: %d/%d=%s%% [期望%s%%]" % (
                suc, cnt, _num(suc / cnt * 100), _num(rate / cnt)))
            c5 = pc.get_int("__StatRcCnt5")
            c96 = pc.get_int("__StatRcCnt96")
            res.add("5- | 96+ 出现率: %s | %s" % (
                ("%s%%(%d)" % (_num(c5 / cnt * 100), c5)) if c5 else "0%",
                ("%s%%(%d)" % (_num(c96 / cnt * 100), c96)) if c96 else "0%"))
            if pc.has("__StatRcCnt1") or pc.has("__StatRcCnt100"):
                res.add("1 | 100 出现数: %d | %d" % (
                    pc.get_int("__StatRcCnt1"), pc.get_int("__StatRcCnt100")))
    if empty:
        e.reply_msg("strPcStatEmpty")
    else:
        e.ctx["stat"] = res.show()
        e.reply_msg("strPcStatShow")
    return 1


class DiceEst:
    """对应 DiceStatic.hpp::DiceEst（含原实现里用 expMean 计算方差的写法）。"""

    def __init__(self, face: int, cnt: int, total: int, sqr: int):
        self.exp_mean = 0.0
        self.est_mean = 0.0
        self.exp_std = 0.0
        self.est_std = 0.0
        self.p_norm_dist = 0.0
        if face <= 1:
            return
        self.exp_mean = (face + 1) / 2.0
        acc = 0.0
        for i in range(1, face + 1):
            acc += (i - self.exp_mean) ** 2
        self.exp_std = math.sqrt(acc / face)
        self.est_mean = total / cnt if cnt else 0
        if cnt > 1:
            self.est_std = math.sqrt((sqr - self.exp_mean ** 2 * cnt) / cnt)
            z = (self.est_mean - self.exp_mean) * math.sqrt(cnt) / self.exp_std
            self.p_norm_dist = 0.5 + 0.5 * math.erf(z / math.sqrt(2))


def _num(val, scale: int = 2, align: bool = False) -> str:
    """对应 StrExtern.hpp 的 toString(double, scale, align)。"""
    f = float(val)
    if f == int(f):
        return str(int(f))
    text = "%.10f" % f
    dot = text.find(".") + scale + 1
    if align:
        return text[:dot]
    last = len(text.rstrip("0"))
    if last > dot:
        last = dot
    if last and text[last - 1] == ".":
        last -= 1
    return text[:last]


# ---------------------------------------------------------------------- .rc
def _rc(e: DiceEvent) -> int:
    if e.cnt >= len(e.msg):
        e.reply_help("rc")
        return 1
    game = game_of(e)
    if e.is_private:
        rule = store.user_conf(e.uid, "rc房规", settings.default_coc_rule) or 0
    elif game is not None and game.has("rr_rc"):
        rule = int(game.get("rr_rc"))
    else:
        rule = store.group_rule(e.gid)
        if rule is None:
            rule = settings.default_coc_rule
    turn = 1
    if e.peek() == "h" and e.peek(1) in (" ", "\t", "\u3000"):
        e.hidden = True
        e.cnt += 1
    else:
        e.skip_space()
        if e.peek() == "_":
            e.hidden = True
            e.cnt += 1
    e.skip_space()
    hash_pos = e.msg.find("#")
    if hash_pos >= 0:
        turn_str = e.msg[e.cnt:hash_pos]
        if not turn_str:
            e.cnt += 1
        elif (len(turn_str) == 1 and turn_str.isdigit()) or turn_str == "10":
            e.cnt += len(turn_str) + 1
            turn = int(turn_str)

    main_dice = "D100"
    difficulty = ""
    int_difficulty = 1
    multi_success = 1
    skill_modify = 0
    skill_text = ""
    skill_multiple = 1
    skill_divisor = 1
    automatic = False
    has_card = str(e.uid) in store.players
    pl = store.player(e.uid) if has_card else None
    pc = pl.get_card("", e.gid) if has_card else None
    is_static = has_card
    is_roulette = game is not None and game.is_part(e.uid) and 100 in game.roulette

    if e.peek_lower() in ("p", "b") and e.peek_lower(-1) != " ":
        is_static = False
        main_dice = e.peek_lower()
        e.cnt += 1
        main_dice += e.read_digit(False)
    e.skip_space()
    if e.peek() == "_":
        e.hidden = True
        e.cnt += 1
    if e.cnt >= len(e.msg):
        e.reply_msg("strUnknownPropErr")
        return 1
    attr = e.read_attr_name()
    e.ctx["attr"] = attr
    if pc is not None:
        if e.peek() == ":":
            if pl.count(attr):
                while e.peek() == ":":
                    e.cnt += 1
                pc = pl[attr]
                e.ctx["pc"] = pc.name
                attr = e.read_attr_name()
            else:
                e.ctx["char"] = attr
                e.reply_msg("strPcNameNotExist")
                return 1
        elif "的" in attr:
            pos = attr.find("的")
            owner = attr[:pos]
            if pl.count(owner):
                pc = pl[owner]
                e.ctx["pc"] = pc.name
                attr = attr[pos + 1:]
    if attr.startswith("自动成功"):
        difficulty = attr[:4]
        attr = attr[4:]
        automatic = True
    for adj in ("困难", "极难", "极限", "简单"):
        if attr.startswith(adj):
            difficulty += adj
            if adj == "困难":
                int_difficulty = 2
            elif adj == "简单":
                multi_success = 2
            else:
                int_difficulty = 5
            attr = attr[len(adj):]
            break
    if pc is not None:
        attr = pc.standard(attr)
    else:
        attr = skill_name(attr)
    e.ctx["attr"] = attr
    if e.peek_lower() == "*" and e.peek(1).isdigit():
        e.cnt += 1
        st, skill_multiple = e.read_num(skill_multiple)
        if st != 0:
            skill_multiple = 1
    while e.peek_lower() in ("+", "-") and e.peek(1).isdigit():
        # 原版这里没吃掉符号，read_num 见到前导 '-' 直接返回 -3，
        # 于是 .rc 敏捷-10（帮助文档里写明的用法）恒不生效。此处显式取符号。
        sign = -1 if e.peek_lower() == "-" else 1
        e.cnt += 1
        st, delta = e.read_num(skill_modify)
        if st == 0:
            skill_modify = sign * delta
            skill_text = ("+%d" % skill_modify) if skill_modify > 0 else str(skill_modify)
    if e.peek_lower() == "/" and e.peek(1).isdigit():
        e.cnt += 1
        st, skill_divisor = e.read_num(skill_divisor)
        if st != 0 or skill_divisor == 0:
            e.reply_msg("strValueErr")
            return 1
    while e.peek() in (" ", "\t") or (e.peek() and e.peek() in "=:"):
        e.cnt += 1
    skill_val = e.read_digit(False)
    e.ctx["reason"] = e.read_rest()

    if not skill_val:
        if pc is not None and pc.has(attr):
            value = pc.get_int(attr)
        else:
            if pc is None:
                attr = skill_name(attr)
                e.ctx["attr"] = attr
            default = skill_default(attr) if pc is None else None
            if default is None:
                e.reply_msg("strUnknownPropErr")
                return 1
            value = default
    elif len(skill_val) > 3:
        e.reply_msg("strPropErr")
        return 1
    else:
        value = int(skill_val)

    final = idiv(idiv(idiv((value * skill_multiple + skill_modify) * multi_success,
                           skill_divisor), int_difficulty), 1)
    if final < 0 or final > 1000:
        e.reply_msg("strSuccessRateErr")
        return 1

    rd = RD(main_dice)
    first = rd.Roll()
    if first == -3:
        e.reply_msg("strZeroDiceErr")
        return 1
    if first == -5:
        e.reply_msg("strDiceTooBigErr")
        return 1

    label = difficulty + attr
    if skill_multiple != 1:
        label += "×%d" % skill_multiple
    label += skill_text
    if skill_divisor != 1:
        label += "/%d" % skill_divisor
    e.ctx["attr"] = label

    reason = e.ctx.get("reason") or ""
    if reason:
        e.reply_msg("strRollSkillReason")
    else:
        e.reply_msg("strRollSkill")
    head = e.replies[-1] if e.replies else ""
    e.replies = e.replies[:-1]

    if turn == 1:
        if is_roulette:
            rd.Roll(game)
        else:
            rd.Roll()
        if is_static:
            pc.cnt_roll_stat(rd.intTotal, 100)
            pc.cnt_rc_stat(rd.intTotal, final)
            store.mark("players")
        ans = rd.FormCompleteString() + "/" + str(final) + " "
        ans += _success_text(roll_success_level(rd.intTotal, final, rule),
                             automatic, multi_success, single=True)
        e.reply(head + ans, False)
    else:
        out = ResList()
        out.dot("\n")
        for _ in range(turn):
            if is_roulette:
                rd.Roll(game)
            else:
                rd.Roll()
            if is_static:
                pc.cnt_roll_stat(rd.intTotal, 100)
                pc.cnt_rc_stat(rd.intTotal, final)
                store.mark("players")
            ans = rd.FormCompleteString() + "/" + str(final) + " "
            ans += _success_text(roll_success_level(rd.intTotal, final, rule),
                                 automatic, multi_success, single=False,
                                 difficulty=int_difficulty)
            out.add(ans)
        e.reply(head + out.show(), False)
    if e.hidden:
        e.push_hidden(e.replies[-1] if e.replies else "")
        e.replies = []
        e.reply_msg("strRollSkillHidden")
    return 1


def _success_text(level: int, automatic: bool, multi_success: int,
                  single: bool, difficulty: int = 1) -> str:
    if single:
        if level == SuccessLevel.Fumble:
            return get_msg_text("strRollFumble")
        if level == SuccessLevel.Failure:
            return get_msg_text("strRollRegularSuccess" if automatic else "strRollFailure")
        if level == SuccessLevel.Critical:
            return get_msg_text("strRollCriticalSuccess")
        if level == SuccessLevel.ExtremeSuccess:
            return get_msg_text("strRollExtremeSuccess")
        if level == SuccessLevel.HardSuccess and multi_success == 1:
            return get_msg_text("strRollHardSuccess")
        return get_msg_text("strRollRegularSuccess")
    if level == SuccessLevel.Fumble:
        return get_msg_text("strFumble")
    if level == SuccessLevel.Failure:
        return get_msg_text("strSuccess" if automatic else "strFailure")
    if level == SuccessLevel.Critical:
        return get_msg_text("strCriticalSuccess")
    if level == SuccessLevel.ExtremeSuccess and difficulty == 1:
        return get_msg_text("strExtremeSuccess")
    if level == SuccessLevel.HardSuccess and difficulty == 1:
        return get_msg_text("strHardSuccess")
    return get_msg_text("strSuccess")


# ---------------------------------------------------------------------- .ri
def _ri(e: DiceEvent) -> int:
    if e.is_private:
        e.reply_help("ri")
        return 1
    init = "D20"
    if e.peek_lower() in ("+", "-"):
        init += e.read_dice()
    elif e.is_roll_dice():
        got = e.read_xdy()
        init = got or "D20"
    name = strip(e.read_rest())
    rd = RD(init, 20)
    err = rd.Roll()
    if err:
        reply_roll_err(e, err, rd)
        return 1
    game = sessions.get(e.gid, e.uid)
    e.game = game
    pos = name.find("#")
    if pos < 0:
        if not name:
            name = idx_pc(e.ctx)
        e.ctx["char"] = name
        game.table_add("先攻", rd.intTotal, name)
        game.table_add("init_exp", rd.strDice, name)
        e.ctx["res"] = rd.FormCompleteString()
    else:
        turn_str = name[:pos]
        name = name[pos + 1:]
        e.ctx["char"] = name
        cnt = 1
        res = ShowList()
        if turn_str:
            turn_rd = RD(turn_str, 20)
            terr = turn_rd.Roll()
            if terr:
                reply_roll_err(e, terr, turn_rd)
                return 1
            if turn_rd.intTotal > 10:
                e.reply_msg("strRollTimeExceeded")
                return 1
            if turn_rd.intTotal <= 0:
                e.reply_msg("strRollTimeErr")
                return 1
            cnt = turn_rd.intTotal
        game.table_add("init_exp", rd.strDice, name)
        no = 0
        while True:
            no += 1
            res.add("%d. %s" % (no, rd.FormCompleteString()))
            game.table_add("先攻", rd.intTotal, "%s%d" % (name, no))
            if no >= cnt:
                break
            rd.Roll()
        e.ctx["res"] = "\n" + res.show("\n")
    e.reply_msg("strRollInit")
    return 1


# ---------------------------------------------------------------------- .sc
def _sc(e: DiceEvent) -> int:
    cost = e.read_until_space()
    e.skip_space()
    if not cost:
        e.reply_help("sc")
        return 1
    if "/" not in cost:
        e.reply_msg("strSanCostInvalid")
        return 1
    attr = "理智"
    _, san = e.read_num(0)
    pc = None
    if not san:
        if str(e.uid) in store.players:
            pc = store.player(e.uid).get_card("", e.gid)
            if pc.has(attr):
                san = pc.get_int(attr)
        if not san:
            e.reply_msg("strSanEmpty")
            return 1
    suc_exp, _, fail_exp = cost.partition("/")
    for part in (suc_exp, fail_exp):
        for ch in part:
            if not ch.isdigit() and ch not in "Dd+-":
                e.reply_msg("strSanCostInvalid")
                return 1
    if san <= 0:
        e.reply_msg("strSanInvalid")
        return 1
    game = game_of(e)
    roll = game.roll(100) if (game is not None and game.is_part(e.uid)) else random.randint(1, 100)
    if pc is not None:
        pc.cnt_roll_stat(roll, 100)
        pc.cnt_rc_stat(roll, san)
        store.mark("players")
    res_text = "1D100=%d/%d" % (roll, san)
    if e.is_private:
        rule = store.user_conf(e.uid, "rc房规", settings.default_coc_rule) or 0
    else:
        rule = store.group_rule(e.gid)
        if rule is None:
            rule = settings.default_coc_rule
    level = roll_success_level(roll, san, rule)
    if level == SuccessLevel.Failure:
        rd = RD(fail_exp)
        if rd.Roll() != 0:
            e.reply_msg("strSanCostInvalid")
            return 1
        change = rd.FormShortString()
    elif level == SuccessLevel.Fumble:
        rd = RD(fail_exp)
        if rd.Max() != 0:
            e.reply_msg("strSanCostInvalid")
            return 1
        change = "Max{%s}=%d" % (rd.strDice, rd.intTotal)
    else:
        rd = RD(suc_exp)
        if rd.Roll() != 0:
            e.reply_msg("strSanCostInvalid")
            return 1
        change = rd.FormShortString()
    loss = rd.intTotal
    final = max(0, san - loss)
    e.ctx["attr"] = attr
    e.ctx["res"] = res_text
    e.ctx["change"] = change
    e.ctx["loss"] = loss
    e.ctx["final"] = final
    e.ctx["rank"] = int(level)
    e.ctx["pc"] = idx_pc(e.ctx) or "调查员"
    if pc is not None and loss:
        pc.set(attr, final)
        store.mark("players")
    e.reply_msg("strSanityRoll")
    return 1


# ---------------------------------------------------------------------- .st
def _st(e: DiceEvent) -> int:
    e.skip_space()
    if e.cnt >= len(e.lower):
        e.reply_help("st")
        return 1
    if e.lower[e.cnt:e.cnt + 3] == "clr":
        if str(e.uid) not in store.players:
            e.reply_msg("strPcNotExistErr")
            return 1
        pc = store.player(e.uid).get_card("", e.gid)
        if "w" not in pc.locks:
            pc.clear()
            e.ctx["char"] = pc.name
            store.mark("players")
            e.reply_msg("strPropCleared")
        else:
            e.reply_msg("strPcLockedWrite")
        return 1
    if e.lower[e.cnt:e.cnt + 3] == "del":
        if str(e.uid) not in store.players:
            e.reply_msg("strPcNotExistErr")
            return 1
        e.cnt += 3
        e.skip_space()
        attr = e.read_attr_name()
        e.ctx["attr"] = attr
        pc = store.player(e.uid).get_card("", e.gid)
        if "w" in pc.locks:
            e.reply_msg("strPcLockedWrite")
        elif pc.erase(attr):
            store.mark("players")
            e.reply_msg("strPropDeleted")
        else:
            e.reply_msg("strPropNotFound")
        return 1

    pl = store.player(e.uid)
    pc = pl.get_card("", e.gid)
    if e.lower[e.cnt:e.cnt + 4] == "show":
        e.cnt += 4
        e.skip_space()
        attr = e.read_attr_name()
        if e.peek() == ":":
            if pl.count(attr):
                while e.peek() == ":":
                    e.cnt += 1
                pc = pl[attr]
                e.ctx["char"] = pc.name
                e.ctx["pc"] = pc.name
                attr = e.read_attr_name()
            else:
                e.ctx["char"] = attr
                e.reply_msg("strPcNameNotExist")
                return 1
        elif "的" in attr:
            pos = attr.find("的")
            owner = attr[:pos]
            if pl.count(owner):
                pc = pl[owner]
                e.ctx["char"] = pc.name
                e.ctx["pc"] = pc.name
                attr = attr[pos + 1:]
        if "r" in pc.locks:
            e.reply_msg("strPcLockedRead")
        elif not attr:
            e.ctx["char"] = pc.name
            e.ctx["type"] = pc.data.get("__Type", "")
            e.ctx["show"] = pc.show_all(False)
            e.reply_msg("strPropList")
        else:
            val = pc.show_one(attr)
            if val is not None:
                e.ctx["attr"] = attr
                e.ctx["val"] = val
                e.reply_msg("strProp")
            else:
                e.ctx["attr"] = attr
                e.reply_msg("strPropNotFound")
        return 1

    # 多卡写法 卡名::属性
    pos = e.msg.find("::", e.cnt)
    if pos >= 0:
        name = strip(filter_cqcode(e.msg[e.cnt:pos]))
        e.cnt = pos + 2
        if name:
            if not pl.count(name):
                type_name = pc.data.get("__Type", "COC7")
                res = pl.empty_card(name, e.gid, type_name)
                if res == 0:
                    if not pl.count(e.gid):
                        pl.change_card(name, e.gid)
                elif res == -1:
                    e.reply_msg("strPcCardFull")
                    return 1
                elif res == -6:
                    e.reply_msg("strPcNameInvalid")
                    return 1
                else:
                    e.reply_msg("strUnknownErr")
                    return 1
            pc = pl[name]
            e.ctx["char"] = pc.name
            e.ctx["pc"] = name
    if "w" in pc.locks:
        e.reply_msg("strPcLockedWrite")
        return 1

    logs = {}
    detailed = False
    changed = 0
    while e.cnt != len(e.lower):
        e.skip_space()
        trans = {"attr": ""}
        if e.peek() == "&":
            attr_name = e.read_to_colon()
            if attr_name:
                new_val = e.read_exp()
                if pc.set(attr_name, new_val):
                    detailed = True
                    trans = {"attr": attr_name, "action": "!",
                             "reason": get_msg_text("strPcTextTooLong")}
                else:
                    trans = {"attr": attr_name, "action": "=", "new": new_val}
                    changed += 1
                logs[attr_name] = trans
            continue
        attr_name = e.read_attr_name()
        if not attr_name:
            e.skip_space()
            while e.peek() and e.peek() in "=:+-*/":
                e.cnt += 1
            e.read_digit(False)
            continue
        has_old = pc.has_attr(attr_name)
        old_val = pc.get(attr_name) if has_old else None
        trans = {"attr": attr_name}
        if has_old:
            trans["old"] = old_val
        while e.peek_lower() in ("=", ":"):
            e.cnt += 1
        if e.peek_lower() in ("+", "-"):
            trans["old"] = pc.get(attr_name)
            mod = RD(str(old_val if old_val is not None else 0) + e.read_dice())
            eno = mod.Roll()
            if eno == 0:
                trans["action"] = "+="
                trans["expr"] = mod.FormCompleteString()
                detailed = True
                trans["new"] = mod.intTotal
                pc.set(attr_name, mod.intTotal)
                changed += 1
                while e.peek() in (" ", "\t", "|"):
                    e.cnt += 1
            else:
                detailed = True
                trans["action"] = "!"
                trans["reason"] = get_msg_text(ROLL_DICE_ERR.get(eno, "strUnknownErr"))
            logs[attr_name] = trans
            continue
        if e.cnt != len(e.lower) and not e.peek_lower().isdigit() and e.peek() not in (" ", "\t"):
            val = e.read_until_space()
            # 原版对 trust<=0 的用户过滤 CQ 码（防注入）；本移植不做信任分级，一律过滤
            val = filter_cqcode(val)
            if pc.set(attr_name, val) == 0:
                trans["action"] = "->" if has_old else ":"
                trans["new"] = val
                changed += 1
            else:
                detailed = True
                trans["action"] = "!"
                trans["reason"] = get_msg_text("strPcTextTooLong")
            logs[attr_name] = trans
            continue
        skill_val = e.read_digit()
        if not skill_val:
            detailed = True
            trans["action"] = "!"
            trans["reason"] = "Input Empty"
            logs[attr_name] = trans
            break
        if len(skill_val) > 9:
            detailed = True
            trans["action"] = "!"
            trans["reason"] = "Too Big"
            logs[attr_name] = trans
            break
        value = int(skill_val)
        if old_val != value and pc.set(attr_name, value) == 0:
            changed += 1
            trans["action"] = "->" if has_old else ":"
            trans["new"] = value
            if has_old:
                detailed = True
            logs[attr_name] = trans
        while e.peek() in (" ", "\t", "|"):
            e.cnt += 1

    if detailed:
        lines = []
        for attr, tx in logs.items():
            action = tx.get("action")
            if action == ":":
                lines.append("%s: %s" % (attr, tx.get("new")))
            elif action == "->":
                lines.append("%s: %s->%s" % (attr, tx.get("old"), tx.get("new")))
            elif action == "+=":
                lines.append("%s=%s" % (attr, tx.get("expr")))
            elif action == "=":
                lines.append("%s=%s" % (attr, tx.get("new")))
            elif action == "!":
                lines.append("%s≠ %s" % (attr, tx.get("reason")))
        e.ctx["cnt"] = changed
        e.ctx["detail"] = "\n".join(lines)
        e.reply_msg("strStDetail")
    elif changed:
        e.ctx["cnt"] = changed
        e.reply_msg("strSetPropSuccess")
    else:
        e.reply_help("st")
    if changed:
        e.ctx["trans"] = logs
        store.mark("players")
    return 1


# ------------------------------------------------------------------- .w / .ww
def _ww(e: DiceEvent) -> int:
    detail = False
    if e.peek_lower() == "w":
        e.cnt += 1
        detail = True
    hidden = False
    if e.peek_lower() == "h":
        hidden = True
        e.cnt += 1
    e.skip_space()
    if e.cnt >= len(e.msg):
        e.reply_help("ww")
        return 1
    if not e.gid:
        hidden = False
    pl_exists = str(e.uid) in store.players
    pc = store.player(e.uid).get_card("", e.gid) if pl_exists else None
    main = ""
    reason = ""
    attr = ""
    if pc is not None:
        while e.cnt < len(e.msg) and e.peek() not in (" ", "\t", "\u3000"):
            c = e.peek()
            # '#' 是轮数分隔符，漏掉它会让 .ww 3#10a8（已有角色卡时）整段被当成属性名；
            # 'd' 一并收进来，让 .ww 3d6 这类非法写法落到 DicePool 报「输入错误」，
            # 而不是把 d 当属性名、剩下的 6 并进骰数变成 36D10。
            if c.isdigit() or c in "a+-*/#dD":
                main += c
                e.cnt += 1
            elif c in "=:":
                e.cnt += 1
            else:
                attr = e.read_attr_name()
                if pc.count_exp(attr):
                    main += pc.get_exp(attr)
                elif isinstance(pc.get(attr), int):
                    main += str(pc.get(attr))
                else:
                    reason = attr
    else:
        main = e.read_dice()
    reason += e.read_rest()
    turn = 1
    default_dice = pc.get_int("__DefaultDice") if (pc is not None and pc.has("__DefaultDice")) else int(
        store.user_conf(e.uid, "默认骰", 10) or 10)
    hash_pos = main.find("#")
    if hash_pos >= 0:
        turn_str = main[:hash_pos] or "1"
        main = main[hash_pos + 1:]
        turn_rd = RD(turn_str, default_dice)
        terr = turn_rd.Roll()
        if terr:
            reply_roll_err(e, terr, turn_rd)
            return 1
        if turn_rd.intTotal > 10:
            e.reply_msg("strRollTimeExceeded")
            return 1
        if turn_rd.intTotal <= 0:
            e.reply_msg("strRollTimeErr")
            return 1
        turn = turn_rd.intTotal
        if "d" in turn_str:
            e.ctx["turn"] = turn_rd.FormShortString()
            e.reply_hidden(get_msg_text("strRollTurn"))
    if not main:
        e.reply_help("ww")
        return 1
    first = main
    for sep in "+-":
        if sep in first:
            first = first[:first.find(sep)]
    for sep in "x*":
        if sep in first:
            first = first[:first.find(sep)]
    if first.isdigit():
        main = first + "D" + main[len(first):]
    game = game_of(e)
    target = 8
    if game is not None and game.has("__DPTarget"):
        target = int(game.get("__DPTarget"))
    elif pc is not None and pc.has("__DPTarget"):
        target = pc.get_int("__DPTarget")
    pool = DicePool(main, target)
    err = pool.roll(game)
    if err:
        reply_roll_err(e, err, pool)
        return 1
    if not detail and turn != 1:
        text = get_msg_text("strRollMuiltDiceReason" if reason else "strRollMuiltDice")
        values = []
        for i in range(turn):
            pool.Roll()
            values.append(str(pool.intTotal))
        res = "{ " + ",".join(values) + " }"
        if hidden:
            e.reply_hidden(text.replace("{res}", res))
        else:
            e.ctx["res"] = res
            e.reply(text)
    else:
        for _ in range(turn):
            pool.roll(game)
            e.ctx["res"] = pool.FormCompleteString() if detail else pool.FormShortString()
            e.ctx["reason"] = reason
            text = get_msg_text("strRollDiceReason" if reason else "strRollDice")
            if hidden:
                e.reply_hidden(text)
            else:
                e.reply(text)
        e.replies = [r for r in e.replies]
    if hidden:
        e.reply_msg("strRollHidden")
    return 1


# ----------------------------------------------------------------------- .r
def _r(e: DiceEvent) -> int:
    detail = True
    if e.peek() == "s":
        detail = False
        e.cnt += 1
    if e.peek_lower() == "h":
        e.hidden = True
        e.cnt += 1
    if not e.gid:
        e.hidden = False
    e.skip_space()
    pl_exists = str(e.uid) in store.players
    pc = store.player(e.uid).get_card("", e.gid) if pl_exists else None
    reason = e.msg[e.cnt:]
    e.ctx["reason"] = reason
    main = ""
    if not reason and pc is not None and pc.count_exp("__DefaultDiceExp"):
        main = pc.get_exp("__DefaultDiceExp")
    elif pc is not None and pc.count_exp(reason):
        main = pc.get_exp(reason)
    else:
        main = e.read_dice()
        if any(not c.isdigit() for c in main):
            reason = e.read_rest()
            e.ctx["reason"] = reason
        else:
            main = ""
    turn = 1
    default_dice = _default_dice(e, pc)
    if "#" in main:
        turn_text = main[:main.find("#")] or "1"
        e.ctx["turn"] = turn_text
        main = main[main.find("#") + 1:]
        turn_rd = RD(turn_text, default_dice)
        terr = turn_rd.Roll()
        if terr:
            reply_roll_err(e, terr, turn_rd)
        if turn_rd.intTotal > 10:
            e.reply_msg("strRollTimeExceeded")
            return 1
        if turn_rd.intTotal <= 0:
            e.reply_msg("strRollTimeErr")
            return 1
        turn = turn_rd.intTotal
        if "d" in turn_text:
            e.ctx["turn"] = turn_rd.FormShortString()
            if e.hidden:
                e.reply_hidden(get_msg_text("strRollTurn"))
            else:
                e.reply_msg("strRollTurn")
    if not main and pc is not None and pc.count_exp(reason):
        main = pc.get_exp(reason)
    rd = RD(main, default_dice)
    game = game_of(e)
    first = rd.Roll(game) if (game is not None and game.is_part(e.uid)) else rd.Roll()
    if first:
        reply_roll_err(e, first, rd)
        return 1
    e.ctx["dice_exp"] = rd.strDice
    is_static = default_dice <= 100 and pc is not None and rd.strDice == "D%d" % default_dice
    text_key = ("strRollMultiDiceReason" if reason else "strRollMultiDice") if turn != 1 \
        else ("strRollDiceReason" if reason else "strRollDice")

    if not detail and turn != 1:
        values = []
        extremes = []
        for i in range(turn):
            rd.Roll()
            if is_static:
                pc.cnt_roll_stat(rd.intTotal, default_dice)
            values.append(str(rd.intTotal))
            if rd.strDice in ("D100", "1D100") and (rd.intTotal <= 5 or rd.intTotal >= 96):
                extremes.append(str(rd.intTotal))
        res = "{ " + ",".join(values) + " }"
        if extremes:
            res += ",极值: " + ",".join(extremes)
        if e.hidden:
            e.reply_hidden(get_msg_text(text_key).replace("{res}", res))
        else:
            e.ctx["res"] = res
            e.reply(get_msg_text(text_key))
    else:
        if turn > 1:
            dices = ResList()
            for _ in range(turn):
                rd.Roll()
                if is_static:
                    pc.cnt_roll_stat(rd.intTotal, default_dice)
                form = str(rd.intTotal)
                if detail:
                    combined = rd.FormStringCombined()
                    separate = rd.FormStringSeparate()
                    if combined != form:
                        form = combined + "=" + form
                    if separate != rd.strDice and separate != combined:
                        form = separate + "=" + form
                dices.add(form)
            e.ctx["res"] = dices.dot(", ").line(8).show()
        else:
            if is_static:
                pc.cnt_roll_stat(rd.intTotal, default_dice)
            e.ctx["res"] = rd.FormCompleteString() if detail else rd.FormShortString()
        if is_static:
            store.mark("players")
        if e.hidden:
            e.reply_hidden(get_msg_text(text_key))
        else:
            e.reply(get_msg_text(text_key))
    if e.hidden:
        e.reply_msg("strRollHidden")
    return 1
