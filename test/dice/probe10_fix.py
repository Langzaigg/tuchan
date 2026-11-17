# -*- coding: utf-8 -*-
"""原版缺陷修正验证：.rc 负修正、.ww 带轮数的骰池。"""
import asyncio
import importlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, r'c:\Users\momiji\Desktop\ATRI-main')
import nonebot
nonebot.init()

from ATRI.plugins.nonebot_plugin_dice import _self_name as _dice_self_name

SELF = _dice_self_name()  # 真实自称：config.yml -> DICE_SELF_NAME -> NICKNAME

pkg = 'ATRI.plugins.nonebot_plugin_dice'
ev = importlib.import_module(pkg + '.event')
o = importlib.import_module(pkg + '.orders')
st = importlib.import_module(pkg + '.store')
se = importlib.import_module(pkg + '.settings')
dk = importlib.import_module(pkg + '.decks')

TMP = tempfile.mkdtemp()
st.store.dir = Path(TMP)
se.settings.dir = Path(TMP)
dk.decks._path = Path(TMP) / 'decks.yml'
st.store.flush_all()
se.settings.load()
dk.decks.load()


class B:
    async def send_group_msg(self, *a, **k):
        pass

    async def send_private_msg(self, *a, **k):
        pass

    async def call_api(self, api, **data):
        return {'user_id': 10001}


async def run(t, gid=555):
    e = ev.DiceEvent(msg=ev.normalize_message(t), uid=10001, gid=gid, nick='测试',
                    self_id=10000, self_name=SELF, can_host=True, is_private=False)
    e.ctx['nick'] = '测试'
    h = await o.basic_order(e, B())
    if not h:
        h = await o.inner_order(e, B())
    print('>>', t)
    for r in e.replies:
        print('   ' + r.replace('\n', '\n   '))
    if not e.replies:
        print('   <无回复>')
    st.store.flush_all()


async def main():
    for c in ['.st 敏捷:60', '.rc 敏捷', '.rc 敏捷+10', '.rc 敏捷-10', '.rc 敏捷-200',
              '.ww 3#10a8', '.ww 10a8', '.ww 2#3d6', '.r 1d6']:
        await run(c)


asyncio.run(main())
