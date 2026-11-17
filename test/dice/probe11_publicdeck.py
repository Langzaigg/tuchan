# -*- coding: utf-8 -*-
"""原版牌堆包（PublicDeck/*.json）加载验证。"""
import asyncio
import importlib
import json
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

TMP = Path(tempfile.mkdtemp(prefix='dicedeck_'))
st.store.dir = TMP
se.settings.dir = TMP
dk.decks._path = TMP / 'decks.yml'
dk.decks._public_dir = TMP / 'PublicDeck'
st.store.flush_all()
se.settings.load()

# 造一个「原版格式」的牌堆包：{ "牌堆名": ["卡面", ...] }
pub = TMP / 'PublicDeck'
pub.mkdir(parents=True, exist_ok=True)
(pub / 'COC7.json').write_text(json.dumps({
    "塔罗测试": ["愚者", "魔术师", "女祭司"],
    "带权重": ["::3::甲", "乙"],
    "嵌套姓": ["{随机姓氏}某"],
    "内嵌骰": ["掷出[1d6]点"],
    "随机姓氏": ["赵", "钱", "孙", "李"],       # 与内置同名 → 应覆盖内置
    "yml覆盖我": ["来自 PublicDeck"],
}, ensure_ascii=False), encoding='utf-8')
(pub / '.隐藏.json').write_text('{"不该出现": ["x"]}', encoding='utf-8')
(pub / '坏文件.json').write_text('{ 这不是 json', encoding='utf-8')

# decks.yml 里显式写一条同名 → 应压过 PublicDeck
(TMP / 'decks.yml').write_text(
    'decks:\n  yml覆盖我: ["来自 decks.yml"]\n', encoding='utf-8')

dk.decks.load()
names = list(dk.decks.decks)
print('牌堆数', len(names))
print('PublicDeck 收到:', [n for n in ('塔罗测试', '带权重', '嵌套姓', '内嵌骰') if n in names])
print('隐藏文件被忽略:', '不该出现' not in names)
print('坏文件不影响其他:', '塔罗测试' in names)
print('PublicDeck 覆盖内置(随机姓氏=赵钱孙李):', dk.decks.get('随机姓氏'))
print('yml 覆盖 PublicDeck:', dk.decks.get('yml覆盖我'))


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
    for c in ['.draw 塔罗测试 3', '.draw 嵌套姓 3', '.draw 内嵌骰 2',
              '.draw 带权重 4', '.draw 带权重 2', '.draw 随机姓氏 3',
              '.deck new 临时=甲|乙', '.deck set 复用=塔罗测试',
              '.deck show 复用', '.draw 复用', '.deck show 临时',
              '.help 全牌堆列表']:
        await run(c)


asyncio.run(main())
