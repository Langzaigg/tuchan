# -*- coding: utf-8 -*-
"""帮助文档体检：逐条 .help <key>，标出空回复、残留占位符、悬空 & 重定向。"""
import asyncio
import importlib
import re
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
md = importlib.import_module(pkg + '._msgdata')
cs = importlib.import_module(pkg + '.constants')

TMP = tempfile.mkdtemp()
st.store.dir = Path(TMP)
se.settings.dir = Path(TMP)
st.store.flush_all()
se.settings.load()


class B:
    async def send_group_msg(self, *a, **k):
        pass

    async def send_private_msg(self, *a, **k):
        pass

    async def call_api(self, api, **data):
        return {}


async def ask(text, gid=555):
    e = ev.DiceEvent(msg=ev.normalize_message(text), uid=10001, gid=gid, nick='测试',
                    self_id=10000, self_name=SELF, can_host=True, is_private=False)
    e.ctx['nick'] = '测试'
    handled = await o.basic_order(e, B())
    if not handled:
        await o.inner_order(e, B())
    return handled, e.replies


PH = re.compile(r'(?<!\\)\{[^{}\n]{0,80}\}')


async def main():
    keys = sorted(cs.GLOBAL_HELP)
    empty, leftover, dangling = [], [], []
    lines = []
    for k in keys:
        handled, replies = await ask('.help ' + k)
        body = '\n'.join(replies)
        lines.append('==== %s (handled=%s) ====' % (k, handled))
        lines.append(body if body else '<EMPTY>')
        if not body.strip():
            empty.append(k)
        elif body.strip() == '<EMPTY>':
            empty.append(k)
        for m in PH.findall(body):
            leftover.append((k, m))
    # 悬空 & 重定向：只检查仍留在索引里的词条
    for k in keys:
        v = cs.GLOBAL_HELP[k].strip()
        if v.startswith('&') and '{' not in v:
            tgt = v[1:].strip()
            if tgt.lower() not in cs.GLOBAL_HELP:
                dangling.append((k, tgt))
    lines.append('==== 报告 ====')
    lines.append('空回复: %s' % (empty or '无'))
    lines.append('残留占位符: %s' % (leftover or '无'))
    lines.append('悬空重定向: %s' % (dangling or '无'))
    Path(r'C:\Users\momiji\AppData\Local\Temp\dice_help_probe.txt').write_text(
        '\n'.join(lines), encoding='utf-8')
    print('空回复', len(empty), '残留', len(leftover), '悬空', len(dangling))


asyncio.run(main())
