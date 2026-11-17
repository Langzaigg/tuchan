import asyncio
import importlib
import os
import shutil
import sys
import tempfile

PROJECT = r'c:\Users\momiji\Desktop\ATRI-main'
sys.path.insert(0, PROJECT)

import nonebot
nonebot.init()

from ATRI.plugins.nonebot_plugin_dice import _self_name as _dice_self_name

SELF = _dice_self_name()  # 真实自称：config.yml -> DICE_SELF_NAME -> NICKNAME

pkg = 'ATRI.plugins.nonebot_plugin_dice'
ev = importlib.import_module(pkg + '.event')
orders = importlib.import_module(pkg + '.orders')
store_mod = importlib.import_module(pkg + '.store')
sess_mod = importlib.import_module(pkg + '.session')

TMP = tempfile.mkdtemp(prefix='dicetest_')
from pathlib import Path
store_mod.store.dir = Path(TMP)
store_mod.store.flush_all()


class FakeBot:
    async def send_group_msg(self, group_id, message):
        print('   [私聊->群%s] %s' % (group_id, message))

    async def send_private_msg(self, user_id, message):
        print('   [私聊->%s] %s' % (user_id, message))


async def run(text, uid=10001, gid=555, nick='测试', can_host=True, show_hidden=True):
    e = ev.DiceEvent(
        msg=ev.normalize_message(text), uid=uid, gid=gid, nick=nick,
        self_id=10000, self_name=SELF, trusted=0, can_host=can_host,
        is_private=(gid == 0))
    e.ctx['nick'] = nick
    handled = 0
    try:
        handled = await orders.basic_order(e, FakeBot())
        if not handled:
            if gid and store_mod.store.group_opt(gid, '停用指令'):
                handled = 0
            else:
                handled = await orders.inner_order(e, FakeBot())
    except Exception:
        import traceback
        traceback.print_exc()
    print('>> %s' % text)
    for r in e.replies:
        print('   ' + r.replace('\n', '\n   '))
    if show_hidden:
        for r in e.hidden_replies:
            print('   (暗) ' + r.replace('\n', '\n   '))
    if not e.replies and not e.hidden_replies:
        print('   <无回复> handled=%s' % handled)
    store_mod.store.flush_all()
    return e


async def main():
    cases = [
        '.help', '.help r', '.help 退群', '.help 不存在的词',
        '.r', '.r 1d6', '.r 2d6+3 测试原因', '.r3#d6', '.rs 3d6', '.rh 1d100',
        '.r 1d100 对抗', '.rd6', '.r d', '.r 100', '.r 20',
        '.ww 10a8', '.w 5a10', '.ww 5', '.ww 3#10a8',
        '.coc', '.coc6', '.coc7d', '.coc6d', '.dnd', '.dnd3',
        '.jrrp', '.rule set COC7', '.rules 大失败', '.rules COC7:大失败', '.rule',
        '.set', '.set 20', '.st', '.st 力量:50 敏捷:60', '.st 力量+1d6', '.st show',
        '.st 力量', '.st del 力量', '.pc', '.pc new 卡特', '.pc list', '.pc show',
        '.rc 力量', '.rc 困难力量', '.rc 力量 50', '.rcp 力量', '.rc3#力量 50',
        '.sc 0/1 70', '.sc 1d10/1d100 50', '.en 教育 60', '.en 教育 +1d10',
        '.ti', '.li', '.nn 小明', '.nn', '.me 笑出了声', '.bot',
        '.init', '.ri', '.init list', '.init clr',
        '.game', '.game new 测试桌', '.game state', '.game join', '.game set rule=COC7',
        '.game rou 100', '.game rou hist', '.ob join', '.ob list', '.ak#标题',
        '.ak+选项一|选项二', '.ak show', '.ak=', '.game over',
        '.setcoc', '.setcoc 1', '.setcoc show', '.setcoc clr',
        '.welcome', '.welcome show',
        '.bot on', '.bot off', '.bot', '.help off', '.help', '.help on',
    ]
    for c in cases:
        await run(c)


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
