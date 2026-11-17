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


async def run(t, gid=555):
    e = ev.DiceEvent(msg=ev.normalize_message(t), uid=10001, gid=gid, nick='测试',
                    self_id=10000, self_name=SELF, can_host=True, is_private=(gid == 0))
    e.ctx['nick'] = '测试'
    handled = await o.basic_order(e, B())
    if not handled:
        await o.inner_order(e, B())
    print('>>', t)
    for r in e.replies:
        print('  ' + r.replace('\n', '\n  '))
    if not e.replies:
        print('  <无回复>')
    st.store.flush_all()


async def main():
    for c in ['.pc new COC7:pc:小明', '.pc show', '.rc 小明:力量', '.rc 小明的敏捷',
              '.pc tag 小明', '.rc 力量', '.pc grp', '.pc tag', '.pc clr', '.pc list',
              '.pc new COC7:bg:二娃', '.pc show 二娃',
              '.bot off', '.help', '.r 1d6', '.bot on', '.r 1d6']:
        await run(c)


asyncio.run(main())
