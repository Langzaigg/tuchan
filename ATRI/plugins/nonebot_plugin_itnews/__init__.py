# python3
# -*- coding: utf-8 -*-
# @Time    : 2021/11/10 16:58
# @Author  : yzyyz
# @Email   :  youzyyz1384@qq.com
# @File    : __init__.py
# @Software: PyCharm
import nonebot
from nonebot import on_command
from nonebot.rule import to_me
from nonebot.adapters.onebot.v11 import Bot, Event,Message,MessageEvent,MessageSegment
from nonebot.params import CommandArg
from nonebot.typing import T_State
from . import itnews
import os, shutil
import time
itnew = on_command(".新闻", aliases={"。新闻", "。咨讯", ".咨讯"})

@itnew.handle()
async def tianqi(bot: Bot, event: Event, args: Message = CommandArg()):
    year = time.strftime("%Y", time.localtime())
    mon = time.strftime("%m", time.localtime())
    day = time.strftime("%d", time.localtime())

    fname = "./data/news/"+str(year) + str(mon) + str(day) + ".png"
    if event.get_user_id != event.self_id:
        if ids := args.extract_plain_text():
            if ids in ['更新', 'up']:
                shutil.rmtree("./data/news/")
                ids = 0
                fname = "./data/news/"+str(year) + str(mon) + str(day) + ".png"
            elif ids.strip().isdigit() and 1 <= int(ids.strip()) <= 20:
                ids = int(ids.strip())
                fname = "./data/news/"+str(year) + str(mon) + str(day) + '_' + str(ids) + ".png" 
            else:
               await itnew.finish('请输入1-20的序号查看新闻内容')
        else:
            ids = 0
            fname = "./data/news/"+str(year) + str(mon) + str(day) + ".png"

        path = os.path.abspath(fname)
        if os.path.exists(path)==False:
            keys='b46fc253325c5c82fafb1d7c5c1459d4'
            itnews.draw_news(keys, ids)
            await bot.send(
                event = event,
                message = MessageSegment.image(path),
                at_sender = True
            )
        else:
            print(path)
            await bot.send(
                event=event,
                message=MessageSegment.image(path),
            )

__usage__ = """
"it新闻", "It新闻","IT新闻","it咨讯", "It咨讯","IT咨讯","IT","it","It"
"""
__plugin_name__ = "it咨讯"

__permission__ = 2
__help__version__ = '0.1.5'