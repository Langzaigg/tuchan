from typing import Any, Dict, List, Tuple

from .llm_tool_plugins import TOOL_REGISTRY
from .logger import logger


def get_tool_schemas(config, chat_key: str = "") -> List[Dict[str, Any]]:
    if not config.LLM_ENABLE_TOOLS:
        return []

    # 预解析当前会话 profile，供 vision 等按 profile 门控的工具复用
    _chat_profile: Dict[str, Any] = {}
    if chat_key:
        try:
            from .chat_manager import ChatManager
            _chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)
            _prof_name = _chat.get_active_profile()
            _chat_profile = config.OPENAI_PROFILES.get(_prof_name, {}) or {}
        except Exception:
            _chat_profile = {}

    schemas = []
    for name, (schema, _) in TOOL_REGISTRY.items():
        # NAS游戏工具：仅在白名单群中暴露
        if name == "nas_game_list" and chat_key:
            from .llm_tool_plugins.nas_game_list import _check_whitelist
            allowed, _, _ = _check_whitelist(config, chat_key)
            if not allowed:
                continue
        # 视觉工具：仅当主模型 multimodal=false 且配置了 model_vision 时暴露
        # 原生多模态 profile 不暴露，避免误导模型；无法解析 profile（无 chat_key）时不暴露
        if name == "vision":
            if not chat_key:
                continue
            if _chat_profile.get("multimodal", True) or not _chat_profile.get("model_vision"):
                continue
        # 画图工具：根据 draw_model 或 manga_mode 选择 schema
        if name == "generate_anima_image" and chat_key:
            from .llm_tool_plugins import anima_generate
            is_manga = anima_generate.get_manga_mode(chat_key)
            # 漫画模式使用动态选择的默认工作流 schema；否则用会话所选模型
            model = anima_generate.get_default_model() if is_manga else anima_generate.get_draw_model(chat_key)
            model_schema = anima_generate.get_schema(model)
            if model_schema:
                schemas.append(model_schema)
                continue
        schemas.append(schema)
    return schemas


async def execute_tool(name: str, args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    tool = TOOL_REGISTRY.get(name)
    if not tool:
        return f"未知工具: {name}", []

    _, runner = tool
    try:
        return await runner(args, config)
    except Exception as e:
        logger.exception(f"工具调用失败: {name}")
        return f"工具 {name} 调用失败: {e!r}", []
