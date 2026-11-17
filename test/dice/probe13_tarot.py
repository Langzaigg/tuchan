# -*- coding: utf-8 -*-
"""塔罗范例牌堆验证：结构 + 走 .draw 实际抽取。"""
import asyncio
import importlib
import json
import shutil
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

SRC = Path(r'c:\Users\momiji\Desktop\nonebot-plugin-dice\examples\PublicDeck\塔罗.json')
if not SRC.exists():
    print('跳过：范例文件不存在', SRC)
    raise SystemExit(0)

data = json.loads(SRC.read_text(encoding='utf-8'))
big = data['塔罗大阿卡纳']
small = data['塔罗小阿卡纳']
full = data['塔罗']
print('大阿卡纳', len(big), '小阿卡纳', len(small), '全套', len(full))
print('全套无重复:', len(set(full)) == len(full) == 78)
print('大+小 == 全套:', big + small == full)
print('四大花色各14张:', all(
    sum(1 for c in small if c.startswith(s)) == 14 for s in ('权杖', '圣杯', '宝剑', '星币')))
bad = [c for c in full if c not in set(big) | set(small)]
print('无未知卡面:', not bad)

TMP = Path(tempfile.mkdtemp(prefix='dicetarot_'))
st.store.dir = TMP
se.settings.dir = TMP
dk.decks._path = TMP / 'decks.yml'
dk.decks._public_dir = TMP / 'PublicDeck'
dk.decks._public_dir.mkdir(parents=True, exist_ok=True)
shutil.copy(SRC, dk.decks._public_dir / '塔罗.json')
st.store.flush_all()
se.settings.load()
dk.decks.load()
print('加载牌堆:', [n for n in data if n in dk.decks.decks])


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
    for c in ['.draw 塔罗 3', '.draw 塔罗大阿卡纳 2', '.draw 塔罗单抽',
              '.draw 塔罗牌阵', '.draw 权重示例 4', '.draw 塔罗正逆 2', '.draw 塔罗正逆']:
        await run(c)


asyncio.run(main())
