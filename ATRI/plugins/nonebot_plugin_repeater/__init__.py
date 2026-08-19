import re

from nonebot import on_message, on_notice, logger
from nonebot.adapters.onebot.v11 import (
    Bot,
    GroupMessageEvent,
    GroupRecallNoticeEvent,
    Message,
    MessageSegment,
)

from . import config
import random

repeater_group = config.repeater_group
shortest = config.shortest_length
blacklist = config.blacklist
whitelist = config.whitelist
repeat_probability = config.repeat_probability
recall_whitelist = config.recall_whitelist  # 撤回还原功能生效群白名单

m = on_message(priority=10, block=False)
recall_notice = on_notice(priority=10, block=False)

last_message = {}
message_times = {}


# 消息预处理
def message_preprocess(message: str):
    raw_message = message
    # contained_images = {}
    # images = re.findall(r'\[CQ:image.*?]', message)
    # pattern = r'file=http://gchat.qpic.cn/gchatpic_new/\d+/\d+-\d+-(.*?)/.*?[,\]]'
    # for i in images:
    #     contained_images.update({i: [re.findall(r'url=(.*?)[,\]]', i)[0], re.findall(pattern, i)[0]]})
    # for i in contained_images:
    #     message = message.replace(i, f'[{contained_images[i][1]}]')
    return message.replace('你', '我'), raw_message


@m.handle()
async def repeater(bot: Bot, event: GroupMessageEvent):
    # 检查是否在黑名单中
    if event.raw_message in blacklist:
        logger.debug(f'[复读姬] 检测到黑名单消息: {event.raw_message}')
        return
    gid = str(event.group_id)
    if gid in repeater_group or "all" in repeater_group:
        global last_message, message_times
        message, raw_message = message_preprocess(str(event.message))
        logger.debug(f'[复读姬] 这一次消息: {message}')
        logger.debug(f'[复读姬] 上一次消息: {last_message.get(gid)}')
        if last_message.get(gid) != message:
            message_times[gid] = 1
        else:
            message_times[gid] += 1
        logger.debug(f'[复读姬] 已重复次数: {message_times.get(gid)}/{config.shortest_times}')
        if message_times.get(gid) == config.shortest_times:
            logger.debug(f'[复读姬] 原始的消息: {str(event.message)}')
            logger.debug(f"[复读姬] 欲发送信息: {raw_message}")
            await bot.send_group_msg(group_id=event.group_id, message=raw_message, auto_escape=False)
        elif message_times[gid] == 1 and gid in whitelist and random.random() < repeat_probability and '我' in raw_message:
            message_times[gid] = config.shortest_times
            await bot.send_group_msg(group_id=event.group_id, message=raw_message.translate(str.maketrans({'你': '我', '我': '你'})).replace('自你', '自我'), auto_escape=False)
        last_message[gid] = message


@recall_notice.handle()
async def recall_handler(bot: Bot, event: GroupRecallNoticeEvent):
    """监听群消息撤回事件，在白名单群内 at 撤回操作者并提示阅读群规。

    不维护额外消息缓存，撤回时直接调用 bot.get_msg 获取原文；
    拿到原文则一并还原，拿不到则只发提示。
    """
    gid = str(event.group_id)
    # 仅在白名单群生效
    if gid not in recall_whitelist:
        return

    # bot 自己的消息被撤回、或 bot 自己执行的撤回，都不处理，避免循环 / 自激
    if event.user_id == event.self_id:
        return
    if event.operator_id == event.self_id:
        return

    # 基础提示：at 撤回操作者 + 提示阅读群规（无论能否拿到原文都会发）
    base_msg = MessageSegment.at(event.operator_id) + ' 撤回了一条消息，请仔细阅读群规'

    # 尝试获取被撤回消息的原文（不额外维护消息缓存）
    raw_message = ''
    try:
        repo = await bot.get_msg(message_id=event.message_id)
        raw_message = repo.get('message', '') or ''
    except BaseException as e:
        logger.debug(f'[复读姬] 获取撤回消息原文失败: {e}')

    # 拿到原文则附上原文，拿不到则只发提示
    if raw_message:
        msg = base_msg + '\n\n撤回内容：\n' + Message(raw_message)
        log_tail = '已还原原文'
    else:
        msg = base_msg
        log_tail = '未取到原文，仅发提示'

    try:
        await bot.send_group_msg(group_id=event.group_id, message=msg)
        logger.info(
            f'[复读姬] 群 {gid} 检测到撤回 mid={event.message_id}，已 at {event.operator_id}，{log_tail}'
        )
    except BaseException as e:
        logger.debug(f'[复读姬] 发送撤回还原消息失败: {e}')
