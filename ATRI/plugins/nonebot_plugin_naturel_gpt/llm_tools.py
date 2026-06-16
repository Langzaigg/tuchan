from typing import Any, Dict, List, Tuple

from .llm_tool_plugins import TOOL_REGISTRY
from .logger import logger


def get_tool_schemas(config, chat_key: str = "") -> List[Dict[str, Any]]:
    if not config.LLM_ENABLE_TOOLS:
        return []
    
    schemas = []
    for name, (schema, _) in TOOL_REGISTRY.items():
        # NAS游戏工具：仅在白名单群中暴露
        if name == "nas_game_list" and chat_key:
            from .llm_tool_plugins.nas_game_list import _check_whitelist
            allowed, _, _ = _check_whitelist(config, chat_key)
            if not allowed:
                continue
        # 画图工具：根据 turbo_mode 选择 schema
        if name == "generate_anima_image" and chat_key:
            from .llm_tool_plugins import anima_generate
            is_turbo = anima_generate.get_turbo_mode(chat_key)
            turbo_schema = anima_generate.get_schema(turbo=True)
            if is_turbo and turbo_schema:
                schemas.append(turbo_schema)
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
