import asyncio
import random
import re
import string
from typing import Any, Dict, List, Optional, Tuple

import httpx

from nonebot import logger, get_bot
from nonebot.adapters.onebot.v11 import MessageSegment

_comfyui_base_url: str = "http://127.0.0.1:8188"
_anima_schema_cache: Optional[Dict[str, Any]] = None
_anima_knowledge_cache: Optional[str] = None

# 画图模式说明：
# force: 常驻工具 + 画图关键词时拦截虚假回复
# on:    常驻工具，不拦截
# auto:  仅在用户消息含画图关键词时注入工具（默认）
# off:   关闭

# 发送上下文：asyncio.Task → {chat_key, bot_id, group_id, user_id}
# 由 matcher 在发起 LLM 请求前注册，后台任务完成后用于直接发送图片
# 使用 Task 作为 key 是因为：不同群的请求并行运行在不同 Task 中，
# 而 _execute_tool_calls 在当前 Task 中 await 执行，因此 run() 可以通过
# asyncio.current_task() 精确找到属于当前请求的上下文，避免并行请求间串扰。
_send_context: Dict[Any, Dict[str, Any]] = {}


def _get_url(path: str) -> str:
    return f"{_comfyui_base_url.rstrip('/')}{path}"


def _generate_task_id() -> str:
    """生成随机的6位字母数字任务编号"""
    return 'draw-' + ''.join(random.choices(string.ascii_letters + string.digits, k=6))


def set_base_url(url: str) -> None:
    global _comfyui_base_url
    _comfyui_base_url = url


def set_chat_mode(chat_key: str, mode: str) -> None:
    """设置指定会话的画图模式（持久化）"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    chat_data.draw_mode = mode if mode in ("force", "on", "auto", "off") else "auto"
    PersistentDataManager.instance.save_to_file(must_save=True)


def get_chat_mode(chat_key: str) -> str:
    """获取指定会话的画图模式，默认 auto"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    return chat_data.draw_mode


def is_chat_enabled(chat_key: str) -> bool:
    """画图是否启用（force/on/auto 都算启用，仅 off 为关闭）"""
    return get_chat_mode(chat_key) != "off"


def any_chat_enabled() -> bool:
    """是否有任何会话启用了画图"""
    from ..persistent_data_manager import PersistentDataManager
    for cd in PersistentDataManager.instance.get_all_chat_datas():
        if cd.draw_mode != "off":
            return True
    return False


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


def _compress_anima_expert(content: str) -> str:
    """精简专家知识：保留硬性规则、字段说明、提示词技巧、多角色规范，去除冗余解释。"""
    lines = content.split('\n')
    result = []
    skip = False
    for line in lines:
        stripped = line.strip()
        # 跳过默认参数和长宽比段落（工具自动处理）
        if stripped.startswith('## 推荐默认参数') or stripped.startswith('## 长宽比'):
            skip = True
            continue
        if skip and stripped.startswith('## '):
            skip = False
        if skip:
            continue
        # 压缩冗余解释行
        if stripped.startswith('> **说明**') or stripped.startswith('> 说明'):
            continue
        result.append(line)
    return '\n'.join(result).strip()


def _compress_artist_list(content: str) -> str:
    """精简画师列表：只保留 @artist 名称，去除说明文字。"""
    artists = []
    for line in content.split('\n'):
        stripped = line.strip()
        if stripped.startswith('- `@') and '`' in stripped:
            # 提取 `@name` 中的名称
            name = stripped.split('`')[1] if '`' in stripped else ''
            if name.startswith('@'):
                artists.append(name)
    return ', '.join(artists)


def _compress_examples(content: str) -> str:
    """精简示例：保留 3 个代表性场景（单角色竖构图、双角色+外观、纯自然语言原创）。"""
    import json as _json
    # 按 ## 分割示例块
    blocks = re.split(r'^## \d+\)', content, flags=re.MULTILINE)
    examples = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        # 提取 JSON 块
        match = re.search(r'```json\s*\n(.*?)\n```', block, re.DOTALL)
        if match:
            try:
                obj = _json.loads(match.group(1))
                examples.append(obj)
            except Exception:
                pass
    if len(examples) <= 3:
        return content
    # 选择 3 个代表性场景：第1个（单角色竖构图）、第4个（双角色+外观描述）、第6个（纯自然语言原创）
    selected = [examples[0]]  # 单角色竖构图
    # 找双角色+有外观描述的
    for ex in examples[1:]:
        if ex.get('appearance') and '2' in ex.get('count', ''):
            selected.append(ex)
            break
    # 找纯自然语言原创（appearance 为空）
    for ex in examples:
        if not ex.get('appearance') and ex.get('nltags'):
            selected.append(ex)
            break
    if len(selected) < 3:
        selected = examples[:3]
    result_parts = []
    for i, ex in enumerate(selected, 1):
        result_parts.append(f"## {i})\n```json\n{_json.dumps(ex, ensure_ascii=False, indent=2)}\n```")
    return '\n\n'.join(result_parts)


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

        # 按文件类型分别压缩
        parts = []
        for k, v in knowledge_data.items():
            kl = k.lower()
            if 'expert' in kl:
                compressed = _compress_anima_expert(v)
                if compressed:
                    parts.append(f"## 提示词规范\n{compressed}\n")
            elif 'artist' in kl:
                artist_str = _compress_artist_list(v)
                if artist_str:
                    parts.append(f"## 常用画师\n{artist_str}\n")
            elif 'example' in kl:
                compressed = _compress_examples(v)
                if compressed:
                    parts.append(f"## 示例\n{compressed}\n")
            else:
                parts.append(f"## {k}\n{v}\n")

        # 精简的核心规则
        parts.append(
            "## 调用规则\n"
            "- 触发词（画/画一个/来一张/draw/改图/重画等）→ 必须在 assistant 消息中附带 tool_calls 调用 generate_anima_image。\n"
            "- 只说「在画了」但不附带 tool_calls = 没有画画。任务编号只能由工具返回，禁止编造。\n"
            "- 历史消息中的「在画了」「等出图」是上一轮结果，每次新请求必须重新调用工具。\n"
            "- 需要确认角色外观时，先用搜索工具（tavily_search 或 bocha_search）用简短查询，不要使用 bangumi 搜索，bangumi 没有外观信息。\n"
            "- 用户提出修改意见时立即重新调用。\n"
            "- 调用前不做画面描述，调用后用第一人称自然描述，不提及工具/系统/调用。\n"
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


async def _request(path: str, method: str = "GET", json: Optional[Dict] = None, timeout: int = 300) -> Any:
    url = _get_url(path)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if method.upper() == "GET":
            resp = await client.get(url)
        else:
            resp = await client.post(url, json=json)
        resp.raise_for_status()
        return resp.json()



def _build_positive(args: Dict[str, Any]) -> str:
    """按固定顺序拼接作画描述，角色相关字段合并到 [角色]。"""
    parts = []
    if args.get("quality_meta_year_safe"):
        parts.append(f"[质量与安全] {args['quality_meta_year_safe']}")
    if args.get("count"):
        parts.append(f"[人数] {args['count']}")
    # 角色相关合并
    role_parts = []
    for key in ("character", "appearance", "style", "environment", "nltags"):
        val = args.get(key)
        if val:
            role_parts.append(str(val))
    if role_parts:
        parts.append(f"[角色] {'；'.join(role_parts)}")
    if args.get("series"):
        parts.append(f"[作品系列] {args['series']}")
    if args.get("artist"):
        parts.append(f"[艺术家] {args['artist']}")
    if args.get("tags"):
        parts.append(f"[通用标签] {args['tags']}")
    return " ".join(parts)


def _enhance_schema(schema_data: Dict[str, Any]) -> Dict[str, Any]:
    """增强 schema description，引导模型积极调用。"""
    data = dict(schema_data)
    original_desc = data.get("description", "")
    data["description"] = (
        "画图工具。触发词（画/画一个/draw/改图/重画等）出现时必须通过 tool_calls 调用此工具，"
        "禁止只发文字不调用。任务编号只能由工具返回，禁止编造。"
        + (f" {original_desc}" if original_desc else "")
    )
    return data


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """工具调用入口：先查队列，再决定是否提交。"""
    base_url = getattr(config, "COMFYUI_BASE_URL", "http://127.0.0.1:8188")
    set_base_url(base_url)

    # 校验参数：至少需要一个有效字段（character / appearance / nltags / artist / series / tags / style / environment）
    _meaningful_keys = ("character", "appearance", "nltags", "artist", "series", "tags", "style", "environment")
    if not args or not any(args.get(k) for k in _meaningful_keys):
        return "参数为空，无法生成图片。请根据用户描述提供作画参数，然后重新调用工具。", []

    current_task = asyncio.current_task()
    send_ctx = dict(_send_context.get(current_task, {}))

    positive_desc = _build_positive(args)
    steps = args.get("steps") or 35

    # 尝试查询队列状态
    queue_info = await _check_queue(steps)
    if queue_info is not None:
        if queue_info.get("queue_too_long"):
            mins = queue_info.get("estimated_remaining_minutes", "?")
            active = queue_info.get("active_tasks", 0)
            qlen = queue_info.get("queue_length", 0)
            logger.info(f"Anima 队列过长，拒绝提交: active={active} queued={qlen} est={mins}min")
            content = (
                f"当前画图队列繁忙（{qlen} 个任务，预计等待 {mins} 分钟），请稍后再试。"
            )
            return content, []
        # 队列正常，后端返回的时长已包含当前任务耗时
        est_seconds = queue_info.get("estimated_remaining_seconds", 60)
        # 计算预计生成时间：当前图片预计生成时间 + n*一分半 - 30秒
        queue_length = queue_info.get("queue_length", 0)
        est_seconds = est_seconds + queue_length * 90 - 30
        est_minutes = max(1, round(est_seconds / 60))
        logger.info(
            f"Anima 队列检查通过: active={queue_info.get('active_tasks', 0)} "
            f"queued={queue_info.get('queue_length', 0)} "
            f"est_remaining={est_seconds}s ({est_minutes}min)"
        )
    else:
        # 接口异常，回退到本地估算：60 + (steps - 35) * 1.5
        est_seconds = int(60 + (int(steps) - 35) * 1.5)
        est_minutes = max(1, round(est_seconds / 60))

    # 生成随机的6位字母数字任务编号
    task_id = _generate_task_id()

    # 保存提示词到数据库
    from ..draw_db import save_prompt
    save_prompt(task_id, args)

    content = (
        f"你正在画一幅插画：{positive_desc}。任务编号：{task_id}，预计{est_seconds}秒完成。"
        f"你必须将任务编号和预计时间告知用户，这是确认任务已成功提交的唯一凭证。"
        f"用第一人称自然地告诉用户你正在作画，不要提到工具或系统。"
        f"注意：调用工具前不要对画面做出描述，调用完成后再描述画面内容。"
    )

    _schedule_bg(_do_generate(args, config, send_ctx, task_id, timeout=600))
    return content, []


async def _check_queue(steps: int) -> Optional[Dict[str, Any]]:
    """直接查 ComfyUI 队列，估算等待时间。异常时返回 None。"""
    try:
        data = await _request("/queue", method="GET", timeout=10)
        running = len(data.get("queue_running", []))
        pending = len(data.get("queue_pending", []))
        real_count = running + pending

        # running 按已完成一半估算，pending 按满时估算
        task_time = int(60 + (int(steps) - 35) * 1.5)
        real_remaining = running * task_time * 0.5 + pending * task_time
        total = real_remaining + task_time  # 加上当前任务自身
        est_minutes = max(1, round(total / 60))

        # 队列长度大于5时拒绝
        queue_too_long = real_count > 5
        
        return {
            "active_tasks": running,
            "queue_length": real_count + 1,
            "estimated_remaining_seconds": round(total),
            "estimated_remaining_minutes": est_minutes,
            "queue_too_long": queue_too_long,
        }
    except Exception as e:
        logger.warning(f"Anima 队列查询失败，回退直接提交: {e}")
        return None


async def _do_generate(args: Dict[str, Any], config, send_ctx: Optional[Dict[str, Any]] = None, task_id: str = "", timeout: int = 600) -> None:
    """后台执行生成，完成后通过 OneBot 直接发送图片。"""
    try:
        data = await _request("/anima/generate", method="POST", json=args, timeout=timeout)
        if not data.get("success"):
            logger.warning(f"Anima 后台生成失败: {data}")
            return

        images = data.get("images", [])
        if not images:
            logger.warning("Anima 后台生成成功但未返回图片")
            return

        prompt_text = data.get("positive", "")
        seed = data.get("seed")
        queue = data.get("queue", {})
        q_active = queue.get("active_tasks", 0)
        q_len = queue.get("queue_length", 0)
        q_mins = queue.get("estimated_remaining_minutes", 0)
        logger.info(
            f"Anima 图片生成完成: {len(images)} 张 seed={seed} | "
            f"队列: active={q_active} queued={q_len} est={q_mins}min"
        )

        for i, img in enumerate(images):
            image_url = img.get("view_url") or img.get("url")
            if not image_url:
                continue
            # 拼接任务序号和图片一起发送
            task_info = f"任务编号：{task_id}" if task_id else ""
            sent = await _send_image_with_ctx(send_ctx, image_url, prompt_text, task_info)
            if not sent:
                _pending_results.append({
                    "type": "image",
                    "url": image_url,
                    "filename": img.get("filename"),
                    "prompt": prompt_text,
                    "seed": seed,
                    "width": data.get("width"),
                    "height": data.get("height"),
                    "chat_key": send_ctx.get("chat_key") if send_ctx else None,
                    "task_id": task_id,
                })
                logger.warning(f"Anima 图片 {i+1} 直接发送失败，已存入 pending 队列等待兜底消费")
    except Exception as e:
        logger.exception("Anima 后台生成任务失败")


async def _send_image_with_ctx(send_ctx: Optional[Dict[str, Any]], image_url: Optional[str], prompt_text: str = "", task_info: str = "") -> bool:
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
        # 拼接任务信息和图片
        msg = MessageSegment.image(file=image_url)
        if task_info:
            msg = MessageSegment.text(task_info + "\n") + msg
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
