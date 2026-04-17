import asyncio
import random
import re
import time
import os
from typing import Awaitable, List, Dict, Callable, Optional, Set, Tuple, Type
from nonebot import on_command, on_message, on_notice
from .logger import logger
from nonebot.params import CommandArg
from nonebot.matcher import Matcher
from nonebot.adapters import Bot, Event
from nonebot.adapters.onebot.v11 import Message, MessageEvent, PrivateMessageEvent, GroupMessageEvent, MessageSegment, GroupIncreaseNoticeEvent
from ATRI.config import BotSelfConfig

from .config import *
from .utils import *
from .chat import Chat
from .persistent_data_manager import PersistentDataManager
from .chat_manager import ChatManager
from .openai_func import TextGenerator
from .command_func import cmd

try:
    from .text_to_image import md_to_img, text_to_img
except ImportError:
    logger.warning('未安装 nonebot_plugin_htmlrender 插件，无法使用 text_to_img')
    config.ENABLE_MSG_TO_IMG = False
    config.ENABLE_COMMAND_TO_IMG = False

permission_check_func:Callable[[Matcher, Event, Bot, Optional[str], str], Awaitable[Tuple[bool,Optional[str]]]]
is_progress:bool = False

msg_sent_set:Set[str] = set() # bot 自己发送的消息

"""消息发送钩子，用于记录自己发送的消息(默认不开启，只有在用户自定义了message_sent事件之后message_sent事件才会被发送到 on_message 回调)"""
# @Bot.on_called_api
async def handle_group_message_sent(bot: Bot, exception: Optional[Exception], api: str, data: Dict[str, Any], result: Any):
    global msg_sent_set
    if result and (api in ['send_msg', 'send_group_msg', 'send_private_msg']):
        msg_id = result.get('message_id', None)
        if msg_id:
            msg_sent_set.add(f"{bot.self_id}_{msg_id}")

""" ======== 注册消息响应器 ======== """
# 注册qq消息响应器 收到任意消息时触发
matcher:Type[Matcher] = on_message(priority=config.NG_MSG_PRIORITY, block=config.NG_BLOCK_OTHERS)
@matcher.handle()
async def handler(matcher_:Matcher, event: MessageEvent, bot:Bot) -> None:
    global msg_sent_set
    if event.post_type == 'message_sent': # 通过bot.send发送的消息不处理
        msg_key = f"{bot.self_id}_{event.message_id}"
        if msg_key in msg_sent_set:
            msg_sent_set.remove(msg_key)
            return
        
    if len(msg_sent_set) > 10:
        if config.DEBUG_LEVEL > 0: logger.warning(f"累积的待处理的自己发送消息数量为 {len(msg_sent_set)}, 请检查逻辑是否有错误")
        msg_sent_set.clear()
    
    # 处理消息前先检查权限
    (permit_success, _) = await permission_check_func(matcher_, event, bot, None, 'message')
    if not permit_success:
        return
    
    # 判断用户账号是否被屏蔽
    if event.get_user_id() in config.FORBIDDEN_USERS:
        if config.DEBUG_LEVEL > 0: logger.info(f"用户 {event.get_user_id()} 被屏蔽，拒绝处理消息")
        return
    # 判断群是否被屏蔽
    if isinstance(event, GroupMessageEvent) and str(event.group_id) in config.FORBIDDEN_GROUPS:
        if config.DEBUG_LEVEL > 0: logger.info(f"群 {event.group_id} 被屏蔽，拒绝处理消息")
        return

    sender_name = await get_user_name(event=event, bot=bot, user_id=event.user_id) or '未知'
    
    resTmplate = (  # 测试用，获取消息的相关信息
        f"收到消息: {event.get_message()}"
        f"\n消息名称: {event.get_event_name()}"
        f"\n消息描述: {event.get_event_description()}"
        f"\n消息来源: {event.get_session_id()}"
        f"\n消息文本: {event.get_plaintext()}"
        f"\n消息主体: {event.get_user_id()}"
        f"\n消息内容: {event.get_message()}"
        f"\n发送者: {sender_name}"
        f"\n是否to-me: {event.is_tome()}"
        # f"\nJSON: {event.json()}"
    )
    if config.DEBUG_LEVEL > 1: logger.info(resTmplate)

    has_image = any(seg.type == "image" for seg in event.message)
    # 如果是忽略前缀 或者消息为空且没有图片，则跳过处理
    if event.get_plaintext().strip().startswith(config.IGNORE_PREFIX) or (not event.get_plaintext() and not has_image):
        if config.DEBUG_LEVEL > 1: logger.info("忽略前缀或消息为空，跳过处理...") # 纯图片消息也会被判定为空消息
        return

    # 判断群聊/私聊
    if isinstance(event, GroupMessageEvent):
        chat_key = 'group_' + event.get_session_id().split("_")[1]
        chat_type = 'group'
    elif isinstance(event, PrivateMessageEvent):
        chat_key = 'private_' + event.get_user_id()
        chat_type = 'private'
    else:
        if config.DEBUG_LEVEL > 0: logger.info("未知消息来源: " + event.get_session_id())
        return
    
    chat_text, wake_up, image_urls = await gen_chat_payload(event=event, bot=bot)

    # 进行消息响应
    await do_msg_response(
        event.get_user_id(),
        chat_text,
        event.is_tome() or wake_up,
        matcher,
        chat_type,
        chat_key,
        sender_name,
        bot=bot,
        image_urls=image_urls,
    )

""" ======== 注册通知响应器 ======== """
# 欢迎新成员通知响应器
welcome:Type[Matcher] = on_notice(priority=20, block=False)
@welcome.handle()  # 监听 welcom
async def _(matcher_:Matcher, event: GroupIncreaseNoticeEvent, bot:Bot):  # event: GroupIncreaseNoticeEvent  群成员增加事件
    if config.DEBUG_LEVEL > 0: logger.info(f"收到通知: {event}")

    if not config.REPLY_ON_WELCOME:  # 如果不回复欢迎消息，则跳过处理
        return
    
    # 处理通知前先检查权限
    (permit_success, _) = await permission_check_func(matcher_, event, bot,None,'notice')
    if not permit_success:
        return

    if isinstance(event, GroupIncreaseNoticeEvent): # 群成员增加通知
        chat_key = 'group_' + event.get_session_id().split("_")[1]
        chat_type = 'group'
    else:
        if config.DEBUG_LEVEL > 0: logger.info(f"未知通知来源: {event.get_session_id()} 跳过处理...")
        return

    resTmplate = (  # 测试用，获取消息的相关信息
        f"会话: {chat_key}"
        f"\n通知来源: {event.get_user_id()}"
        f"\n是否to-me: {event.is_tome()}"
        f"\nDict: {event.dict()}"
        f"\nJSON: {event.json()}"
    )
    if config.DEBUG_LEVEL > 0: logger.info(resTmplate)

    user_name = await get_user_name(event=event, bot=bot, user_id=int(event.get_user_id())) or f'qq:{event.get_user_id()}'

    # 进行消息响应
    await do_msg_response(
        event.get_user_id(),
        f'{user_name} has joined the group, welcome!',
        event.is_tome(),
        welcome,
        chat_type,
        chat_key,
        '[System]',
        True,
        bot=bot,
    )

""" ======== 注册指令响应器 ======== """
# QQ:人格设定指令 用于设定人格的相关参数
identity:Type[Matcher] = on_command("identity", aliases={"人格设定", "人格", "rg"}, rule=to_me(), priority=config.NG_MSG_PRIORITY - 1, block=True)
@identity.handle()
async def _(matcher_:Matcher, event: MessageEvent, bot:Bot, arg: Message = CommandArg()):
    global is_progress  # 是否产生编辑进度
    is_progress = False
    # 判断是否是禁止使用的用户
    if event.get_user_id() in config.FORBIDDEN_USERS:
        await identity.finish(f"您的账号({event.get_user_id()})已被禁用，请联系管理员。")

    # 判断群聊/私聊
    if isinstance(event, GroupMessageEvent):
        chat_key = 'group_' + event.get_session_id().split("_")[1]
    elif isinstance(event, PrivateMessageEvent):
        chat_key = 'private_' + event.get_user_id()
    else:
        if config.DEBUG_LEVEL > 0: logger.info("未知消息来源: " + event.get_session_id())
        return

    chat:Chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)
    chat_presets_dict = chat.chat_data.preset_datas

    raw_cmd:str = arg.extract_plain_text()
    if config.DEBUG_LEVEL > 0: logger.info(f"接收到指令: {raw_cmd} | 来源: {chat_key}")
    
    '\n'.join([f'  -> {k + " (当前)" if k == chat.preset_key else k}' for k in chat_presets_dict.keys()])

    # 执行命令前先检查权限
    (permit_success, permit_msg) = await permission_check_func(matcher_, event,bot,raw_cmd,'cmd')
    if not permit_success:
        await identity.finish(permit_msg if permit_msg else "对不起！你没有权限进行此操作 ＞﹏＜")

    # 执行命令 *取消注释下列行以启用新的命令执行器*
    res = cmd.execute(
        chat=chat,
        command='rg '+ raw_cmd,
        chat_presets_dict=chat_presets_dict,
    )

    if res:
        if res.get('msg'):     # 如果有返回消息则发送
            if config.ENABLE_COMMAND_TO_IMG:
                img = await text_to_img(res.get('msg')) # type: ignore
                await identity.send(MessageSegment.image(img))
            else:
                await identity.send(str(res.get('msg')))
        elif res.get('error'):
            await identity.finish(f"执行命令时出现错误: {res.get('error')}")  # 如果有返回错误则发送s

    else:
        await identity.finish("输入的命令好像有点问题呢... 请检查下再试试吧！ ╮(>_<)╭")

    if res.get('is_progress'): # 如果有编辑进度，进行数据保存
        # 更新所有全局预设到会话预设中
        if config.DEBUG_LEVEL > 0: logger.info(f"用户: {event.get_user_id()} 进行了人格预设编辑: {cmd}")
        PersistentDataManager.instance.save_to_file()  # 保存数据
    return


""" ======== 消息响应方法 ======== """
def _strip_markdown(text: str) -> str:
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"(^|\n)\s{0,3}#{1,6}\s*", r"\1", text)
    text = re.sub(r"(^|\n)\s*[-*+]\s+", r"\1", text)
    text = re.sub(r"(^|\n)\s*\d+\.\s+", r"\1", text)
    text = text.replace("**", "").replace("__", "").replace("~~", "")
    return text


def _normalize_reply_segment(text: str) -> str:
    text = _strip_markdown(text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip().strip("*';；").rstrip("。").strip()


async def do_msg_response(trigger_userid:str, trigger_text:str, is_tome:bool, matcher: Type[Matcher], chat_type: str, chat_key: str, sender_name: Optional[str] = None, wake_up: bool = False, loop_times=0, loop_data={}, bot:Bot = None, image_urls: Optional[List[str]] = None): # type: ignore
    """消息响应方法"""

    sender_name = sender_name or 'anonymous'
    chat:Chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)

    # 判断对话是否被禁用
    if not chat.is_enable:
        if config.DEBUG_LEVEL > 1: logger.info("对话已被禁用，跳过处理...")
        return

    # 检测是否包含违禁词
    for w in config.WORD_FOR_FORBIDDEN:
        if str(w).lower() in trigger_text.lower():
            if config.DEBUG_LEVEL > 0: logger.info(f"检测到违禁词 {w}，拒绝处理...")
            return

    # 唤醒词检测（支持当前激活角色名，仅在句首出现时无条件唤醒）
    text_head = trigger_text.lower().lstrip()
    wake_prefix = False
    if chat.preset_key.lower() and text_head.startswith(chat.preset_key.lower()):
        wake_prefix = True
    for w in config.WORD_FOR_WAKE_UP:
        if str(w).lower() and text_head.startswith(str(w).lower()):
            wake_prefix = True
            break
    if wake_prefix:
        wake_up = True

    # 随机回复判断（唤醒词不在句首时，也通过随机概率触发）
    if not wake_up and random.random() < config.RANDOM_CHAT_PROBABILITY:
        wake_up = True

    # 其它人格唤醒判断
    if chat.preset_key.lower() not in trigger_text.lower() and chat.enable_auto_switch_identity:
        for preset_key in chat.preset_keys:
            if preset_key.lower() in trigger_text.lower():
                chat.change_presettings(preset_key)
                logger.info(f"检测到 {preset_key} 的唤醒词，切换到 {preset_key} 的人格")
                if chat_type != 'server':
                    await matcher.send(f'[NG] 已切换到 {preset_key} (￣▽￣)-ok !')
                wake_up = True
                break

    current_preset_key = chat.preset_key

    # 判断是否需要回复
    if (    # 如果不是 bot 相关的信息，则直接返回
        wake_up or \
        (random.random() < config.REPLY_ON_NAME_MENTION_PROBABILITY and (any(n.lower() in trigger_text.lower() for n in list(BotSelfConfig.nickname) + [chat.preset_key]))) or \
        (config.REPLY_ON_AT and is_tome and '全体成员' not in trigger_text.lower())
    ):
        # 更新全局对话历史记录
        # chat.update_chat_history_row(sender=sender_name, msg=trigger_text, require_summary=True)
        await chat.update_chat_history_row(sender=sender_name,
                                    msg=f"@{chat.preset_key} {trigger_text}" if is_tome and chat_type=='group' else trigger_text,
                                    require_summary=False, record_time=True, images=image_urls)    # 只有在需要回复时才记录时间，用于节流
        logger.info("符合 bot 发言条件，进行回复...")
    else:
        if config.CHAT_ENABLE_RECORD_ORTHER:
            await chat.update_chat_history_row(sender=sender_name, msg=trigger_text, require_summary=False, record_time=False, images=image_urls)
            if config.DEBUG_LEVEL > 1: logger.info("不是 bot 相关的信息，记录但不进行回复")
        else:
            if config.DEBUG_LEVEL > 1: logger.info("不是 bot 相关的信息，不进行回复")
        return
    
    wake_up = False # 进入对话流程，重置唤醒状态

    # 记录对用户的对话信息
    await chat.update_chat_history_row_for_user(sender=sender_name, msg=trigger_text, userid=trigger_userid, username=sender_name, require_summary=False)

    if chat.preset_key != current_preset_key:
        if config.DEBUG_LEVEL > 0: logger.warning(f'等待OpenAI请求返回的过程中人格预设由[{current_preset_key}]切换为[{chat.preset_key}],当前消息不再继续响应.1')
        return
    
    # 节流判断 接收到消息后等待一段时间，如果在这段时间内再次收到消息，则跳过响应处理
    # 效果表现为：如果在一段时间内连续收到消息，则只响应最后一条消息
    last_recv_time = chat.last_msg_time
    await asyncio.sleep(config.REPLY_THROTTLE_TIME)
    if last_recv_time != chat.last_msg_time: # 如果最后一条消息时间不一致，说明在节流时间内收到了新消息，跳过处理
        if config.DEBUG_LEVEL > 0: logger.info('节流时间内收到新消息，跳过处理...')
        return
    
    # 主动聊天参与逻辑 *待定方案
    # 达到一定兴趣阈值后，开始进行一次启动发言准备 收集特定条数的对话历史作为发言参考
    # 启动发言后，一段时间内兴趣值逐渐下降，如果随后被呼叫，则兴趣值提升
    # 监测对话历史中是否有足够的话题参与度，如果有，则继续提高话题参与度，否则，降低话题参与度
    # 兴趣值影响发言频率，兴趣值越高，发言频率越高
    # 如果监测到对话记录中有不满情绪(如: 闭嘴、滚、不理你、安静等)，则大幅降低兴趣值并且降低发言频率，同时进入一段时间的沉默期(0-120分钟)
    # 沉默期中降低响应"提及"的概率，沉默期中被直接at，则恢复一定兴趣值提升兴趣值并取消沉默期
    # 兴趣值会影响回复的速度，兴趣值越高，回复速度越快
    # 发言概率贡献比例 = (随机值: 10% + 话题参与度: 50% + 兴趣值: 40%) * 发言几率基数(0.01~1.0)

    sta_time:float = time.time()

    # 生成对话 prompt 模板
    prompt_template = chat.get_chat_prompt_template(userid=trigger_userid, chat_type=chat_type)
    tg = TextGenerator.instance
    req_tokens = tg.cal_token_count(str(prompt_template))
    logger.info(f"生成 prompt 完成，请求 token 数: {req_tokens}")
    # 生成 log 输出用的 prompt 模板
    log_prompt_template = '\n'.join([f"[{m['role']}]\n{m['content']}\n" for m in prompt_template]) if isinstance(prompt_template, list) else prompt_template
    if config.DEBUG_LEVEL > 0:
        # logger.info("对话 prompt 模板: \n" + str(log_prompt_template))
        # 保存 prompt 模板到日志文件
        with open(os.path.join(config.NG_LOG_PATH, f"{chat_key}.{time.strftime('%Y-%m-%d %H-%M-%S')}.prompt.log"), 'a', encoding='utf-8') as f:
            f.write(f"prompt 模板: \n{log_prompt_template}\n")
        logger.info(f"对话 prompt 模板已保存到日志文件: {chat_key}.{time.strftime('%Y-%m-%d %H-%M-%S')}.prompt.log")

    chat.update_gen_time()  # 更新上次生成时间
    time_before_request = time.time()
    reply_prefix = f'<{chat.preset_key}> ' if (chat_type == 'server') else ''
    raw_parts: List[str] = []
    stream_buffer = ""
    sent_segments = 0
    last_send_time = 0.0

    async def send_segment(segment: str) -> None:
        nonlocal sent_segments, last_send_time
        reply_text = _normalize_reply_segment(segment)
        if not reply_text:
            return
        if re.match(r'^[^\u4e00-\u9fa5\w]{1}$', reply_text):
            return
        now = time.time()
        wait_time = max(0.0, float(config.REPLY_SEGMENT_INTERVAL) - (now - last_send_time))
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        await matcher.send(f"{reply_prefix}{reply_text}")
        sent_segments += 1
        last_send_time = time.time()

    async def on_text_chunk(chunk: str) -> None:
        nonlocal stream_buffer
        raw_parts.append(chunk)
        stream_buffer += chunk
        if not config.NG_ENABLE_MSG_SPLIT:
            return
        while sent_segments < max(1, config.REPLY_MAX_SEGMENTS) - 1 and "\n\n" in stream_buffer:
            segment, stream_buffer = stream_buffer.split("\n\n", 1)
            await send_segment(segment)

    async def on_reasoning_chunk(chunk: str) -> None:
        if config.LLM_SHOW_REASONING:
            await on_text_chunk(chunk)

    raw_res, success = await tg.stream_response(
        prompt=prompt_template,
        type='chat',
        custom={'bot_name': chat.preset_key, 'sender_name': sender_name},
        plugin_config=config,
        on_text=on_text_chunk,
        on_reasoning=on_reasoning_chunk,
    )  # 生成对话结果

    # 工具产生的图片（如pixiv搜图）始终发送，不受后续错误影响
    for tool_output in tg.consume_tool_outputs():
        if tool_output.get("type") == "image" and tool_output.get("url"):
            await matcher.send(MessageSegment.image(file=tool_output["url"]))

    if not success:
        logger.warning("生成对话结果失败，跳过处理...")
        if not raw_parts and raw_res:
            await send_segment(raw_res)
        return

    raw_res = raw_res or ''.join(raw_parts)

    if stream_buffer:
        await send_segment(stream_buffer)

    if time.time() - time_before_request > config.OPENAI_TIMEOUT:
        logger.warning(f'OpenAI响应超过timeout值[{config.OPENAI_TIMEOUT}]，停止处理')
        return

    if chat.preset_key != current_preset_key:
        if config.DEBUG_LEVEL > 0: logger.warning(f'等待OpenAI响应返回的过程中人格预设由[{current_preset_key}]切换为[{chat.preset_key}],当前消息不再继续处理.2')
        return

    cost_token = tg.cal_token_count(str(prompt_template) + raw_res)
    if config.DEBUG_LEVEL > 0: logger.info(f"token消耗: {cost_token} | 对话响应: \"{raw_res}\"")
    await chat.update_chat_history_row(sender=chat.preset_key, msg=raw_res, require_summary=True, record_time=False)
    chat.update_send_time()
    await chat.update_chat_history_row_for_user(sender=chat.preset_key, msg=raw_res, userid=trigger_userid, username=sender_name, require_summary=True)
    PersistentDataManager.instance.save_to_file()
    if config.DEBUG_LEVEL > 0: logger.info(f"对话响应完成 | 耗时: {time.time() - sta_time}s")
    return

