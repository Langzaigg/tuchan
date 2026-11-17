import asyncio
import importlib
import sys
import tempfile
from pathlib import Path

PROJECT = r'c:\Users\momiji\Desktop\ATRI-main'
sys.path.insert(0, PROJECT)

import nonebot
nonebot.init()

from nonebot import on_message
from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender

pkg = 'ATRI.plugins.nonebot_plugin_dice'
plugin = importlib.import_module(pkg)
store_mod = importlib.import_module(pkg + '.store')

TMP = tempfile.mkdtemp(prefix='dicetest3_')
store_mod.store.dir = Path(TMP)
store_mod.store.flush_all()

SENT = []
OTHER = []

other_matcher = on_message(priority=50, block=False)


@other_matcher.handle()
async def _other(event: GroupMessageEvent):
    OTHER.append(str(event.message))


class FakeBot(Bot):
    def __init__(self):
        super().__init__(Adapter(nonebot.get_driver()), '10000')

    async def call_api(self, api, **data):
        if api in ('send_group_msg', 'send_private_msg', 'send_msg'):
            SENT.append((api, data))
            return {'message_id': 1}
        if api == 'get_group_member_info':
            return {'role': 'member'}
        return {}

    async def send(self, event, message, **kwargs):
        SENT.append(('send', str(message)))
        return {'message_id': 1}


def make_event(text, at=None, uid=10001):
    msg = Message()
    if at is not None:
        msg.append(MessageSegment.at(at))
    if text:
        msg.append(MessageSegment.text(text))
    sender = Sender(user_id=uid, nickname='测试', card='测试名片')
    return GroupMessageEvent(
        time=0, self_id=10000, post_type='message', sub_type='normal',
        user_id=uid, message_type='group', message_id=1, message=msg,
        original_message=msg, raw_message=str(msg), font=0, sender=sender,
        group_id=555, to_me=(at == 10000),
    )


async def dispatch(event):
    from nonebot.message import handle_event
    SENT.clear()
    OTHER.clear()
    try:
        await handle_event(FakeBot(), event)
    except Exception as exc:
        print('   !! %s: %s' % (type(exc).__name__, exc))
    return list(SENT), list(OTHER)


async def main():
    cases = [
        ('普通聊天，不触发', None),
        ('.r 1d6', None),
        ('。r 1d6', None),
        ('！r 1d6', None),
        ('．r 1d6', None),
        ('.不是指令', None),
        ('。识别 图片', None),
        ('.r 1d6', 99999),
        ('.bot off', None),
        ('.r 1d6', None),
        ('.bot on', None),
        ('.r 1d6', None),
    ]
    for text, at in cases:
        ev = make_event(text, at)
        sent, other = await dispatch(ev)
        print('>> %-18r at=%-6s 骰子回复=%d 其他插件=%s' % (text, at, len(sent), other))


asyncio.run(main())
