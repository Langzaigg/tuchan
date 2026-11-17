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
sess_mod = importlib.import_module(pkg + '.session')

TMP = tempfile.mkdtemp(prefix='dicelog_')
store_mod.store.dir = Path(TMP)
store_mod.store.flush_all()

SENT = []


class FakeBot(Bot):
    def __init__(self):
        super().__init__(Adapter(nonebot.get_driver()), '10000')

    async def call_api(self, api, **data):
        SENT.append((api, {k: str(v)[:60] for k, v in data.items()}))
        return {'message_id': 1}


def make_event(text, uid=10001, nick='测试', card='测试名片'):
    msg = Message(MessageSegment.text(text)) if text else Message()
    sender = Sender(user_id=uid, nickname=nick, card=card)
    return GroupMessageEvent(
        time=0, self_id=10000, post_type='message', sub_type='normal',
        user_id=uid, message_type='group', message_id=1, message=msg,
        original_message=msg, raw_message=str(msg), font=0, sender=sender,
        group_id=555, to_me=False)


async def dispatch(text, uid=10001, nick='测试', card='测试名片'):
    from nonebot.message import handle_event
    SENT.clear()
    await handle_event(FakeBot(), make_event(text, uid, nick, card))
    return list(SENT)


async def main():
    for text in ['.log new 第一次跑团', '大家晚上好', '.r 1d100 察觉',
                 '.st 力量:60', '我看看', '.log off', '这句不该进日志',
                 '.log on', '继续', '.log end']:
        sent = await dispatch(text)
        print('>> %-16s -> %d' % (text, len(sent)))
        for api, data in sent:
            print('    ', api, data)
    game = sess_mod.sessions.get_by_name('g555')
    print('--- session log state:', game.log)
    p = game.log_path()
    print('--- file:', p, p.exists())
    if p.exists():
        print(p.read_text(encoding='utf-8'))


asyncio.run(main())
