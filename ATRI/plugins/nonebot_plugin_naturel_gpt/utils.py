import hashlib
from typing import Any, Dict, List, Optional, Tuple, Union
import asyncio
import requests
import aiohttp

from nonebot.matcher import Matcher
from nonebot.adapters import Bot
from nonebot.adapters import Event
from nonebot.rule import Rule
from nonebot.permission import SUPERUSER
from nonebot.adapters.onebot.v11.exception import ActionFailed, ApiNotAvailable, NetworkError
from nonebot.adapters.onebot.v11.permission import GROUP_ADMIN, GROUP_OWNER
from nonebot.adapters.onebot.v11 import Message, MessageEvent, PrivateMessageEvent, GroupMessageEvent, MessageSegment, GroupIncreaseNoticeEvent

from .config import config
from .logger import logger
try:
    import ujson as json
except ImportError:
    import json

def to_me():
    if config.NG_TO_ME:
        from nonebot.rule import to_me

        return to_me()

    async def _to_me() -> bool:
        return True

    return Rule(_to_me)

async def default_permission_check_func(matcher:Matcher, event: Event, bot:Bot, cmd:Optional[str], type:str = 'cmd') -> Tuple[bool, Optional[str]]:
    """默认权限检查函数"""
    if not cmd: # 非命令调用
        return (True, None)
    
    if not hasattr(event, 'user_id'): # 获取不到 user_id 字段默认返回成功
        return (True, None)
    else:
        user_id = str(getattr(event, 'user_id'))

    if user_id == bot.self_id: # bot 在控制自己，永远有权限
        return (True, None)
    
    cmd_list = [c.strip() for c in cmd.split(' ') if c.strip()]
    if(len(cmd_list) == 0): # rg
        return (True, None)
    
    is_super_user = user_id in config.ADMIN_USERID or await (SUPERUSER)(bot, event)
    is_admin = is_super_user or isinstance(event, PrivateMessageEvent) or await (GROUP_ADMIN | GROUP_OWNER)(bot, event) # 超级管理员，私聊，群主，群管理，均视为admin

    common_cmd = ['', '查询', 'query', '设定', 'set', '更新', 'update', 'edit', '添加', 'new', '开启', 'on', '关闭', 'off', '重置', 'reset']
    super_cmd = ['admin', '删除', 'del', 'delete', '锁定', 'lock', '解锁', 'unlock', '扩展', 'ext',  'debug', '会话', 'chats', '记忆', 'memory', 'get', 'upload', 'ph']
    
    cmd_0 = cmd_list[0]
    if cmd_0 in super_cmd or '-global' in cmd_list: # 超级命令或者命令中包含 `-global` 选项需要超级管理员权限
        return (is_super_user, None if is_super_user else '权限不足，只有超级管理员才允许使用此指令')
    elif cmd_0 in common_cmd:
        return (is_admin, None if is_admin else '权限不足，只有管理员才允许使用此指令')
    else:
        return (True, None)
    
async def gen_chat_text(event: MessageEvent, bot:Bot) -> Tuple[str, bool]:
    """生成合适的会话消息内容(eg. 将cq at 解析为真实的名字)"""
    if not isinstance(event, GroupMessageEvent):
        return event.get_plaintext(), False
    else:
        wake_up = False
        msg = ''
        for seg in event.message:
            if seg.is_text():
                msg += seg.data.get('text', '')
            elif seg.type == 'at':
                qq = seg.data.get('qq', None)
                if qq:
                    if qq == 'all':
                        msg += '@全体成员'
                        wake_up = True
                    else:
                        user_name = await get_user_name(event=event, bot=bot,user_id=int(qq))
                        if user_name:
                            msg += f'@{user_name}' # 保持给bot看到的内容与真实用户看到的一致
        return msg, wake_up


def _is_supported_image_url(url: str) -> bool:
    return bool(url) and str(url).strip().startswith(("http://", "https://", "data:image/", "file:///"))


""" ======== 合并转发（forward）消息解析 ======== """

FORWARD_MAX_MESSAGES = 20       # 单条合并转发最多读取的消息条数
FORWARD_MAX_IMAGES = 4          # 单条合并转发最多带出的图片张数
FORWARD_MSG_MAX_CHARS = 150     # 单条消息文本截断长度
FORWARD_TOTAL_MAX_CHARS = 1200  # 整条合并转发的文本总长上限


def _forward_node_payload(node: Any) -> Dict[str, Any]:
    """把各家实现（go-cqhttp / NapCat / Lagrange）的 node 统一成内容 dict。"""
    if not isinstance(node, dict):
        return {}
    if node.get("type") == "node" and isinstance(node.get("data"), dict):
        return node["data"]
    return node


def _forward_node_segments(node: Any) -> List[Any]:
    """取出 node 承载的消息段列表（message / content 两种键名都兼容）。"""
    data = _forward_node_payload(node)
    for key in ("message", "content"):
        value = data.get(key)
        if isinstance(value, str):
            # 部分协议端把 content 给成 CQ 码串，交给适配器解析成消息段
            if "[CQ:" in value:
                try:
                    return list(Message(value))
                except Exception:
                    pass
            return [{"type": "text", "data": {"text": value}}]
        if isinstance(value, list):
            return value
    return []


def _forward_node_sender(node: Any) -> str:
    data = _forward_node_payload(node)
    sender = data.get("sender") if isinstance(data.get("sender"), dict) else {}
    for name in (
        data.get("nickname"),
        data.get("name"),
        sender.get("card"),
        sender.get("nickname"),
        sender.get("name"),
        data.get("user_id"),
        sender.get("user_id"),
    ):
        if name:
            return str(name)
    return "未知"


async def _fetch_forward_nodes(bot: Bot, forward_seg: Any) -> List[Any]:
    """拿到合并转发的 node 列表：优先用消息段内联的 content，否则调 get_forward_msg。"""
    data: Dict[str, Any] = {}
    if isinstance(forward_seg, MessageSegment):
        data = forward_seg.data or {}
    elif isinstance(forward_seg, dict):
        data = forward_seg.get("data") or forward_seg

    inline = data.get("content")
    if isinstance(inline, list) and inline:
        return inline

    message_id = data.get("id") or data.get("message_id")
    if not message_id:
        return []

    payload: Any = None
    last_error: Optional[BaseException] = None
    for args in ({"id": str(message_id)}, {"message_id": str(message_id)}):
        try:
            payload = await bot.call_api("get_forward_msg", **args)
            break
        except Exception as e:
            last_error = e
    else:
        logger.warning(f"合并转发内容获取失败 id={message_id}: {last_error!r}")
        return []

    if isinstance(payload, dict):
        for key in ("messages", "message"):
            nodes = payload.get(key)
            if isinstance(nodes, list):
                return nodes
    if isinstance(payload, list):
        return payload
    return []


async def extract_forward_content(
    bot: Bot,
    forward_seg: Any,
    *,
    image_offset: int = 0,
    max_messages: int = FORWARD_MAX_MESSAGES,
    max_images: int = FORWARD_MAX_IMAGES,
) -> Tuple[str, List[str]]:
    """解析一条合并转发，返回 (塞进 prompt 的文本, 图片 URL 列表)。

    文本里的 ``[图片N]`` 从 ``image_offset + 1`` 开始编号，与调用方把这些 URL 插到
    image_urls 的位置对齐。只取最多 max_messages 条消息 / max_images 张图片；
    任何解析失败都返回 ("", [])，不影响主流程。
    """
    try:
        nodes = await _fetch_forward_nodes(bot, forward_seg)
    except Exception as e:
        logger.warning(f"合并转发解析异常: {e!r}")
        return ("", [])
    if not nodes:
        return ("", [])

    total = len(nodes)
    lines: List[str] = []
    image_urls: List[str] = []
    total_chars = 0
    char_limited = False

    for node in nodes[:max_messages]:
        text_parts: List[str] = []
        for seg in _forward_node_segments(node):
            if isinstance(seg, str):
                text_parts.append(seg)
                continue
            if isinstance(seg, MessageSegment):
                seg_type, seg_data = seg.type, (seg.data or {})
            elif isinstance(seg, dict):
                seg_type, seg_data = seg.get("type"), (seg.get("data") or {})
            else:
                continue
            if seg_type == "text":
                text_parts.append(str(seg_data.get("text") or ""))
            elif seg_type == "at":
                qq = seg_data.get("qq")
                text_parts.append("@全体成员" if qq == "all" else f"@{qq}")
            elif seg_type == "image":
                if len(image_urls) >= max_images:
                    continue
                url = seg_data.get("url") or seg_data.get("file")
                if _is_supported_image_url(url):
                    image_urls.append(str(url))
                    text_parts.append(f"[图片{image_offset + len(image_urls)}]")
            elif seg_type == "forward":
                # 嵌套转发不递归展开，留个占位让模型知道这里还有内容
                text_parts.append("[嵌套合并转发]")

        text = "".join(text_parts).strip()
        if len(text) > FORWARD_MSG_MAX_CHARS:
            text = text[:FORWARD_MSG_MAX_CHARS] + "…"
        if not text:
            continue
        line = f"{_forward_node_sender(node)}: {text}"
        if total_chars + len(line) > FORWARD_TOTAL_MAX_CHARS:
            char_limited = True
            break
        total_chars += len(line)
        lines.append(line)

    if not lines and not image_urls:
        return ("", [])

    if char_limited:
        header = f"[合并转发消息 共{total}条，内容过长已截断]"
    elif total > max_messages:
        header = f"[合并转发消息 共{total}条，仅读取前{max_messages}条]"
    else:
        header = f"[合并转发消息 共{total}条]"
    body = "\n".join(lines) if lines else "（无文字内容）"
    if char_limited:
        body += "\n…（后续内容过长，已省略）"
    return (f"{header}\n{body}", image_urls)


async def get_reply_original_message(bot: Bot, event: MessageEvent) -> List[Any]:
    """reply 消息是空壳（协议端只给 message_id）时，取回原消息的消息段列表。"""
    reply = getattr(event, "reply", None)
    message_id = getattr(reply, "message_id", None) if reply is not None else None
    if not message_id:
        return []
    try:
        payload = await bot.call_api("get_msg", message_id=str(message_id))
    except Exception as e:
        logger.warning(f"被回复消息获取失败 id={message_id}: {e!r}")
        return []
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, list):
            return message
        if isinstance(message, str) and message.strip():
            return [{"type": "text", "data": {"text": message}}]
    return []


async def _extract_message_text_and_images(message: Message, event: MessageEvent, bot: Bot) -> Tuple[str, List[str]]:
    text = ""
    image_urls: List[str] = []
    image_index = 0
    for seg in message:
        if seg.is_text():
            text += seg.data.get("text", "")
        elif seg.type == "at":
            qq = seg.data.get("qq", None)
            if qq == "all":
                text += "@全体成员"
            elif qq:
                try:
                    user_name = await get_user_name(event=event, bot=bot, user_id=int(qq))
                    text += f"@{user_name or qq}"
                except Exception:
                    text += f"@{qq}"
        elif seg.type == "image":
            url = seg.data.get("url") or seg.data.get("file")
            if _is_supported_image_url(url):
                image_index += 1
                image_urls.append(str(url))
                text += f"[图片{image_index}]"
        elif seg.type == "forward":
            # 合并转发：展开成「昵称: 内容」多行文本，图片并入本条消息的图片列表
            forward_text, forward_images = await extract_forward_content(
                bot, seg, image_offset=len(image_urls)
            )
            if forward_text:
                text += ("\n" if text and not text.endswith("\n") else "") + forward_text
            image_urls.extend(forward_images)
    return text.strip(), image_urls


async def gen_chat_payload(event: MessageEvent, bot: Bot) -> Tuple[str, bool, List[str]]:
    """生成会话文本和图片 URL。保留 gen_chat_text 兼容旧调用。"""
    if not config.MULTIMODAL_ENABLE:
        chat_text, wake_up = await gen_chat_text(event, bot)
        return chat_text, wake_up, []
    
    # 使用 _extract_message_text_and_images 获取带有图片标记的文本
    chat_text, image_urls = await _extract_message_text_and_images(event.message, event, bot)
    
    # 检查是否需要唤醒
    wake_up = False
    if isinstance(event, GroupMessageEvent):
        for seg in event.message:
            if seg.type == 'at' and seg.data.get('qq') == 'all':
                wake_up = True
                break
    
    return chat_text, wake_up, image_urls
    

async def get_user_name(event: Union[MessageEvent, GroupIncreaseNoticeEvent], bot:Bot, user_id:int) -> Optional[str]:
    """获取QQ用户名，如果GROUP_CARD为Ture优先群名片"""
    if isinstance(event, GroupMessageEvent) and event.sub_type == 'anonymous' and event.anonymous: # 匿名消息
        return f'[匿名]{event.anonymous.name}'

    if (isinstance(event, GroupMessageEvent) or isinstance(event, GroupIncreaseNoticeEvent)):
        try:
            user_info = await bot.get_group_member_info(group_id=event.group_id, user_id=user_id, no_cache=False) or {}
        except (ActionFailed, ApiNotAvailable, NetworkError):
            user_info = {}
        user_name = user_info.get('nickname', None)
        if config.GROUP_CARD:
            user_name = user_info.get('card', None) or user_name
        if not user_name and isinstance(event, GroupMessageEvent) and event.user_id == user_id:
            sender = event.sender
            if sender:
                user_name = (sender.card if config.GROUP_CARD else None) or sender.nickname
        user_name = user_name or str(user_id)
    else:
        user_name = event.sender.nickname if event.sender else event.get_user_id()

    if user_name and config.NG_CHECK_USER_NAME_HYPHEN and ('-' in user_name): # 检查用户名中的连字符, 去掉第一个连字符之前的部分
        user_name = user_name.split('-', 1)[1].replace('-', '').strip()
        
    return user_name

async def translate(text:str, from_:str="auto", to_:str="en") -> str:
    """翻译（注意：依赖外部 API，可能不稳定）"""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://hf.space/embed/mikeee/gradio-gtr/+/api/predict",
                json={"data": [text, from_, to_]}
            )
            resp.raise_for_status()
            return resp.json()["data"][0]
    except Exception as e:
        raise Exception(f"翻译 API 请求失败: {e}")

async def async_fetch(
    url,
    method: str = "get",
    params: Optional[Dict] = None,
    data: Union[str, Dict] = "{}",
    headers: Optional[Dict] = None,
    proxy_server: str = "",
    timeout: int = 60,
) -> str:
    """发起异步请求"""

    if headers is None:
        headers = {}
    if params is None:
        params = {}
    if isinstance(data, dict):
        data = json.dumps(data)

    # 条件创建 session，避免资源泄漏
    if proxy_server:
        conn = aiohttp.TCPConnector(limit=10, verify_ssl=False)
        merged_headers = dict(headers)
        merged_headers.update({"Proxy-Switch-Ip": "yes", "Proxy-Server": proxy_server})
        session = aiohttp.ClientSession(connector=conn, headers=merged_headers)
    else:
        session = aiohttp.ClientSession(headers=headers)

    async with session:
        async with getattr(session, method)(
            url,
            params=params,
            data=data,
            timeout=timeout,
        ) as resp:
            return await resp.text()

def fetch(
    url,
    method: str = "get",
    params: Optional[Dict] = None,
    data: Union[str, Dict] = "{}",
    headers: Optional[Dict] = None,
    proxy_server: str = "",
    timeout: int = 60,
) -> str:
    """发起请求"""

    if headers is None:
        headers = {}
    if params is None:
        params = {}
    if isinstance(data, dict):
        data = json.dumps(data)

    if proxy_server:
        proxies = {
            "http": f"http://{proxy_server}",
            "https": f"https://{proxy_server}",
        }
    else:
        proxies = None

    resp = getattr(requests, method)(
        url,
        params=params,
        data=data,
        headers=headers,
        proxies=proxies,
        timeout=timeout,
    )
    return resp.text

def md5(s):
    m = hashlib.md5()
    m.update(s.encode("utf-8"))
    return m.hexdigest()
