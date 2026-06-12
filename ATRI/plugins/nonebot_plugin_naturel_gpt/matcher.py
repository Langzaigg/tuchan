import asyncio
import json
import random
import re
import time
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Awaitable, List, Dict, Callable, Optional, Set, Tuple, Type, Deque
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
from .openai_func import (
    TextGenerator,
    is_model_request_error_text,
    sanitize_draw_reply_text,
    sanitize_internal_control_text,
)
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


async def _wait_for_tool_completion(chat_key: str, timeout: float = 60.0) -> bool:
    """等待工具调用完成，返回是否成功等到"""
    tg = TextGenerator.instance
    if not tg.is_tool_calling(chat_key):
        return True
    
    event = _chat_tool_done_events.setdefault(chat_key, asyncio.Event())
    event.clear()
    
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except asyncio.TimeoutError:
        logger.warning(f"[并发控制] 群 {chat_key} 等待工具调用超时")
        return False


def _notify_tool_completion(chat_key: str) -> None:
    """通知工具调用完成"""
    event = _chat_tool_done_events.get(chat_key)
    if event:
        event.set()


def _setup_tool_done_callback(chat_key: str) -> None:
    """为TextGenerator设置工具调用完成回调"""
    tg = TextGenerator.instance
    def on_done():
        _notify_tool_completion(chat_key)
    tg.set_tool_done_callback(chat_key, on_done)

# ======== 并发控制 ========
# 不同会话并行；同一会话新请求取消旧请求，并把旧新问题合并后重新生成。
_chat_locks: Dict[str, asyncio.Lock] = {}                     # chat_key → 该群的锁
_chat_running_tasks: Dict[str, asyncio.Task] = {}          # chat_key → 当前运行中的 Task
_chat_active_inputs: Dict[str, Dict[str, Any]] = {}        # chat_key → 当前请求输入快照
_chat_tool_done_events: Dict[str, asyncio.Event] = {}      # chat_key → 工具调用完成事件
_recent_context_buffers: Dict[str, Deque[Dict[str, Any]]] = {}  # chat_key → 非触发消息缓冲

def _get_chat_lock(chat_key: str) -> asyncio.Lock:
    """获取指定会话的锁，不存在时自动创建"""
    if chat_key not in _chat_locks:
        _chat_locks[chat_key] = asyncio.Lock()
    return _chat_locks[chat_key]


def _context_buffer_limit() -> int:
    try:
        target_rounds = int(getattr(config, "CONTEXT_WINDOW_SIZE", 1) or 1)
    except (TypeError, ValueError):
        target_rounds = 1
    try:
        overflow_ratio = float(getattr(config, "CONTEXT_COMPRESS_THRESHOLD_RATIO", 0.5) or 0)
    except (TypeError, ValueError):
        overflow_ratio = 0.5
    target_rounds = max(1, target_rounds)
    overflow_rounds = int(target_rounds * max(0.0, overflow_ratio))
    return target_rounds + overflow_rounds


def _get_recent_context_buffer(chat_key: str) -> Deque[Dict[str, Any]]:
    max_buf = _context_buffer_limit()
    buf = _recent_context_buffers.get(chat_key)
    if buf is None:
        buf = deque(maxlen=max_buf)
        _recent_context_buffers[chat_key] = buf
    elif buf.maxlen != max_buf:
        buf = deque(list(buf)[-max_buf:], maxlen=max_buf)
        _recent_context_buffers[chat_key] = buf
    return buf


def _push_recent_context_buffer(
    chat_key: str,
    sender: str,
    text: str,
    images: Optional[List[str]] = None,
) -> None:
    """记录非触发群聊消息，等待下一条触发消息作为 context_only 注入。"""
    image_list = list(images or [])
    normalized_text = str(text or "").strip()
    if not normalized_text and not image_list:
        return
    if not normalized_text and image_list:
        normalized_text = " ".join(f"[图片{i + 1}]" for i in range(len(image_list)))

    buf = _get_recent_context_buffer(chat_key)
    buf.append({
        "sender": sender or "anonymous",
        "text": normalized_text,
        "images": image_list,
        "timestamp": time.time(),
    })
    if config.DEBUG_LEVEL > 0:
        logger.info(
            f"[上下文缓冲] 已缓存非触发消息 | 会话: {chat_key} | sender={sender} | "
            f"text_len={len(normalized_text)} | images={len(image_list)} | "
            f"buffer={len(buf)}/{buf.maxlen}"
        )


def _flush_recent_context_buffer(chat_key: str) -> Tuple[str, List[str]]:
    """清空入口层非触发缓冲，返回 (合并文本, 图片URL列表)。"""
    buf = _recent_context_buffers.pop(chat_key, None)
    if not buf:
        return "", []

    parts: List[str] = []
    images: List[str] = []
    img_counter = 0
    for item in buf:
        item_images = list(item.get("images") or [])
        text = str(item.get("text") or "").strip()
        if not text and item_images:
            text = " ".join(f"[图片{i + 1}]" for i in range(len(item_images)))

        for i in range(len(item_images)):
            img_counter += 1
            marker = f"[图片{i + 1}]"
            replacement = f"[图片{img_counter}]"
            if marker in text:
                text = text.replace(marker, replacement, 1)
            else:
                text = f"{text} {replacement}".strip()

        if not text and not item_images:
            continue
        ts = time.strftime('%H:%M', time.localtime(float(item.get("timestamp") or time.time())))
        parts.append(f"[{ts}] {item.get('sender') or 'anonymous'}: {text}")
        images.extend(item_images)

    return "\n".join(parts), images

"""消息发送钩子，用于记录自己发送的消息(默认不开启，只有在用户自定义了message_sent事件之后message_sent事件才会被发送到 on_message 回调)"""
# @Bot.on_called_api
async def handle_group_message_sent(bot: Bot, exception: Optional[Exception], api: str, data: Dict[str, Any], result: Any):
    global msg_sent_set
    if result and (api in ['send_msg', 'send_group_msg', 'send_private_msg']):
        msg_id = result.get('message_id', None)
        if msg_id:
            msg_sent_set.add(f"{bot.self_id}_{msg_id}")

""" ======== 注册消息响应器 ======== """


async def _make_polaroid_image(image_url: str, author: Optional[str], pid: Optional[int]) -> Optional[str]:
    """下载图片并加上拍立得风格装饰（白边 + 作者名），返回本地文件路径。失败返回 None。"""
    try:
        import httpx
        from PIL import Image
        from PIL.ImageDraw import ImageDraw as Draw
        from PIL.ImageFont import truetype as load_font
        import io
    except ImportError:
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(image_url)
            resp.raise_for_status()
            img_data = resp.content
    except Exception as e:
        logger.warning(f"拍立得图片下载失败: {e}")
        return None

    try:
        img = Image.open(io.BytesIO(img_data)).convert("RGBA")
        img_w, img_h = img.size

        # 拍立得风格：下方留白 60px 用于写作者信息，上方/左右留白 16px
        border = 16
        caption_h = 60
        canvas_w = img_w + border * 2
        canvas_h = img_h + border * 2 + caption_h

        # 纯白背景（拍立得相纸）
        canvas = Image.new("RGBA", (canvas_w, canvas_h), (255, 255, 255, 255))

        # 贴图：上方留白 16px，左右居中
        canvas.paste(img, (border, border), img)

        # 在底部 caption 区画作者名
        draw = Draw(canvas)
        caption_text = f"by {author}" if author else (f"PID:{pid}" if pid else "TuChan")
        for attempt_text in [caption_text, "TuChan"]:
            try:
                font = load_font(r"C:\Windows\Fonts\msyh.ttc", 16)
                text_bbox = draw.textbbox((0, 0), attempt_text, font=font)
                text_w = text_bbox[2] - text_bbox[0]
                text_x = max(border, (canvas_w - text_w) // 2)
                text_y = img_h + border + 20
                draw.text((text_x, text_y), attempt_text, font=font, fill=(120, 120, 120, 255))
                break
            except (UnicodeEncodeError, OSError, TypeError):
                continue

        # 保存到临时文件
        tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False, dir=tempfile.gettempdir())
        tmp_path = tmp.name
        tmp.close()
        canvas.convert("RGB").save(tmp_path, "PNG")
        return tmp_path

    except Exception as e:
        logger.warning(f"拍立得图片装饰失败: {e}")
        return None


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

    # 兜底：尝试消费 Anima 后台作画的 pending 结果（直接 OneBot 发送失败时的补救）
    # 只消费属于当前群的 pending，避免图片发到错误的会话
    try:
        from .llm_tool_plugins import anima_generate
        if isinstance(event, GroupMessageEvent):
            _fallback_chat_key = 'group_' + str(event.group_id)
        elif isinstance(event, PrivateMessageEvent):
            _fallback_chat_key = 'private_' + event.get_user_id()
        else:
            _fallback_chat_key = None
        if _fallback_chat_key:
            for pending in anima_generate.consume_pending_results_for(_fallback_chat_key):
                image_url = pending.get("url")
                if image_url:
                    try:
                        await matcher_.send(MessageSegment.image(file=image_url))
                    except Exception as e:
                        logger.warning(f"Anima 兜底图片发送失败 ({image_url}): {e}")
    except Exception:
        pass
    
    # 判断用户账号是否被屏蔽
    if event.get_user_id() in config.FORBIDDEN_USERS:
        if config.DEBUG_LEVEL > 0: logger.info(f"用户 {event.get_user_id()} 被屏蔽，拒绝处理消息")
        return
    # 判断群是否被屏蔽
    if isinstance(event, GroupMessageEvent) and str(event.group_id) in config.FORBIDDEN_GROUPS:
        if config.DEBUG_LEVEL > 0: logger.info(f"群 {event.group_id} 被屏蔽，拒绝处理消息")
        return

    sender_name = PersistentDataManager.instance.get_custom_nickname(event.get_user_id()) \
        or await get_user_name(event=event, bot=bot, user_id=event.user_id) or '未知'
    
    # 检查原始消息中是否有 @bot 的 at 消息段，防止被 _check_reply 删除后导致 is_tome() 返回 False
    if not event.to_me and hasattr(event, 'original_message'):
        for seg in event.original_message:
            if seg.type == "at" and str(seg.data.get("qq", "")) == str(bot.self_id):
                event.to_me = True
                break
    
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

    # 提取被回复消息的内容（文本 + 图片），作为上下文注入触发消息
    if getattr(event, 'reply', None) and event.reply.message:
        reply_text_parts: List[str] = []
        reply_image_urls: List[str] = []
        for seg in event.reply.message:
            if seg.is_text():
                reply_text_parts.append(seg.data.get("text", ""))
            elif seg.type == "image":
                url = seg.data.get("url") or seg.data.get("file")
                if url and str(url).strip().startswith(("http://", "https://", "data:image/", "file:///")):
                    reply_image_urls.append(str(url))
        reply_text = "".join(reply_text_parts).strip()

        # 获取被回复者名称
        reply_sender = event.reply.sender
        reply_sender_name: Optional[str] = None
        if reply_sender:
            if isinstance(event, GroupMessageEvent) and reply_sender.user_id:
                try:
                    reply_sender_name = await get_user_name(event=event, bot=bot, user_id=reply_sender.user_id)
                except Exception:
                    pass
            reply_sender_name = reply_sender_name or reply_sender.nickname or str(reply_sender.user_id or "未知")

        if reply_text or reply_image_urls:
            # 将被回复图片插入到 image_urls 前面，重新编号已有标记
            existing_count = len(image_urls) if image_urls else 0
            offset = len(reply_image_urls)
            if offset > 0 and existing_count > 0:
                for i in range(existing_count, 0, -1):
                    chat_text = chat_text.replace(f"[图片{i}]", f"[图片{i + offset}]")
                image_urls = reply_image_urls + list(image_urls or [])
            elif offset > 0:
                image_urls = list(reply_image_urls)

            # 构建被回复消息的图片标记
            reply_img_markers = " ".join(f"[图片{i + 1}]" for i in range(len(reply_image_urls))) if reply_image_urls else ""
            reply_content = reply_text
            if reply_img_markers:
                reply_content = f"{reply_content} {reply_img_markers}" if reply_content else reply_img_markers

            reply_prefix = f"[回复 {reply_sender_name} 的消息] {reply_content}"
            chat_text = f"{reply_prefix}\n{chat_text}" if chat_text else reply_prefix
            if config.DEBUG_LEVEL > 0:
                logger.info(f"已注入被回复消息上下文 | 发送者: {reply_sender_name} | 文本: {reply_text[:50]}{'...' if len(reply_text) > 50 else ''} | 图片: {len(reply_image_urls)}张")

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
        event=event,
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
        user_id=event.get_user_id(),
    )

    if res:
        if res.get('msg'):     # 如果有返回消息则发送
            if config.ENABLE_COMMAND_TO_IMG and not res.get('no_img'):
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


def _is_bad_request_error(text: Optional[str]) -> bool:
    if not text:
        return False
    lower_text = text.lower()
    return "http 400" in lower_text or "status code: 400" in lower_text or "bad request" in lower_text


def _is_probably_image_bad_request(text: Optional[str]) -> bool:
    if not text:
        return False
    lower_text = text.lower()
    image_markers = ("image_url", "image url", "invalid image", "download", "fetch", "url", "vision")
    return _is_bad_request_error(text) and any(marker in lower_text for marker in image_markers)


def _is_image_download_error(text: Optional[str]) -> bool:
    if not text:
        return False
    lower_text = text.lower()
    return (
        "failed to download" in lower_text
        or "cannot download image" in lower_text
        or "text` is not set" in lower_text  # Xiaomi proxy 图片处理失败
        or "download url data" in lower_text
    )


def _prompt_contains_images(prompt: List[Dict[str, Any]]) -> bool:
    for message in prompt:
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "image_url":
                    return True
    return False


def _count_prompt_text_and_images(prompt: List[Dict[str, Any]], tg: TextGenerator) -> Tuple[int, int]:
    text_parts: List[str] = []
    image_count = 0
    for message in prompt:
        content = message.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    text_parts.append(str(item.get("text") or ""))
                elif item.get("type") == "image_url":
                    image_count += 1
        elif content is not None:
            text_parts.append(str(content))
    return tg.cal_token_count("\n".join(text_parts)), image_count


def _snapshot_request_profile(chat: Chat) -> Dict[str, Any]:
    """固定本轮请求使用的 profile，避免多群并发切换 TextGenerator 单例状态。"""
    active_profile = chat.get_active_profile()
    profile = dict(config.OPENAI_PROFILES.get(active_profile, {}) or {})
    if profile:
        profile["api_keys"] = list(profile.get("api_keys", config.OPENAI_API_KEYS) or [""])
        profile["enable_stream"] = config.LLM_ENABLE_STREAM
        return profile
    return {
        "api_keys": list(config.OPENAI_API_KEYS or [""]),
        "base_url": config.OPENAI_BASE_URL or "",
        "proxy": config.OPENAI_PROXY_SERVER or None,
        "use_socket_proxy": False,
        "multimodal": True,
        "model": config.CHAT_MODEL,
        "model_mini": config.CHAT_MODEL_MINI,
        "max_tokens": config.REPLY_MAX_TOKENS,
        "temperature": config.CHAT_TEMPERATURE,
        "top_p": config.CHAT_TOP_P,
        "frequency_penalty": config.CHAT_FREQUENCY_PENALTY,
        "presence_penalty": config.CHAT_PRESENCE_PENALTY,
        "max_summary_tokens": config.CHAT_MAX_SUMMARY_TOKENS,
        "timeout": config.OPENAI_TIMEOUT,
        "enable_stream": config.LLM_ENABLE_STREAM,
    }


def _sanitize_prompt_for_log(prompt: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """清理 prompt 中的图片 base64，保留 URL 占位符，便于 debug 阅读"""
    result = []
    for msg in prompt:
        clean_msg = {"role": msg.get("role", "")}
        content = msg.get("content")
        if isinstance(content, list):
            clean_parts = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "text":
                    clean_parts.append({
                        "type": "text",
                        "text": sanitize_internal_control_text(str(item.get("text", "") or "")),
                    })
                elif item.get("type") == "image_url":
                    url = item.get("image_url", {}).get("url", "")
                    if url.startswith("data:"):
                        clean_parts.append({"type": "image_url", "image_url": {"url": "[base64图片，已省略]"}})
                    else:
                        clean_parts.append({"type": "image_url", "image_url": {"url": url}})
            clean_msg["content"] = clean_parts
        elif content is not None:
            clean_msg["content"] = sanitize_internal_control_text(str(content))
        if msg.get("tool_calls"):
            clean_msg["tool_calls"] = msg["tool_calls"]
        if msg.get("reasoning_content"):
            clean_msg["reasoning_content"] = msg["reasoning_content"]
        if msg.get("name"):
            clean_msg["name"] = msg["name"]
        if msg.get("tool_call_id"):
            clean_msg["tool_call_id"] = msg["tool_call_id"]
        result.append(clean_msg)
    return result


def _save_debug_log(chat_key: str, prompt: List[Dict[str, Any]], response: str,
                    tool_messages: List[Dict[str, Any]], reasoning: str,
                    cost_tokens: int, success: bool) -> None:
    """保存每个群最近一次 LLM 请求/响应到 JSON 文件"""
    log_dir = Path(config.NG_LOG_PATH)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_key = chat_key.replace("/", "_").replace("\\", "_")
    log_file = log_dir / f"{safe_key}.latest.json"
    data = {
        "chat_key": chat_key,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "success": success,
        "cost_tokens": cost_tokens,
        "prompt": _sanitize_prompt_for_log(prompt),
        "response": response,
    }
    if tool_messages:
        data["tool_messages"] = _sanitize_prompt_for_log(tool_messages)
        # 提取中间轮的 assistant 回复文本，便于查看工具调用阶段说了什么
        intermediate = [
            sanitize_internal_control_text(str(msg.get("content", "") or "")) for msg in tool_messages
            if msg.get("role") == "assistant" and msg.get("content")
        ]
        if intermediate:
            data["intermediate_responses"] = intermediate
    if reasoning:
        data["reasoning"] = reasoning
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 debug 日志失败: {e!r}")


def _save_error_log(chat_key: str, prompt: List[Dict[str, Any]], response: str,
                    tool_messages: List[Dict[str, Any]], reasoning: str,
                    cost_tokens: int) -> None:
    """保存失败请求的完整未脱敏 prompt，每个群只保留最新一份，用于排查 API 兼容性问题"""
    log_dir = Path(config.NG_LOG_PATH)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_key = chat_key.replace("/", "_").replace("\\", "_")
    log_file = log_dir / f"{safe_key}.error.json"
    data = {
        "chat_key": chat_key,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "cost_tokens": cost_tokens,
        "prompt": prompt,
        "response": response,
    }
    if tool_messages:
        data["tool_messages"] = tool_messages
    if reasoning:
        data["reasoning"] = reasoning
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存 error 日志失败: {e!r}")



def _estimate_cache_hit_tokens(prompt: List[Dict[str, Any]], tg: TextGenerator) -> int:
    """估算 prompt 前缀可命中的缓存 token 数。前 2 条 system 消息在同会话内稳定。"""
    if len(prompt) < 2:
        return 0
    # 取前 2 条 system 消息的 token 数作为稳定前缀
    prefix_text_parts: List[str] = []
    for msg in prompt[:2]:
        content = msg.get("content")
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    prefix_text_parts.append(str(item.get("text") or ""))
        elif content is not None:
            prefix_text_parts.append(str(content))
    return tg.cal_token_count("\n".join(prefix_text_parts)) if prefix_text_parts else 0


def _strip_think_tags(text: str) -> Tuple[str, str]:
    """提取 <think>...</think> 内容作为思考内容，返回 (清理后文本, 思考内容)"""
    import re
    thinks = re.findall(r'<think>([\s\S]*?)</think>', text)
    cleaned = re.sub(r'<think>[\s\S]*?</think>', '', text).strip()
    reasoning = "\n".join(thinks).strip() if thinks else ""
    return cleaned, reasoning


async def do_msg_response(
    trigger_userid: str,
    trigger_text: str,
    is_tome: bool,
    matcher: Type[Matcher],
    chat_type: str,
    chat_key: str,
    sender_name: Optional[str] = None,
    wake_up: bool = False,
    loop_times=0,
    loop_data=None,
    bot: Bot = None,
    image_urls: Optional[List[str]] = None,
    event: Optional[Event] = None,
): # type: ignore
    """消息响应方法"""
    loop_data = loop_data or {}

    # 设置工具调用完成回调
    _setup_tool_done_callback(chat_key)

    sender_name = sender_name or 'anonymous'
    chat:Chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)
    chat.apply_profile()  # 按群切换到对应的 OpenAI profile

    # ======== 并发控制：第一阶段（锁内）========
    # 读取旧输入、判断是否需要回复、合并输入、决定是否打断
    old_task_to_cancel = None
    should_reply = False
    content_is_labeled = False
    chat_lock = _get_chat_lock(chat_key)
    
    async with chat_lock:
        old_input = _chat_active_inputs.get(chat_key)
        incoming_text = trigger_text
        incoming_images = list(image_urls or [])

        # 判断对话是否被禁用
        if not chat.is_enable:
            if config.DEBUG_LEVEL > 1: logger.info("对话已被禁用，跳过处理...")
            _chat_active_inputs.pop(chat_key, None)
            return

        # 检测是否包含违禁词
        for w in config.WORD_FOR_FORBIDDEN:
            if str(w).lower() in trigger_text.lower():
                if config.DEBUG_LEVEL > 0: logger.info(f"检测到违禁词 {w}，拒绝处理...")
                _chat_active_inputs.pop(chat_key, None)
                return

        # 唤醒词检测（支持当前激活角色名，仅在句首出现时无条件唤醒）
        text_head = incoming_text.lower().lstrip()
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
        if chat.preset_key.lower() not in incoming_text.lower() and chat.enable_auto_switch_identity:
            for preset_key in chat.preset_keys:
                if preset_key.lower() in incoming_text.lower():
                    chat.change_presettings(preset_key)
                    logger.info(f"检测到 {preset_key} 的唤醒词，切换到 {preset_key} 的人格")
                    await matcher.send(f'[NG] 已切换到 {preset_key} (￣▽￣)-ok !')
                    wake_up = True
                    break

        # 判断是否需要回复
        has_name_mention = any(n.lower() in incoming_text.lower() for n in list(BotSelfConfig.nickname) + [chat.preset_key])
        name_mention_reply = random.random() < config.REPLY_ON_NAME_MENTION_PROBABILITY and has_name_mention
        at_reply = config.REPLY_ON_AT and is_tome and '全体成员' not in incoming_text.lower()
        should_reply = wake_up or name_mention_reply or at_reply
        reply_reasons: List[str] = []
        if wake_up:
            reply_reasons.append("wake")
        if name_mention_reply:
            reply_reasons.append("name")
        if at_reply:
            reply_reasons.append("at")

        if should_reply and chat_key in _chat_running_tasks and TextGenerator.instance.is_tool_calling(chat_key):
            # 旧请求正在工具调用时不能合并或删除旧 active input。
            # 旧请求会自己完成；这里只把本次触发消息排队到下一轮，避免形成孤立 assistant。
            logger.info(f"[并发控制] 群 {chat_key} 旧请求正在工具调用中，排队新触发输入")
            TextGenerator.instance.set_pending_merge_input(chat_key, {
                "text": incoming_text,
                "sender": sender_name,
                "trigger_userid": trigger_userid,
                "images": incoming_images,
                "matcher": matcher,
                "chat_type": chat_type,
                "is_tome": is_tome,
                "event": event,
                "bot": bot,
            })
            _chat_active_inputs.pop(chat_key, None)
            return

        if should_reply:
            if old_input:
                old_text = str(old_input.get("text") or "").strip()
                old_sender = str(old_input.get("sender") or "").strip()
                new_text = incoming_text.strip()
                if old_sender and old_sender != sender_name:
                    merged_parts = []
                    if old_text:
                        merged_parts.append(f"{old_sender}: {old_text}")
                    if new_text:
                        merged_parts.append(f"{sender_name}: {new_text}")
                    trigger_text = "\n\n".join(merged_parts)
                    content_is_labeled = True
                else:
                    merged_parts = [part for part in [old_text, new_text] if part]
                    trigger_text = "\n\n".join(merged_parts)
                image_urls = list(old_input.get("images") or []) + incoming_images
                if old_input.get("recorded"):
                    chat.remove_last_prompt_user_message()
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"[并发控制] 已合并旧新提问: {chat_key}")
            else:
                trigger_text = incoming_text
                image_urls = incoming_images
            # 只有确定需要回复的消息，才设置活跃输入、打断旧请求并注册自己
            _chat_active_inputs[chat_key] = {
                "text": trigger_text,
                "sender": sender_name,
                "images": list(image_urls or []),
                "recorded": False,
            }
            if chat_key in _chat_running_tasks:
                old_task = _chat_running_tasks[chat_key]
                # 标记需要打断旧任务
                old_task_to_cancel = old_task
                _chat_running_tasks[chat_key] = asyncio.current_task()
                logger.info(f"[并发控制] 将打断群 {chat_key} 的旧请求")
            else:
                _chat_running_tasks[chat_key] = asyncio.current_task()
        else:
            # 不需要回复的消息，推入临时缓冲区，不进入 prompt_messages
            if not old_input:
                _chat_active_inputs.pop(chat_key, None)
            _push_recent_context_buffer(
                chat_key=chat_key,
                sender=sender_name,
                text=incoming_text,
                images=incoming_images,
            )
            return

    # ======== 并发控制：第二阶段（锁外）========
    # 执行打断（在锁外避免死锁）
    if old_task_to_cancel:
        old_task_to_cancel.cancel()
        logger.info(f"[并发控制] 已打断群 {chat_key} 的旧请求")

    current_preset_key = chat.preset_key

    # 记录用户消息到 prompt_messages
    await chat.update_chat_history_row(sender=sender_name,
                                msg=trigger_text,
                                require_summary=False, record_time=True, images=image_urls,
                                record_for_prompt=True,
                                content_is_labeled=content_is_labeled,
                                user_id=trigger_userid)
    if chat_key in _chat_active_inputs:
        _chat_active_inputs[chat_key]["recorded"] = True

    wake_up = False # 进入对话流程，重置唤醒状态

    # 记录对用户的对话信息
    await chat.update_chat_history_row_for_user(sender=sender_name, msg=trigger_text, userid=trigger_userid, username=sender_name, require_summary=False)

    if chat.preset_key != current_preset_key:
        if config.DEBUG_LEVEL > 0: logger.warning(f'等待OpenAI请求返回的过程中人格预设由[{current_preset_key}]切换为[{chat.preset_key}],当前消息不再继续响应.1')
        return

    # 节流判断
    last_recv_time = chat.last_msg_time
    await asyncio.sleep(config.REPLY_THROTTLE_TIME)
    if last_recv_time != chat.last_msg_time:
        if config.DEBUG_LEVEL > 0: logger.info('节流时间内收到新消息，跳过处理...')
        return

    # 将缓冲区的非触发消息和中断回复合并为一条 context_only 消息注入。
    # 放在节流之后，确保触发消息附近新出现的非触发群聊也能进入本轮 prompt。
    legacy_context, legacy_images = chat.flush_context_buffer()
    recent_context, recent_images = _flush_recent_context_buffer(chat_key)
    buffered_context = "\n".join(part for part in [legacy_context, recent_context] if part)
    buffered_images = legacy_images + recent_images
    interrupted = chat.pop_interrupted_response()

    context_parts = []
    if buffered_context:
        context_parts.append(f"[群聊上下文-非触发消息]\n{buffered_context}")
    if interrupted:
        context_parts.append(f"[上一轮被中断的回复] {interrupted}")

    if context_parts:
        await chat.update_chat_history_row(
            sender="群聊上下文",
            msg="\n\n".join(context_parts),
            images=buffered_images,
            context_only=True,
        )
        if config.DEBUG_LEVEL > 0:
            logger.info(
                f"[上下文缓冲] 已注入 context_only: 文本段={len(context_parts)} | 图片={len(buffered_images)}"
            )
    # context 图片由 _apply_image_gating 注入到 context_only 消息中，不再合并到触发消息

    sta_time:float = time.time()

    # 检测画图关键词（用于 auto 模式判断是否注入画图知识）
    _DRAWING_KEYWORDS = ("画", "draw", "改图", "重画", "来一张", "整一张")
    _has_draw_request = any(kw in (trigger_text or "").lower() for kw in _DRAWING_KEYWORDS)

    # 提取触发消息中 @或昵称提到的用户 ID，用于附带其个人印象
    mentioned_userids: List[str] = []
    if event and isinstance(event, MessageEvent):
        # 从原始消息提取 @段（event.message 可能已被 _check_reply/_check_at_me 修改）
        _orig_msg = getattr(event, 'original_message', None) or event.message
        for seg in _orig_msg:
            if seg.type == "at":
                qq = str(seg.data.get("qq", ""))
                if qq and qq != "all" and qq != str(bot.self_id):
                    mentioned_userids.append(qq)
    # 从触发文本匹配已知用户的群昵称
    if trigger_text and chat.chat_preset.chat_impressions:
        _text_lower = trigger_text.lower()
        for uid, imp_data in chat.chat_preset.chat_impressions.items():
            if uid == trigger_userid or uid in mentioned_userids:
                continue
            nick = (imp_data.nickname or "").strip()
            if nick and len(nick) >= 2 and nick.lower() in _text_lower:
                mentioned_userids.append(uid)
    if mentioned_userids and config.DEBUG_LEVEL > 0:
        logger.info(f"触发消息提到的用户: {mentioned_userids}")

    # 生成对话 prompt 模板
    prompt_template = await chat.get_chat_prompt_template(userid=trigger_userid, chat_type=chat_type, has_draw_request=_has_draw_request, mentioned_userids=mentioned_userids or None)

    # 注册 Anima 发送上下文，供后台作画任务完成后直接通过 OneBot 发送图片
    from .llm_tool_plugins import anima_generate
    _anima_group_id = chat_key.split("_")[1] if chat_type == "group" else None
    _anima_user_id = chat_key.split("_")[1] if chat_type == "private" else None
    anima_generate.register_send_context(
        chat_key=chat_key,
        bot_id=bot.self_id,
        group_id=_anima_group_id,
        user_id=_anima_user_id,
    )

    tg = TextGenerator.instance
    tg._current_chat_key = chat_key  # 设置当前会话key供工具使用
    tg._current_trigger_userid = trigger_userid  # 设置当前用户id供工具使用
    request_profile = _snapshot_request_profile(chat)
    text_tokens, prompt_image_count = _count_prompt_text_and_images(prompt_template, tg)
    cache_hit_tokens = _estimate_cache_hit_tokens(prompt_template, tg)
    logger.info(
        f"触发回复 | 会话: {chat_key} | 预设: {chat.preset_key} | "
        f"原因: {','.join(reply_reasons) or 'unknown'} | "
        f"tokens: {text_tokens} + {prompt_image_count}图 | "
        f"缓存命中: ~{cache_hit_tokens} tokens"
    )
    def _content_to_log_str(content: Any) -> str:
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    elif item.get("type") == "image_url":
                        parts.append("[图片]")
            return "\n".join(parts) if parts else str(content)
        return str(content) if content is not None else ""
    log_prompt_template = '\n'.join([f"[{m['role']}]\n{_content_to_log_str(m['content'])}\n" for m in prompt_template]) if isinstance(prompt_template, list) else prompt_template
    if config.DEBUG_LEVEL > 0:
        with open(os.path.join(config.NG_LOG_PATH, f"{chat_key}.{time.strftime('%Y-%m-%d %H-%M-%S')}.prompt.log"), 'a', encoding='utf-8') as f:
            f.write(f"prompt 模板: \n{log_prompt_template}\n")
        logger.info(f"对话 prompt 模板已保存到日志文件: {chat_key}.{time.strftime('%Y-%m-%d %H-%M-%S')}.prompt.log")

    chat.update_gen_time()  # 更新上次生成时间
    time_before_request = time.time()
    reply_prefix = ''
    raw_parts: List[str] = []
    stream_buffer = ""
    sent_segments = 0
    last_send_time = 0.0
    _in_think = False          # 是否正在 <think> 块内
    _think_buffer = ""          # 当前思考块的累积内容
    _extracted_reasoning = ""   # 从 content 中提取的完整思考内容

    async def send_segment(segment: str) -> None:
        nonlocal sent_segments, last_send_time
        segment = sanitize_internal_control_text(segment)
        segment = sanitize_draw_reply_text(segment, allow_task_ids=True)
        reply_text = _normalize_reply_segment(segment)
        reply_text = sanitize_internal_control_text(reply_text)
        reply_text = sanitize_draw_reply_text(reply_text, allow_task_ids=True)
        reply_text, _ = _strip_think_tags(reply_text)
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
        nonlocal stream_buffer, _think_buffer, _in_think
        raw_parts.append(chunk)
        # 实时拦截 <think>...</think> 标签（Grok 等模型在 content 中返回思考内容）
        _think_buffer_local = _think_buffer
        remaining = chunk
        while remaining:
            if _in_think:
                end_idx = remaining.find("</think>")
                if end_idx >= 0:
                    _think_buffer_local += remaining[:end_idx]
                    remaining = remaining[end_idx + len("</think>"):]
                    _in_think = False
                    # 思考块结束，将内容存入 reasoning_content
                    nonlocal _extracted_reasoning
                    _extracted_reasoning += _think_buffer_local
                    _think_buffer_local = ""
                    _think_buffer = ""
                else:
                    _think_buffer_local += remaining
                    _think_buffer = _think_buffer_local
                    return  # 整个 chunk 都在思考块内，不输出
            else:
                start_idx = remaining.find("<think>")
                if start_idx >= 0:
                    # <think> 前的正常内容输出
                    before = remaining[:start_idx]
                    if before:
                        stream_buffer += before
                    _in_think = True
                    remaining = remaining[start_idx + len("<think>"):]
                else:
                    # 无 think 标签，正常输出
                    stream_buffer += remaining
                    remaining = ""
        if not config.NG_ENABLE_MSG_SPLIT:
            return
        while sent_segments < max(1, config.REPLY_MAX_SEGMENTS) - 1 and "\n\n" in stream_buffer:
            segment, stream_buffer = stream_buffer.split("\n\n", 1)
            await send_segment(segment)

    async def on_reasoning_chunk(chunk: str) -> None:
        if config.LLM_SHOW_REASONING:
            await on_text_chunk(chunk)

    try:
        # 生成对话结果（含图片 400 重试，最多 2 次）
        MAX_RETRIES = 2
        for _retry in range(1 + MAX_RETRIES):
            raw_res, success, tool_messages, reasoning_content = await tg.stream_response(
                prompt=prompt_template,
                type='chat',
                custom={'bot_name': chat.preset_key, 'sender_name': sender_name},
                plugin_config=config,
                request_profile=request_profile,
                on_text=on_text_chunk,
                on_reasoning=on_reasoning_chunk,
            )

            # 每次失败都保存完整的未脱敏 error log（即使后续会重试）
            if not success:
                failure_cost = tg.cal_token_count(str(prompt_template) + str(raw_res or ""))
                _save_error_log(chat_key, prompt_template, str(raw_res or ""), tool_messages, reasoning_content, failure_cost)

            # 成功或无图片可剥离时不再重试
            if success or not _prompt_contains_images(prompt_template):
                break
            # 仅在图片相关 400 错误时重试（非图片 400 如工具调用格式错误，剥离图片无意义）
            if not _is_image_download_error(raw_res):
                break
            if _retry >= MAX_RETRIES:
                logger.warning(f"已达到最大重试次数 ({MAX_RETRIES})，停止重试")
                break

            logger.warning(f"含图片上下文请求返回 400 (第 {_retry + 1} 次)，回退到无图片上下文重试...")
            chat.cleanup_after_bad_request(keep_history=5)
            PersistentDataManager.instance.save_to_file(must_save=True)
            raw_parts.clear()
            stream_buffer = ""
            sent_segments = 0
            prompt_template = await chat.get_chat_prompt_template(
                userid=trigger_userid,
                chat_type=chat_type,
                include_images=False,
                has_draw_request=_has_draw_request,
                mentioned_userids=mentioned_userids or None,
            )

        # 工具产生的图片（如pixiv搜图）始终发送，不受后续错误影响
        for tool_output in tg.consume_tool_outputs(chat_key):
            if tool_output.get("type") == "image" and tool_output.get("url"):
                image_url = tool_output["url"]
                author = tool_output.get("author")
                pid = tool_output.get("pid")
                gallery_url = tool_output.get("gallery_url")

                # 第一档：直接发原图
                try:
                    await matcher.send(MessageSegment.image(file=image_url))
                    continue
                except Exception as e:
                    logger.warning(f"图片直接发送失败 ({image_url}): {e}")

                # 第二档：拍立得装饰后重试
                polaroid_path = None
                try:
                    polaroid_path = await _make_polaroid_image(image_url, author, pid)
                    if polaroid_path:
                        await matcher.send(MessageSegment.image(file=polaroid_path))
                        continue
                except Exception as e2:
                    logger.warning(f"拍立得图片发送也失败: {e2}")
                finally:
                    if polaroid_path:
                        try:
                            Path(polaroid_path).unlink(missing_ok=True)
                        except Exception:
                            pass

                # 第三档：发 Pixiv 画廊链接
                if gallery_url:
                    await matcher.send(f"图片发送失败，Pixiv 画廊链接：{gallery_url}")
                else:
                    await matcher.send(f"[图片]({image_url})")

        # Anima 兜底：如果后台任务已完成但直接发送失败（例如 bot 实例不可用），
        # 结果会留在 pending 队列中，在此处尝试通过 matcher 发送。
        # 注意：正常情况下后台任务会自己通过 OneBot 发送，此处只处理兜底场景。
        from .llm_tool_plugins import anima_generate
        for pending in anima_generate.consume_pending_results_for(chat_key):
            image_url = pending.get("url")
            if image_url:
                try:
                    await matcher.send(MessageSegment.image(file=image_url))
                except Exception as e:
                    logger.warning(f"Anima 兜底图片发送也失败 ({image_url}): {e}")
                    await matcher.send(f"[图片]({image_url})")

        if not success:
            logger.warning("生成对话结果失败，跳过处理...")
            # 即使失败，如果有已生成的正常回复（可能包含工具调用结果），也要保存到历史；
            # 但大模型请求异常本身是内部错误，不允许作为 assistant 上下文保存。
            raw_res_for_save = ''.join(raw_parts).strip()
            if not raw_res_for_save and raw_res and not is_model_request_error_text(raw_res):
                raw_res_for_save = raw_res
            if raw_res_for_save:
                raw_res_for_save = sanitize_internal_control_text(raw_res_for_save)
                raw_res_for_save = sanitize_draw_reply_text(raw_res_for_save, allow_task_ids=True)
                raw_res_for_save, _ = _strip_think_tags(raw_res_for_save)
                if is_model_request_error_text(raw_res_for_save):
                    raw_res_for_save = ""
            if raw_res_for_save:
                await chat.update_chat_history_row(sender=chat.preset_key, msg=raw_res_for_save, require_summary=False, record_time=False, is_bot_reply=True)
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"[失败回复保存] 已保存 {len(raw_res_for_save)} 字的回复到历史")
            failure_response = raw_res_for_save or raw_res or ''.join(raw_parts)
            failure_cost = tg.cal_token_count(str(prompt_template) + str(failure_response or ""))
            _save_debug_log(
                chat_key,
                prompt_template,
                str(failure_response or ""),
                tool_messages,
                reasoning_content,
                failure_cost,
                success,
            )
            if _is_bad_request_error(raw_res):
                logger.warning("检测到 400 Bad Request，清理图片上下文和近期历史...")
                chat.cleanup_after_bad_request(keep_history=5)
                PersistentDataManager.instance.save_to_file(must_save=True)
                await matcher.send("[系统] 请求上下文异常，已清理近期上下文，请继续对话")
                return
            if raw_res and "token" in raw_res.lower():
                logger.warning("检测到 token 超限错误，清理近期历史...")
                chat.cleanup_after_bad_request(keep_history=5)
                PersistentDataManager.instance.save_to_file(must_save=True)
                await matcher.send("[系统] 对话历史过长已自动清理，请继续对话")
                return
            if not raw_parts and raw_res:
                if is_model_request_error_text(raw_res):
                    await matcher.send("[系统] 请求模型失败，已记录错误日志，请稍后重试")
                else:
                    await send_segment(raw_res)
            return

        raw_res = raw_res or ''.join(raw_parts)
        raw_res = sanitize_internal_control_text(raw_res)
        raw_res = sanitize_draw_reply_text(raw_res, allow_task_ids=True)
        # 合并流式拦截的思考内容 + 兜底正则提取
        if _extracted_reasoning and not reasoning_content:
            reasoning_content = _extracted_reasoning
        raw_res, think_reasoning = _strip_think_tags(raw_res)
        if think_reasoning and not reasoning_content:
            reasoning_content = think_reasoning

        if stream_buffer:
            await send_segment(stream_buffer)

        if chat.preset_key != current_preset_key:
            if config.DEBUG_LEVEL > 0: logger.warning(f'等待OpenAI响应返回的过程中人格预设由[{current_preset_key}]切换为[{chat.preset_key}],当前消息不再继续处理.2')
            return

        cost_token = tg.cal_token_count(str(prompt_template) + raw_res)
        if config.DEBUG_LEVEL > 0: logger.info(f"token消耗: {cost_token} | 对话响应: \"{raw_res}\"")

        # 保存 debug 日志（每个群最近一次请求/响应）
        _save_debug_log(chat_key, prompt_template, raw_res, tool_messages, reasoning_content, cost_token, success)
        
        # 保存工具调用消息到内存（不持久化）
        if tool_messages:
            tool_summary_target = await chat.save_tool_messages(tool_messages)
            # 模式3: 异步生成工具调用摘要（不阻塞响应）
            if config.TOOL_CONTEXT_MODE == 3 and tool_summary_target:
                asyncio.create_task(chat.generate_tool_call_summary(
                    tool_messages,
                    trigger_text=trigger_text,
                    target_msg=tool_summary_target,
                ))
        
        # 记录Bot回复，is_bot_reply=True表示同时更新精简窗口和全量窗口
        await chat.update_chat_history_row(sender=chat.preset_key, msg=raw_res, require_summary=True, record_time=False, is_bot_reply=True)
        chat.update_send_time()
        await chat.update_chat_history_row_for_user(sender=chat.preset_key, msg=raw_res, userid=trigger_userid, username=sender_name, require_summary=True)
        PersistentDataManager.instance.save_to_file()
        if config.DEBUG_LEVEL > 0: logger.info(f"对话响应完成 | 耗时: {time.time() - sta_time}s")
        
        # 检查是否有待合并的输入
        pending_input = tg.get_pending_merge_input(chat_key)
        if pending_input:
            logger.info(f"[并发控制] 群 {chat_key} 检测到待合并的输入，继续处理")
            await do_msg_response(
                trigger_userid=pending_input.get("trigger_userid", trigger_userid),
                trigger_text=pending_input.get("text", ""),
                is_tome=pending_input.get("is_tome", is_tome),
                matcher=pending_input.get("matcher", matcher),
                chat_type=pending_input.get("chat_type", chat_type),
                chat_key=chat_key,
                sender_name=pending_input.get("sender", sender_name),
                bot=pending_input.get("bot", bot),
                image_urls=pending_input.get("images"),
                event=pending_input.get("event"),
            )
        return
    except asyncio.CancelledError:
        try:
            partial = ''.join(raw_parts).strip()
        except NameError:
            partial = ""
        if partial:
            partial = sanitize_internal_control_text(partial)
            partial = sanitize_draw_reply_text(partial, allow_task_ids=True)
            # 剥离 <think> 标签，只保留实际回复内容
            partial = re.sub(r'<think>.*?</think>', '', partial, flags=re.DOTALL).strip()
        if partial:
            chat.set_interrupted_response(partial)
            logger.info(f"[并发控制] 群 {chat_key} 请求被中断，已保存 {len(partial)} 字的部分回复")
        raise
    finally:
        if _chat_running_tasks.get(chat_key) is asyncio.current_task():
            _chat_running_tasks.pop(chat_key, None)
            _chat_active_inputs.pop(chat_key, None)
        # 注销 Anima 发送上下文
        from .llm_tool_plugins import anima_generate
        anima_generate.unregister_send_context(chat_key)

