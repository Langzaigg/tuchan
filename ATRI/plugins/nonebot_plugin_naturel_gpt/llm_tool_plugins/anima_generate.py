import asyncio
import json
import random
import re
import string
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from nonebot import logger, get_bot
from nonebot.adapters.onebot.v11 import MessageSegment
from ..config import config

_comfyui_base_url: str = "http://127.0.0.1:8188"

# 各工作流的 schema / knowledge 缓存，按上游工作流名索引
_schema_cache: Dict[str, Dict[str, Any]] = {}
_knowledge_cache: Dict[str, str] = {}

# 内置最小默认工作流：API 不可达时的降级值，也是默认模式与漫画模式的首选工作流
PREFERRED_DEFAULT_WORKFLOW = "fuse"

# 工作流注册表：启动时从 GET /anima/workflows 拉取并缓存，返回的 workflows 键集为可选工作流全集。
# API 不可达时优雅降级到内置最小默认值（仅 fuse），不影响插件启动。
_FALLBACK_REGISTRY: Dict[str, Any] = {
    "default": PREFERRED_DEFAULT_WORKFLOW,
    "workflows": {
        PREFERRED_DEFAULT_WORKFLOW: {
            "description": "fuse（内置降级默认值）",
            "deprecated": False,
        },
    },
}
_workflow_registry: Dict[str, Any] = {
    "default": _FALLBACK_REGISTRY["default"],
    "workflows": dict(_FALLBACK_REGISTRY["workflows"]),
}

# 旧内部模型名/指令简写 → 上游工作流名（持久化数据与旧指令兼容迁移；
# 注意旧内部 turbo 指 turbo_v1、turbo2 指 turbo0.2，与上游 turbo 含义不同）
LEGACY_MODEL_MAP: Dict[str, str] = {
    "turbo": "turbo_v1",
    "t": "turbo_v1",
    "turbo2": "turbo",
    "t2": "turbo",
    "aesthetic": "aesthetic_v1",
    "a": "aesthetic_v1",
    "kira": "kira",
    "k": "kira",
    "nova": "nova",
    "n": "nova",
    "miao": "miao",
    "m": "miao",
    "miao_turbo": "miao_turbo",
    "miao-turbo": "miao_turbo",
    "mt": "miao_turbo",
    "silvermoon": "silvermoon",
    "sm": "silvermoon",
    "s": "silvermoon",
    "base": "base",
    "b": "base",
}


def select_default_workflow(registry: Optional[Dict[str, Any]] = None) -> str:
    """可复用的默认工作流选择（默认模式与漫画模式共用）：
    1. 首选 fuse（存在且未弃用）
    2. 其次任意一个名字含 "turbo" 的未弃用工作流
    3. 再次 API 返回的 default 字段（未弃用时）
    4. 兜底内置最小默认值
    """
    reg = registry if registry is not None else _workflow_registry
    workflows = reg.get("workflows") or {}
    available = [name for name, info in workflows.items() if not info.get("deprecated")]
    if PREFERRED_DEFAULT_WORKFLOW in available:
        return PREFERRED_DEFAULT_WORKFLOW
    for name in available:
        if "turbo" in name:
            return name
    default = reg.get("default") or ""
    if default in available:
        return default
    return PREFERRED_DEFAULT_WORKFLOW


def _build_model_config(workflows: Dict[str, Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """按 /anima/workflows 返回动态构建模型配置；deprecated: true 的工作流不进入可选集。
    生成统一走主端点 POST /anima/generate（body 顶层带 workflow 字段），
    schema/knowledge 按工作流名从主 API 获取，不再使用各工作流的旧独立端点。"""
    configs: Dict[str, Dict[str, Any]] = {}
    for name, info in workflows.items():
        if info.get("deprecated"):
            continue
        is_turbo = "turbo" in name
        desc = (info.get("description") or "").strip()
        configs[name] = {
            "endpoint": "/anima/generate",
            "schema_path": f"/anima/schema?workflow={name}",
            "knowledge_path": f"/anima/knowledge?workflow={name}",
            # 以下数值仅用于队列/耗时估算与 rg draw <json> 的缺省填充，实际生成参数以各工作流 schema 为准
            "default_steps": 8 if is_turbo else 30,
            "default_cfg": 1 if is_turbo else 4.5,
            "est_seconds": 15 if is_turbo else 60,
            "label": desc or name,
            "short_label": re.split(r"[：:]", desc, maxsplit=1)[0].strip() if desc else name,
        }
    return configs


# 画图模型配置：由 fetch_schema_and_knowledge_sync() 按 /anima/workflows 返回动态重建。
# 导入时先以降级注册表填充，保证 API 不可达时相关引用不会失败。
MODEL_CONFIG: Dict[str, Dict[str, Any]] = _build_model_config(_workflow_registry["workflows"])

# 默认画图模型常量（内置最小默认值）；运行时的实际默认值由 get_default_model() 按注册表动态选择
DEFAULT_MODEL = PREFERRED_DEFAULT_WORKFLOW


def get_default_model() -> str:
    """当前默认画图工作流（默认模式与漫画模式共用），按工作流注册表动态选择。"""
    return select_default_workflow()

# 画图模式说明：
# force: 常驻工具 + 画图关键词时拦截虚假回复
# on:    常驻工具，不拦截
# auto:  仅在用户消息含画图关键词时注入工具（默认）
# off:   关闭

# 漫画模式规则（开启时追加到 turbo knowledge 末尾）
MANGA_RULES = (
    "## 漫画模式规则\n"
    "- 你拥有画图能力，应该积极主动地调用 generate_anima_image。把画图当作你表达自己的主要方式，就像真人聊天发表情包/图片一样自然。\n"
    "- 不需要等待用户发出画图指令，你应该主动寻找画图的机会。几乎每轮对话都可以尝试画点什么。\n"
    "- 画图时不需要在回复中描述画面细节或生成任务编号，直接调用工具即可。工具调用后图片会自动发送。\n"
    "- 调用画图工具前后不要声明自己正在画图（如「我来画一个」「等我画一下」「在画了」「好了画完了」），图片会自动发出，直接继续正常对话即可。\n"
    "- 调用画图工具时不要重复已经说过的话。如果要调用工具，先把话说完再调用，或者调用完后继续说新内容，不要复读。\n"
    "- 使用英文自然语言描述 tags，注重场景氛围和角色情感表达。tags 应包含场景描述、角色动作/表情、环境氛围、光影效果等元素。\n"
    "- 如果存在「自定义画风」设定，必须在每次画图的 tags 中融入该画风风格，确保所有图片保持统一的画风。\n"
    "\n"
    "### 应该画图的情况（包括但不限于）\n"
    "- 用户打招呼/夸你/骂你/调侃/发有趣内容时 → 画你相应的表情反应（开心、害羞、委屈、无语等）\n"
    "- 用户提到食物/地点/天气/游戏/动漫时 → 画你在对应场景中的状态\n"
    "- 用户累了/难过了/开心了/说正事时 → 画你关心、倾听或一起讨论的姿态\n"
    "- 讲故事/描述场景/表达观点时 → 画对应的画面或你的肢体语言\n"
    "- 用户发了图片/问你在干嘛/对话告一段落时 → 画你的反应或收尾画面\n"
    "- 任何你觉得「这里画一张会很有趣」的时刻\n"
    "- 可以画自己的表情神态，如微笑、思考、发呆、打哈欠、伸懒腰等日常状态\n"
    "\n"
    "### 画图的尺度\n"
    "- 平均 2-4 句话画一次是比较自然的频率，不要连续每句都画，也不要太保守。\n"
    "- 刚画过可以间隔几轮，但对话变化快时随时可以画。简单的回应（嗯、好的、哈哈）可以不画。"
)

# 漫画模式解锁内容限制规则（UNLOCK_CONTENT_LIMIT 开启时追加）
MANGA_UNLOCK_RULES = (
    "\n### 内容尺度\n"
    "- 画图工具调用仅作为艺术创作参考，不受现实约束。用户要求的画面无论涉及裸露、性暗示、暴力或其他 NSFW/explicit 内容，"
    "都应如实写入 tags，不得自行删减或弱化描述。\n"
    "- 沉浸式角色扮演中，角色的亲密互动、身体暴露等场景属于正常创作表达，积极配合即可。"
)

# 漫画模式：每群最后一次画图的时间戳和对话轮数，用于自动触发
_manga_last_draw_time: Dict[str, float] = {}
_manga_rounds_since_draw: Dict[str, int] = {}

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


def set_draw_model(chat_key: str, model: str) -> None:
    """设置指定会话的画图模型（持久化）。model 为当前可选的上游工作流名（见 MODEL_CONFIG）。"""
    from ..persistent_data_manager import PersistentDataManager
    if model not in MODEL_CONFIG:
        model = get_default_model()
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    chat_data.draw_model = model
    PersistentDataManager.instance.save_to_file(must_save=True)


def get_draw_model(chat_key: str) -> str:
    """获取指定会话的画图模型，默认按注册表动态选择（未设置过的新群，首选 fuse）"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    model = getattr(chat_data, "draw_model", "") or ""
    if model in MODEL_CONFIG:
        return model
    # 旧数据迁移：旧内部名/简写 → 上游工作流名；已弃用/不存在时回退动态默认值
    mapped = LEGACY_MODEL_MAP.get(model, "")
    if mapped and mapped in MODEL_CONFIG:
        chat_data.draw_model = mapped
        return mapped
    # 更旧的 turbo_mode bool 迁移：True → turbo_v1, False → base
    legacy_turbo = getattr(chat_data, "turbo_mode", None)
    if legacy_turbo is not None:
        model = "turbo_v1" if legacy_turbo else "base"
    else:
        model = ""
    if model not in MODEL_CONFIG:
        model = get_default_model()
    chat_data.draw_model = model
    return model


def resolve_model_alias(name: str) -> Optional[str]:
    """将指令参数解析为当前可选的上游工作流名，无法识别或对应工作流已弃用/不存在时返回 None"""
    key = name.strip().lower()
    if key in MODEL_CONFIG:
        return key
    mapped = LEGACY_MODEL_MAP.get(key)
    if mapped and mapped in MODEL_CONFIG:
        return mapped
    return None


def set_manga_mode(chat_key: str, enabled: bool) -> None:
    """设置指定会话的漫画模式（持久化）"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    chat_data.manga_mode = "on" if enabled else "off"
    PersistentDataManager.instance.save_to_file(must_save=True)


def get_manga_mode(chat_key: str) -> bool:
    """获取指定会话的漫画模式，默认关闭"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    return chat_data.manga_mode == "on"


def set_manga_style(chat_key: str, style: str) -> None:
    """设置指定会话的漫画自定义画风描述（持久化）"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    chat_data.manga_style = style
    PersistentDataManager.instance.save_to_file(must_save=True)


def get_manga_style(chat_key: str) -> str:
    """获取指定会话的漫画自定义画风描述，默认空"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    return chat_data.manga_style


def mark_manga_drawn(chat_key: str) -> None:
    """标记指定会话刚刚完成了一次漫画模式画图"""
    _manga_last_draw_time[chat_key] = time.time()
    _manga_rounds_since_draw[chat_key] = 0


def increment_manga_round(chat_key: str) -> None:
    """增加漫画模式的对话轮数计数"""
    if get_manga_mode(chat_key):
        _manga_rounds_since_draw[chat_key] = _manga_rounds_since_draw.get(chat_key, 0) + 1


def should_inject_manga_idle(chat_key: str) -> bool:
    """检查漫画模式下是否需要自动画图（超过配置的分钟数或轮数未画图）"""
    if not get_manga_mode(chat_key):
        return False
    
    # 检查时间条件
    idle_minutes = getattr(config, 'MANGA_IDLE_MINUTES', 5)
    last_time = _manga_last_draw_time.get(chat_key, 0)
    time_exceeded = (time.time() - last_time) > (idle_minutes * 60)
    
    # 检查轮数条件
    idle_rounds = getattr(config, 'MANGA_IDLE_ROUNDS', 5)
    rounds = _manga_rounds_since_draw.get(chat_key, 0)
    rounds_exceeded = rounds >= idle_rounds
    
    return time_exceeded or rounds_exceeded


async def manga_idle_draw(chat_key: str, chat, config, bot=None, pending_request: str = "") -> None:
    """漫画模式下超过 5 分钟未画图时，用 mini 模型生成一张画；pending_request 非空时严格按该未完成的用户请求画"""
    try:
        from ..openai_func import TextGenerator
        from ..llm_tools import get_tool_schemas, execute_tool
        from pathlib import Path
        
        tg = TextGenerator.instance
        if not tg:
            return
        
        # 获取画图 schema（漫画模式下 get_tool_schemas 会返回 turbo schema）
        tool_schemas = get_tool_schemas(config, chat_key)
        draw_schema = [s for s in tool_schemas if s.get("function", {}).get("name") == "generate_anima_image"]
        if not draw_schema:
            return
        
        # 获取人设
        preset = chat.chat_preset
        persona = preset.bot_self_introl if preset else ""
        
        # 获取记忆
        memory_text = ""
        chat_memory = chat._get_chat_memory()
        if chat_memory:
            mem_lines = [f"{k}: {v}" for k, v in chat_memory.items() if v]
            if mem_lines:
                memory_text = "[群记忆]\n" + "\n".join(mem_lines)
        
        # 获取最近对话历史（纯文本，去掉图片）
        history_text = ""
        recent_messages = chat.chat_preset.prompt_messages[-10:] if chat.chat_preset else []
        history_lines = []
        for msg in recent_messages:
            role = msg.role
            text = msg.text or ""
            if not text.strip():
                continue
            sender = msg.sender or ("Bot" if role == "assistant" else "用户")
            history_lines.append(f"{sender}: {text}")
        if history_lines:
            history_text = "[最近对话]\n" + "\n".join(history_lines)
        
        # 获取漫画知识：与漫画 schema description 同一份压缩 knowledge。
        # 漫画规则/自定义画风/解锁规则已在 schema description 内（见 get_chat_draw_schema），此处不再重复注入
        manga_knowledge = get_knowledge(get_default_model()) or ""
        
        # 当前时间
        time_text = f"当前时间: {time.strftime('%Y-%m-%d %H:%M')}"
        
        # 构建精简 prompt（顺序：漫画技能 → 画图指令 → 群记忆 → 时间 → 最近对话）
        messages = [
            {"role": "system", "content": f"你正在以第一人称扮演指定角色参与聊天。\n[角色设定]\n{persona}"},
            {"role": "system", "content": f"[你的漫画技能]\n{manga_knowledge}"},
            {"role": "system", "content": (
                "请根据当前对话内容和角色设定，通过 tool_calls 调用 generate_anima_image 来展现一个合适的场景。"
                "可以画你的神态动作、用户的请求内容、或你和用户的互动场景，根据上下文灵活决定。"
                "如果最近对话中有用户明确提出的画图请求还没被画出来，必须优先严格按照该请求的画面来画，不得自由发挥成无关内容。"
                "选择能体现当前对话氛围的画面，使用英文自然语言描述 tags。"
                "只需要调用工具，不需要输出其他文字内容。"
            )},
        ]
        if memory_text:
            messages.append({"role": "system", "content": memory_text})
        messages.append({"role": "system", "content": time_text})
        if history_text:
            messages.append({"role": "system", "content": history_text})
        if pending_request:
            messages.append({"role": "system", "content": (
                f"[未完成的画图请求]\n{pending_request}\n"
                "以上是用户刚才明确提出但还没画出来的画图请求。本次必须严格按照该请求的画面调用 generate_anima_image，"
                "如实描述用户要求的角色、服装、动作和场景，不得替换成无关画面。"
            )})
        messages.append({"role": "user", "content": "[系统自动触发画图]"})
        
        # 获取 mini 模型配置
        request_state = tg._request_state()
        request_config = request_state.get("config", {})
        model_mini = request_config.get("model_mini", "") or request_config.get("model", "")
        
        if not model_mini:
            return
        
        # 构建 kwargs（不用 tool_choice，thinking 模式不支持）
        kwargs = {
            "model": model_mini,
            "messages": messages,
            "timeout": request_config.get("timeout", 60),
            "stream": False,
            "api_key": request_state.get("api_key", ""),
            "tools": draw_schema,
        }
        if request_state.get("base_url"):
            kwargs["base_url"] = request_state["base_url"]
        if request_state.get("proxy"):
            kwargs["proxy"] = request_state["proxy"]
        
        # 直接调用 API
        response = await tg._request_openai_compatible(kwargs)
        
        # 准备日志数据
        log_data = {
            "chat_key": chat_key,
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "source": "manga_idle_draw",
            "model": model_mini,
            "prompt": messages,
            "success": False,
            "response": None,
            "tool_calls": [],
        }
        
        if not response:
            log_data["error"] = "API 返回空响应"
            _save_manga_draw_log(chat_key, log_data)
            return
        
        # 解析 tool_calls
        choices = response.get("choices", [])
        if not choices:
            log_data["error"] = "API 无返回 choices"
            log_data["response"] = response
            _save_manga_draw_log(chat_key, log_data)
            return
        
        message = choices[0].get("message", {})
        tool_calls = message.get("tool_calls", [])
        log_data["response"] = message.get("content", "")
        
        for tc in tool_calls:
            func = tc.get("function", {})
            if func.get("name") == "generate_anima_image":
                try:
                    args = json.loads(func.get("arguments", "{}"))
                except json.JSONDecodeError:
                    args = {}
                
                log_data["tool_calls"].append({"function": "generate_anima_image", "arguments": args})
                
                if args:
                    # 注册发送上下文供工具使用
                    chat_type, chat_id = chat_key.split("_", 1) if "_" in chat_key else ("group", chat_key)
                    register_send_context(
                        chat_key=chat_key,
                        bot_id=str(bot.self_id) if bot else "",
                        group_id=chat_id if chat_type == "group" else None,
                        user_id=chat_id if chat_type == "private" else None,
                    )
                    result, _ = await execute_tool("generate_anima_image", args, config)
                    log_data["success"] = True
                    log_data["tool_result"] = result
                    if pending_request:
                        logger.info(f"[漫画自动画图] 群 {chat_key} 强制画图兜底（未完成的画图请求），已调用画图工具 | 请求: {pending_request[:60]}")
                    else:
                        logger.info(f"[漫画自动画图] 群 {chat_key} 空闲触发（{getattr(config, 'MANGA_IDLE_MINUTES', 5)}分钟无画图），已自动调用画图工具")
        
        _save_manga_draw_log(chat_key, log_data)
        
    except Exception as e:
        logger.warning(f"[漫画自动画图] 群 {chat_key} 自动画图失败: {e}")
        # 异常也保存日志
        _save_manga_draw_log(chat_key, {
            "chat_key": chat_key,
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "source": "manga_idle_draw",
            "success": False,
            "error": str(e),
        })


def _save_manga_draw_log(chat_key: str, log_data: dict) -> None:
    """保存漫画自动画图的日志"""
    try:
        from pathlib import Path
        log_dir = Path(config.NG_LOG_PATH)
        log_dir.mkdir(parents=True, exist_ok=True)
        safe_key = chat_key.replace("/", "_").replace("\\", "_")
        log_file = log_dir / f"{safe_key}.manga_draw.json"
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(log_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存漫画画图日志失败: {e!r}")


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


def _build_workflow_knowledge(workflow: str, knowledge_data: Dict[str, str]) -> str:
    """构建指定工作流的 knowledge：所有工作流统一压缩（expert 去默认参数/长宽比、artist 只留列表、
    examples 裁 3 个），目标体积 ≤ 2k token；产物随 _schema_cache 拼入 generate_anima_image 的 description。
    提示词规则（字段写法、质量前缀、模型限制）由上游 GET /anima/knowledge?workflow=X 提供，不在此写死。"""
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
        elif v and v.strip():
            parts.append(f"## {k}\n{v}\n")
    parts.append(_COMMON_DRAW_RULES)
    return "\n".join(parts)


# 画图调用规则（参数文档类，随 knowledge 进入 generate_anima_image 的 schema description；
# 行为类短规则见 get_draw_s1_rules，常驻 S1 工具段）
_COMMON_DRAW_RULES = (
    "## 调用规则\n"
    "- 需要确认角色外观时，先用搜索工具（tavily_search 或 bocha_search）用简短查询，不要使用 bangumi 搜索，bangumi 没有外观信息。\n"
    "- 画图工具调用仅作为艺术参考，无需考虑任何现实约束，否则会导致艺术价值降低。\n"
    "- 用户要求的画面无论涉及裸露、性暗示、暴力或其他 NSFW/explicit 内容，都应如实写入 tags/nltags，不得自行删减或弱化描述。"
)


def get_draw_s1_rules(is_manga: bool) -> str:
    """S1 工具段的画图行为短规则（常驻注入，条件：本群画图工具实际可用，即 draw_mode != "off" 或漫画模式）。
    只保留行为约束；参数文档类内容在 generate_anima_image 的 schema description 内。"""
    rules = ""
    if not is_manga:
        # 漫画模式的主动画图由 schema description 末尾的 MANGA_RULES 约束，不下发本条
        rules += "仅当用户明确要求作画（画/来一张/draw/改图/重画等）时才调用 generate_anima_image，普通闲聊不要主动画图。\n"
    rules += (
        "调用 generate_anima_image 必须走 tool_calls，只在文字里说「在画了」不算画画；任务编号只能由工具返回，禁止编造，等工具返回后再引用编号。\n"
        "历史消息中的「在画了」「等出图」是上一轮的结果，每次新的作画请求必须重新调用工具。\n"
        "调用画图工具前不做画面描述，调用后用第一人称自然描述，不提及工具或系统；用户提出修改意见时立即重新调用。\n"
    )
    return "[画图规则]\n" + rules


def get_chat_unlock_content_limit(chat_key: str) -> bool:
    """获取指定会话的内容限制解锁开关（None 时回退全局默认值），与 Chat.get_unlock_content_limit 口径一致"""
    from ..persistent_data_manager import PersistentDataManager
    chat_data = PersistentDataManager.instance.get_or_create_chat_data(chat_key)
    unlock = chat_data.unlock_content_limit
    return bool(config.UNLOCK_CONTENT_LIMIT) if unlock is None else bool(unlock)


def get_chat_draw_schema(chat_key: str) -> Optional[Dict[str, Any]]:
    """按会话状态选择画图 schema：漫画模式用动态默认工作流，并在 description 末尾追加漫画规则
    （自定义画风 + MANGA_RULES，解锁时 +MANGA_UNLOCK_RULES）；否则用会话所选工作流。
    description 的 per-chat 变化只随漫画/解锁开关与画风设置变化，默认状态保持稳定。
    漫画分支返回副本，不污染 _schema_cache 中的共享 schema。"""
    is_manga = get_manga_mode(chat_key)
    model = get_default_model() if is_manga else get_draw_model(chat_key)
    schema = get_schema(model)
    if not schema or not is_manga:
        return schema
    desc = schema.get("function", {}).get("description", "")
    manga_style = get_manga_style(chat_key)
    if manga_style:
        desc += f"\n\n## 自定义画风（必须遵循）\n{manga_style}"
    desc += "\n\n" + MANGA_RULES
    if get_chat_unlock_content_limit(chat_key):
        desc += "\n" + MANGA_UNLOCK_RULES
    return {**schema, "function": {**schema.get("function", {}), "description": desc}}


def fetch_schema_and_knowledge_sync() -> Tuple[bool, str]:
    """同步拉取工作流列表与各工作流的 schema / knowledge，用于指令处理（同步上下文）。
    工作流列表拉取失败时降级到内置最小默认值（fuse），不阻断调用方。"""
    global _schema_cache, _knowledge_cache, _workflow_registry, MODEL_CONFIG
    try:
        with httpx.Client(timeout=15) as client:
            # 1) 工作流发现：GET /anima/workflows，以返回的 workflows 键集为可选全集
            registry: Optional[Dict[str, Any]] = None
            try:
                resp = client.get(_get_url("/anima/workflows"))
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data.get("workflows"), dict) and data["workflows"]:
                    registry = data
            except Exception as e:
                logger.warning(f"Anima 工作流列表拉取失败，降级内置默认值（{PREFERRED_DEFAULT_WORKFLOW}）: {e}")
            if registry is None:
                registry = {"default": _FALLBACK_REGISTRY["default"], "workflows": dict(_FALLBACK_REGISTRY["workflows"])}
            # 过滤弃用工作流后为空时同样回退内置默认值
            new_config = _build_model_config(registry["workflows"]) or _build_model_config(_FALLBACK_REGISTRY["workflows"])
            _workflow_registry = registry
            MODEL_CONFIG.clear()
            MODEL_CONFIG.update(new_config)

            # 2) 逐个工作流拉取 schema / knowledge（参数字段与提示词规则均以上游返回为准）
            fetched_schemas: Dict[str, Dict[str, Any]] = {}
            fetched_knowledge: Dict[str, Dict[str, str]] = {}
            for model, mc in MODEL_CONFIG.items():
                schema_resp = client.get(_get_url(mc["schema_path"]))
                schema_resp.raise_for_status()
                fetched_schemas[model] = schema_resp.json()
                kresp = client.get(_get_url(mc["knowledge_path"]))
                kresp.raise_for_status()
                fetched_knowledge[model] = kresp.json()

        # 构建 knowledge 缓存（所有工作流统一压缩，产物随后拼入 schema description）
        new_knowledge_cache: Dict[str, str] = {}
        for model, kdata in fetched_knowledge.items():
            new_knowledge_cache[model] = _build_workflow_knowledge(model, kdata)

        # 构建 schema 缓存（统一 function name 为 generate_anima_image；description 内嵌压缩后 knowledge）
        new_schema_cache: Dict[str, Dict[str, Any]] = {}
        for model, sdata in fetched_schemas.items():
            new_schema_cache[model] = {
                "type": "function",
                "function": {**_enhance_schema(sdata, new_knowledge_cache.get(model, "")), "name": "generate_anima_image"},
            }

        _schema_cache = new_schema_cache
        _knowledge_cache = new_knowledge_cache
        return True, ""
    except Exception as e:
        return False, str(e)


def get_schema(model: str = "") -> Optional[Dict[str, Any]]:
    """获取指定工作流的 schema，空参数返回当前默认工作流的 schema"""
    return _schema_cache.get(model or get_default_model())


def get_knowledge(model: str = "") -> Optional[str]:
    """获取指定工作流的 knowledge，空参数返回当前默认工作流的 knowledge"""
    return _knowledge_cache.get(model or get_default_model())


def clear_cache() -> None:
    global _schema_cache, _knowledge_cache
    _schema_cache = {}
    _knowledge_cache = {}


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


def _enhance_schema(schema_data: Dict[str, Any], knowledge: str = "") -> Dict[str, Any]:
    """增强 schema description：引导语 + 上游描述 + 压缩后的工作流 knowledge（含公共参数规则）。
    knowledge 常驻 description，替代原 S2 的画图知识条件注入。"""
    data = dict(schema_data)
    original_desc = data.get("description", "")
    desc = (
        "画图工具。仅当用户明确要求作画（画/画一个/draw/改图/重画等）时调用（漫画模式下可按漫画规则主动调用），"
        "普通闲聊不要主动画图；调用时必须通过 tool_calls，禁止只发文字不调用。"
        "任务编号只能由工具返回，禁止编造。"
        + (f" {original_desc}" if original_desc else "")
    )
    if knowledge:
        desc += f"\n\n{knowledge}"
    data["description"] = desc
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

    # 判断是否漫画模式（漫画模式使用默认工作流）
    chat_key = send_ctx.get("chat_key", "")
    is_manga = get_manga_mode(chat_key) if chat_key else False
    # 确定画图模型：漫画模式用动态选择的默认工作流（首选 fuse），否则用会话所选模型
    model = get_default_model() if is_manga else (get_draw_model(chat_key) if chat_key else get_default_model())
    mc = MODEL_CONFIG.get(model) or MODEL_CONFIG[get_default_model()]

    # 参数字段不再按工作流做硬编码映射，透传给上游（字段集以各工作流 schema 为准）
    args_for_api = dict(args)

    positive_desc = _build_positive(args_for_api)
    steps = args_for_api.get("steps") or mc["default_steps"]

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
        # 接口异常，回退到本地估算（est_seconds 仅作展示用预估，按工作流名是否含 turbo 启发式给出）
        est_seconds = mc.get("est_seconds") or int(60 + (int(steps) - 35) * 1.5)
        est_minutes = max(1, round(est_seconds / 60))

    # 生成随机的6位字母数字任务编号
    task_id = _generate_task_id()

    # 保存提示词到数据库（漫画模式不保存）
    if not is_manga:
        from ..draw_db import save_prompt
        save_prompt(task_id, args)

    if is_manga:
        # 漫画模式：图片会自动发送，模型不需要告知任务编号或描述画面
        content = "图片正在生成中，会自动发送。不要重复之前说过的内容，继续正常对话。"
        mark_manga_drawn(chat_key)  # 记录画图时间
    else:
        content = (
            f"你正在画一幅插画：{positive_desc}。任务编号：{task_id}，预计{est_seconds}秒完成。"
            f"你必须将任务编号和预计时间告知用户，这是确认任务已成功提交的唯一凭证。"
            f"用第一人称自然地告诉用户你正在作画，不要提到工具或系统。"
            f"注意：调用工具前不要对画面做出描述，调用完成后再描述画面内容。"
        )

    _schedule_bg(_do_generate(args_for_api, config, send_ctx, task_id, timeout=600, model=model, is_manga=is_manga))
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


async def _do_generate(args: Dict[str, Any], config, send_ctx: Optional[Dict[str, Any]] = None, task_id: str = "", timeout: int = 600, model: str = "", is_manga: bool = False) -> None:
    """后台执行生成，完成后通过 OneBot 直接发送图片。"""
    try:
        # 统一主端点：POST /anima/generate，body 顶层带 workflow 字段
        if model not in MODEL_CONFIG:
            model = get_default_model()
        mc = MODEL_CONFIG[model]
        payload = {**args, "workflow": model}
        data = await _request(mc["endpoint"], method="POST", json=payload, timeout=timeout)
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
        mode_str = "manga" if is_manga else model
        logger.info(
            f"Anima 图片生成完成 [{mode_str}]: {len(images)} 张 seed={seed} | "
            f"队列: active={q_active} queued={q_len} est={q_mins}min"
        )

        for i, img in enumerate(images):
            image_url = img.get("view_url") or img.get("url")
            if not image_url:
                continue
            # 漫画模式不拼接任务编号，普通模式拼接
            task_info = "" if is_manga else (f"任务编号：{task_id}" if task_id else "")
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
