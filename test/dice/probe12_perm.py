# -*- coding: utf-8 -*-
"""权限验证：主人 / 群主 / 管理员 / 普通成员。"""
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

from nonebot.adapters.onebot.v11 import GroupMessageEvent, PrivateMessageEvent

pkg = 'ATRI.plugins.nonebot_plugin_dice'
p = importlib.import_module(pkg)
ev = importlib.import_module(pkg + '.event')
o = importlib.import_module(pkg + '.orders')
st = importlib.import_module(pkg + '.store')
se = importlib.import_module(pkg + '.settings')
dk = importlib.import_module(pkg + '.decks')

TMP = Path(tempfile.mkdtemp(prefix='diceperm_'))
st.store.dir = TMP
se.settings.dir = TMP
dk.decks._path = TMP / 'decks.yml'
dk.decks._public_dir = TMP / 'PublicDeck'
st.store.flush_all()
se.settings.load()
dk.decks.load()


class Sender:
    def __init__(self, role):
        self.role = role


class Bot:
    async def get_group_member_info(self, group_id=0, user_id=0, no_cache=False):
        return {"role": ""}

    async def send_group_msg(self, *a, **k):
        pass

    async def send_private_msg(self, *a, **k):
        pass

    async def call_api(self, api, **data):
        return {'user_id': 10001}


def gmsg(uid, role, gid=555):
    return GroupMessageEvent.construct(
        group_id=gid, user_id=uid, sender=Sender(role), self_id=10000,
        message_type="group", raw_message="")


def pmsg(uid):
    return PrivateMessageEvent.construct(
        user_id=uid, sender=Sender(""), self_id=10000,
        message_type="private", raw_message="")


class B:
    can_host = False
    is_master = False


async def run(text, can_host=False, is_master=False, gid=555, private=False):
    e = ev.DiceEvent(msg=ev.normalize_message(text), uid=10001, gid=0 if private else gid,
                    nick='测试', self_id=10000, self_name=SELF,
                    can_host=can_host, is_private=private, is_master=is_master)
    e.ctx['nick'] = '测试'
    h = await o.basic_order(e, Bot())
    if not h:
        h = await o.inner_order(e, Bot())
    flag = '主人' if is_master else ('可主持' if can_host else '普通成员')
    print('>> [%s] %s' % (flag, text))
    for r in e.replies:
        print('   ' + r.replace('\n', '\n   '))
    if not e.replies:
        print('   <无回复>')
    st.store.flush_all()


async def main():
    print('--- canRoomHost 判定 ---')
    print('私聊            ->', await p._can_host(Bot(), pmsg(10001), False))
    print('群主 owner      ->', await p._can_host(Bot(), gmsg(10001, 'owner'), False))
    print('管理员 admin    ->', await p._can_host(Bot(), gmsg(10001, 'admin'), False))
    print('普通成员 member ->', await p._can_host(Bot(), gmsg(10001, 'member'), False))
    print('role 缺失(查询失败) ->', await p._can_host(Bot(), gmsg(10001, ''), False))
    print('主人(成员身份)  ->', await p._can_host(Bot(), gmsg(10001, 'member'), True))

    print('--- 群内开关 ---')
    await run('.bot off')
    await run('.bot off', can_host=True)
    await run('.setcoc 1')
    await run('.setcoc 1', can_host=True)
    await run('.deck new 甲=1|2')
    await run('.deck new 甲=1|2', can_host=True)
    await run('.system load')
    await run('.system load', can_host=True, is_master=True)
    await run('.help off')
    await run('.help off', can_host=True)


asyncio.run(main())
