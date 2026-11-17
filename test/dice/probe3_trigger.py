import asyncio
import importlib
import sys
import tempfile
from pathlib import Path

PROJECT = r'c:\Users\momiji\Desktop\ATRI-main'
sys.path.insert(0, PROJECT)

import nonebot
nonebot.init()

from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent, Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Sender

pkg = 'ATRI.plugins.nonebot_plugin_dice'
plugin = importlib.import_module(pkg)
store_mod = importlib.import_module(pkg + '.store')

TMP = tempfile.mkdtemp(prefix='dicetest2_')
store_mod.store.dir = Path(TMP)
store_mod.store.flush_all()

SENT = []


class FakeBot(Bot):
    def __init__(self):
        super().__init__(Adapter(nonebot.get_driver()), '10000')

    async def call_api(self, api, **data):
        if api in ('send_group_msg', 'send_private_msg'):
            SENT.append((api, data))
            return {'message_id': 1}
        if api == 'get_group_member_info':
            return {'role': 'member'}
        return {}

    async def send(self, event, message, **kwargs):
        SENT.append(('send', str(message)))
        return {'message_id': 1}


def make_event(text, at=None, group=True, uid=10001):
    msg = Message()
    if at is not None:
        msg.append(MessageSegment.at(at))
    if text:
        msg.append(MessageSegment.text(text))
    sender = Sender(user_id=uid, nickname='测试', card='测试名片')
    if group:
        return GroupMessageEvent(
            time=0, self_id=10000, post_type='message', sub_type='normal',
            user_id=uid, message_type='group', message_id=1, message=msg,
            original_message=msg, raw_message=str(msg), font=0, sender=sender,
            group_id=555, to_me=(at == 10000),
        )
    raise RuntimeError


async def dispatch(event):
    from nonebot.message import handle_event
    SENT.clear()
    try:
        await handle_event(FakeBot(), event)
    except Exception as exc:
        print('   !! %s: %s' % (type(exc).__name__, exc))
    return list(SENT)


async def main():
    cases = [
        ('普通聊天，不触发', None),
        ('.r 1d6', None),
        ('。r 1d6', None),
        ('！r 1d6', None),
        ('!r 1d6', None),
        ('．r 1d6', None),
        ('.不是指令', None),
        ('。识别 图片', None),
        ('.r 1d6', 10000),
        ('.r 1d6', 99999),
        ('.help', None),
    ]
    for text, at in cases:
        ev = make_event(text, at)
        sent = await dispatch(ev)
        print('>> %r at=%s -> %d 条' % (text, at, len(sent)))
        for item in sent:
            print('    ', item[0], str(item[1])[:80].replace('\n', ' | '))
        await asyncio.sleep(0)


asyncio.run(main())
