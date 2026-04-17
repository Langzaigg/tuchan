from typing import Any, Dict, List, Tuple

from .llm_tool_plugins import TOOL_REGISTRY
from .logger import logger


def get_tool_schemas(config) -> List[Dict[str, Any]]:
    if not config.LLM_ENABLE_TOOLS:
        return []
    return [schema for schema, _ in TOOL_REGISTRY.values()]


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
