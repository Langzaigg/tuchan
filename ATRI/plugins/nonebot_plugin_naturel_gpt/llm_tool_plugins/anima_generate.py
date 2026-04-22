import asyncio
from typing import Any, Dict, List, Optional, Tuple

import httpx

from nonebot import logger, get_bot
from nonebot.adapters.onebot.v11 import MessageSegment

_comfyui_base_url: str = "http://127.0.0.1:8188"
_anima_schema_cache: Optional[Dict[str, Any]] = None
_anima_knowledge_cache: Optional[str] = None

# 内存级会话开关：只记录哪些 chat_key 启用了 Anima 画图
_chat_enabled: set = set()

# 发送上下文：asyncio.Task → {chat_key, bot_id, group_id, user_id}
# 由 matcher 在发起 LLM 请求前注册，后台任务完成后用于直接发送图片
# 使用 Task 作为 key 是因为：不同群的请求并行运行在不同 Task 中，
# 而 _execute_tool_calls 在当前 Task 中 await 执行，因此 run() 可以通过
# asyncio.current_task() 精确找到属于当前请求的上下文，避免并行请求间串扰。
_send_context: Dict[Any, Dict[str, Any]] = {}


def _get_url(path: str) -> str:
    return f"{_comfyui_base_url.rstrip('/')}{path}"


def set_base_url(url: str) -> None:
    global _comfyui_base_url
    _comfyui_base_url = url


def set_chat_enabled(chat_key: str, enabled: bool) -> None:
    if enabled:
        _chat_enabled.add(chat_key)
    else:
        _chat_enabled.discard(chat_key)


def is_chat_enabled(chat_key: str) -> bool:
    return chat_key in _chat_enabled


def any_chat_enabled() -> bool:
    return bool(_chat_enabled)


def register_send_context(chat_key: str, bot_id: str, group_id: Optional[str] = None, user_id: Optional[str] = None) -> None:
    """注册发送上下文，供后台任务完成后发送图片。由 matcher 在发起请求前调用。
    使用当前 asyncio Task 作为 key，这样并行请求间不会串扰。"""
    task = asyncio.current_task()
    _send_context[task] = {
        "chat_key": chat_key,
        "bot_id": bot_id,
        "group_id": group_id,
        "user_id": user_id,
    }


def unregister_send_context(chat_key: str) -> None:
    """注销发送上下文。由 matcher 在请求结束后调用。
    遍历找到匹配 chat_key 的条目删除（因为 key 是 Task 对象）。"""
    to_remove = None
    for task, ctx in _send_context.items():
        if ctx.get("chat_key") == chat_key:
            to_remove = task
            break
    if to_remove is not None:
        del _send_context[to_remove]


def health_check_sync() -> Tuple[bool, str]:
    """同步健康检查，用于指令处理（同步上下文）。"""
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(_get_url("/anima/health"))
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "ok":
                return True, ""
            return False, f"服务状态异常: {data}"
    except Exception as e:
        return False, str(e)


def fetch_schema_and_knowledge_sync() -> Tuple[bool, str]:
    """同步获取 schema 与 knowledge，用于指令处理（同步上下文）。"""
    global _anima_schema_cache, _anima_knowledge_cache
    try:
        with httpx.Client(timeout=15) as client:
            schema_resp = client.get(_get_url("/anima/schema"))
            schema_resp.raise_for_status()
            schema_data = schema_resp.json()

            knowledge_resp = client.get(_get_url("/anima/knowledge"))
            knowledge_resp.raise_for_status()
            knowledge_data = knowledge_resp.json()

        _anima_schema_cache = {
            "type": "function",
            "function": _enhance_schema(schema_data),
        }

        parts = []
        for k, v in knowledge_data.items():
            parts.append(f"## {k}\n{v}\n")
        parts.append(
            "## 角色扮演引导\n"
            "上述是你自己的绘画技巧。用户请你画画时，自然地用第一人称说你正在画什么，"
            "避免提及工具、系统、调用等词。"
        )
        _anima_knowledge_cache = "\n".join(parts)

        return True, ""
    except Exception as e:
        return False, str(e)


def get_schema() -> Optional[Dict[str, Any]]:
    return _anima_schema_cache


def get_knowledge() -> Optional[str]:
    return _anima_knowledge_cache


def clear_cache() -> None:
    global _anima_schema_cache, _anima_knowledge_cache
    _anima_schema_cache = None
    _anima_knowledge_cache = None


async def _request(path: str, method: str = "GET", json: Optional[Dict] = None) -> Any:
    url = _get_url(path)
    async with httpx.AsyncClient(timeout=300) as client:
        if method.upper() == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=json)
        resp.raise_for_status()
        return resp.json()


def _build_positive(args: Dict[str, Any]) -> str:
    """根据参数拼出人类可读的作画思路/提示词描述。"""
    parts = []
    if args.get("quality_meta_year_safe"):
        parts.append(f"质量与安全: {args['quality_meta_year_safe']}")
    if args.get("count"):
        parts.append(f"人数: {args['count']}")
    if args.get("character"):
        parts.append(f"角色: {args['character']}")
    if args.get("series"):
        parts.append(f"作品: {args['series']}")
    if args.get("artist"):
        parts.append(f"画师: {args['artist']}")
    if args.get("style"):
        parts.append(f"风格: {args['style']}")
    if args.get("appearance"):
        parts.append(f"外观: {args['appearance']}")
    if args.get("tags"):
        parts.append(f"标签: {args['tags']}")
    if args.get("environment"):
        parts.append(f"环境: {args['environment']}")
    if args.get("nltags"):
        parts.append(f"自然语言: {args['nltags']}")
    if args.get("aspect_ratio"):
        parts.append(f"比例: {args['aspect_ratio']}")
    return "；".join(parts)


def _enhance_schema(schema_data: Dict[str, Any]) -> Dict[str, Any]:
    """增强 schema，提升模型调用积极性，并引导其认为是自身在画画。"""
    data = dict(schema_data)
    original_desc = data.get("description", "")
    data["description"] = (
        "使用你自己的绘画能力创作插画。这是你自己的画师技能，不是外部工具。"
        + (f" 技术说明: {original_desc}" if original_desc else "")
    )
    return data


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """工具调用入口：立即返回作画思路，后台提交生成任务。"""
    base_url = getattr(config, "COMFYUI_BASE_URL", "http://127.0.0.1:8188")
    set_base_url(base_url)

    # 通过当前 asyncio Task 精确取出属于本请求的发送上下文快照
    # 传给后台任务，这样即使 matcher 的 finally 提前注销了上下文，
    # 后台任务仍可使用快照发送。即使不同群的请求并行也不会串扰。
    current_task = asyncio.current_task()
    send_ctx = dict(_send_context.get(current_task, {}))  # 浅拷贝

    positive_desc = _build_positive(args)
    # 返回给 LLM，引导其以第一人称描述自己在画画
    content = (
        f"你正在画一幅插画：{positive_desc}。"
        f"用第一人称向用户描述你的创作，不要提到工具或系统，只说你自己在画画。"
    )

    # 启动后台任务，传入上下文快照以便完成后发送
    _schedule_bg(_do_generate(args, config, send_ctx))

    return content, []


async def _do_generate(args: Dict[str, Any], config, send_ctx: Optional[Dict[str, Any]] = None) -> None:
    """后台执行生成，完成后通过 OneBot 直接发送图片。"""
    try:
        data = await _request("/anima/generate", method="POST", json=args)
        if not data.get("success"):
            logger.warning(f"Anima 后台生成失败: {data}")
            return

        images = data.get("images", [])
        if not images:
            logger.warning("Anima 后台生成成功但未返回图片")
            return

        img = images[0]
        image_url = img.get("view_url") or img.get("url")
        prompt_text = data.get("positive", "")
        seed = data.get("seed")
        logger.info(f"Anima 图片生成完成: {img.get('filename')} seed={seed}")

        # 尝试直接通过 OneBot 发送图片
        sent = await _send_image_with_ctx(send_ctx, image_url, prompt_text)
        if not sent:
            # 如果直接发送失败，仍然存入 pending 队列作为兜底
            _pending_results.append({
                "type": "image",
                "url": image_url,
                "filename": img.get("filename"),
                "prompt": prompt_text,
                "seed": seed,
                "width": data.get("width"),
                "height": data.get("height"),
                "chat_key": send_ctx.get("chat_key") if send_ctx else None,
            })
            logger.warning(f"Anima 图片直接发送失败，已存入 pending 队列等待兜底消费")
    except Exception as e:
        logger.exception("Anima 后台生成任务失败")


async def _send_image_with_ctx(send_ctx: Optional[Dict[str, Any]], image_url: Optional[str], prompt_text: str = "") -> bool:
    """通过 OneBot 直接发送图片到对应会话。成功返回 True。"""
    if not send_ctx or not image_url:
        return False

    bot_id = send_ctx.get("bot_id")
    if not bot_id:
        return False

    try:
        bot = get_bot(bot_id)
    except KeyError:
        logger.warning(f"未找到 bot_id={bot_id} 的 Bot 实例，无法直接发送")
        return False

    group_id = send_ctx.get("group_id")
    user_id = send_ctx.get("user_id")

    try:
        msg = MessageSegment.image(file=image_url)
        if group_id:
            await bot.send_group_msg(group_id=int(group_id), message=msg)
        elif user_id:
            await bot.send_private_msg(user_id=int(user_id), message=msg)
        else:
            return False


        logger.info(f"Anima 图片已通过 OneBot 发送到 group={group_id or user_id}")
        return True
    except Exception as e:
        logger.warning(f"Anima 图片通过 OneBot 发送失败: {e}")
        return False










# 全局待发送结果队列，matcher 侧负责消费
_pending_results: List[Dict[str, Any]] = []

# 保留对后台任务的引用，防止被 gc 取消
_bg_tasks: set = set()


def consume_pending_results() -> List[Dict[str, Any]]:
    """消费并清空待发送结果队列。由 matcher 侧调用。"""
    global _pending_results
    results = _pending_results[:]
    _pending_results.clear()
    return results


def consume_pending_results_for(chat_key: str) -> List[Dict[str, Any]]:
    """消费指定 chat_key 的待发送结果，其余保留。由 matcher 兜底调用。"""
    global _pending_results
    matched = [r for r in _pending_results if r.get("chat_key") == chat_key]
    _pending_results = [r for r in _pending_results if r.get("chat_key") != chat_key]
    return matched


def _schedule_bg(coro) -> None:
    """调度后台任务并保留引用，防止被 asyncio gc 取消。"""
    task = asyncio.create_task(coro)
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
