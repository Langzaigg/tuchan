import asyncio
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

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
store_mod.store.dir = Path(TMP)
store_mod.store.flush_all()


class FakeBot:
    async def send_group_msg(self, group_id, message):
        print('   [群%s] %s' % (group_id, message))

    async def send_private_msg(self, user_id, message):
        print('   [私聊%s] %s' % (user_id, message))


async def run(text, uid=10001, gid=555, nick='测试'):
    e = ev.DiceEvent(msg=ev.normalize_message(text), uid=uid, gid=gid, nick=nick,
                    self_id=10000, self_name=SELF, can_host=True, is_private=(gid == 0))
    e.ctx['nick'] = nick
    handled = 0
    try:
        handled = await orders.basic_order(e, FakeBot())
        if not handled and not (gid and store_mod.store.group_opt(gid, '停用指令')):
            handled = await orders.inner_order(e, FakeBot())
    except Exception:
        import traceback
        traceback.print_exc()
    print('>> %s' % text)
    for r in e.replies:
        print('   ' + r.replace('\n', '\n   '))
    for r in e.hidden_replies:
        print('   (暗) ' + r.replace('\n', '\n   '))
    if not e.replies and not e.hidden_replies:
        print('   <无回复>')
    store_mod.store.flush_all()


async def main():
    for c in [
        '.st 力量:60 敏捷:50 体质:55 体型:65 意志:50 教育:70 幸运:45 理智:50 生命:12 魔法:10',
        '.rc 力量', '.rc 困难力量', '.rc 极难力量', '.rc 简单力量', '.rc 自动成功力量',
        '.rc 力量*5', '.rc 力量-10', '.rc 力量/2', '.rcp 力量', '.rcb2 力量',
        '.rc 力量 50 理由', '.rc _力量', '.rc3#力量',
        '.sc 0/1', '.sc 1d10/1d100 50', '.sc -1d6/-1d6',
        '.en 教育', '.en 教育 +1d10', '.en 幸运 +1d3/1d10',
        '.st show 力量', '.st 生命-1', '.st &伤害=1d6+DB',
        '.pc build', '.pc show', '.pc stat', '.pc temp', '.pc nn 新卡',
        '.pc new COC7:背景', '.pc list',
        '.game new COC7-团', '.game state', '.game rou 6', '.r 1d6', '.r 1d6',
        '.game rou hist', '.game rou reset', '.game rou clr',
        '.game kick 10002', '.game exit', '.game open 团#1', '.game over',
        '.init del', '.ri 17 boss', '.ri 3#木乃伊', '.init list',
        '.me off', '.me', '.me on', '.me',
        '.jrrp off', '.jrrp', '.jrrp on',
        '.help 设定', '.help 协议', '.help 群管', '.help 房规',
        '.wwh 5a8', '.wh 3a8',
        '.r 2d6#5', '.r 5#d6', '.r 1d6+1d6 理由', '.r 1d100',
    ]:
        await run(c)


asyncio.run(main())
shutil.rmtree(TMP, ignore_errors=True)
