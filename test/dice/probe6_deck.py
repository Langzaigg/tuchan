import asyncio
import importlib
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
settings_mod = importlib.import_module(pkg + '.settings')
decks_mod = importlib.import_module(pkg + '.decks')

TMP = tempfile.mkdtemp(prefix='dicetest4_')
store_mod.store.dir = Path(TMP)
settings_mod.settings.dir = Path(TMP)
decks_mod.decks._path = Path(TMP) / 'decks.yml'
store_mod.store.flush_all()
settings_mod.settings.load()
decks_mod.decks.load()


class FakeBot:
    async def send_group_msg(self, group_id, message):
        print('   [群%s] %s' % (group_id, message))

    async def send_private_msg(self, user_id, message):
        print('   [私聊%s] %s' % (user_id, message))

    async def call_api(self, api, **data):
        if api == 'get_group_member_list':
            return [{'user_id': 10001}, {'user_id': 10002}, {'user_id': 10003}]
        return {}


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
        '.name', '.name cn', '.name 3', '.name jp',
        '.nnn', '.nnn cn',
        '.draw', '.draw 随机姓氏', '.draw 随机姓氏 5', '.draw _随机姓氏 2',
        '.deck', '.deck new 测试=甲|乙|丙', '.deck show', '.deck show 测试',
        '.draw 测试', '.draw 测试', '.draw 测试', '.draw 测试',
        '.deck show 测试',
        '.deck reset 测试', '.deck show 测试',
        '.deck set 序号=range 1 5', '.deck show 序号', '.draw 序号 3', '.deck show 序号',
        '.deck set 成员=member', '.deck show 成员', '.draw 成员 2',
        '.deck del 测试', '.deck clr', '.deck show',
        '.deck new 加权=甲::3::乙', '.deck show 加权', '.draw 加权 3',
        '.deck set', '.deck new',
    ]:
        await run(c)


asyncio.run(main())
