import importlib
import inspect
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from nonebot import logger

from . import anima_generate

# 工具注册表: {name: (schema, run_func)}
TOOL_REGISTRY: Dict[str, Tuple[Dict[str, Any], Callable]] = {}

# 不参与自动发现的模块
_EXCLUDED_MODULES = {"__init__", "common", "anima_generate"}


def _discover_tools(config) -> None:
    """自动发现并注册 llm_tool_plugins 目录下的工具。"""
    plugin_dir = Path(__file__).parent
    registered: List[str] = []
    disabled_tools = set(getattr(config, "LLM_DISABLED_TOOLS", []) or [])

    for py_file in sorted(plugin_dir.glob("*.py")):
        module_name = py_file.stem
        if module_name in _EXCLUDED_MODULES:
            continue
        if module_name in disabled_tools:
            logger.info(f"[工具发现] 跳过禁用的工具模块: {module_name}")
            continue

        try:
            module = importlib.import_module(f".{module_name}", package=__name__)
        except Exception as e:
            logger.warning(f"[工具发现] 加载模块 {module_name} 失败: {e}")
            continue

        # 检查 should_load(config) - 可选的条件加载钩子
        should_load = getattr(module, "should_load", None)
        if should_load and not should_load(config):
            continue

        # 方式1: 模块导出 TOOLS 列表 [(name, schema, run), ...]
        tools_list = getattr(module, "TOOLS", None)
        if tools_list and isinstance(tools_list, (list, tuple)):
            for tool_def in tools_list:
                if len(tool_def) == 3:
                    name, schema, run_func = tool_def
                    TOOL_REGISTRY[name] = (schema, run_func)
                    registered.append(name)
            init_func = getattr(module, "init", None)
            if init_func:
                try:
                    init_func(config)
                except Exception as e:
                    logger.warning(f"[工具发现] {module_name} 初始化失败: {e}")
            continue

        # 方式2: 模块导出 schema + run (单工具)
        schema = getattr(module, "schema", None)
        run_func = getattr(module, "run", None)
        if schema and run_func and inspect.isfunction(run_func):
            TOOL_REGISTRY[module_name] = (schema, run_func)
            registered.append(module_name)
            init_func = getattr(module, "init", None)
            if init_func:
                try:
                    init_func(config)
                except Exception as e:
                    logger.warning(f"[工具发现] {module_name} 初始化失败: {e}")
            continue

        # 方式3: 模块导出 get_tools() 函数 (动态工具列表)
        get_tools = getattr(module, "get_tools", None)
        if get_tools and inspect.isfunction(get_tools):
            try:
                tools = get_tools()
                for name, tool_schema, tool_run in tools:
                    TOOL_REGISTRY[name] = (tool_schema, tool_run)
                    registered.append(name)
            except Exception as e:
                logger.warning(f"[工具发现] {module_name} get_tools() 失败: {e}")
            continue

    logger.info(f"[工具发现] 已注册 {len(registered)} 个工具: {', '.join(registered)}")


def init_tools(config) -> None:
    """启动时根据配置条件注册工具。"""
    _discover_tools(config)


def enable_anima_tool() -> bool:
    """注册 Anima 画图工具到 TOOL_REGISTRY，返回是否成功。"""
    schema = anima_generate.get_schema()
    if schema and "generate_anima_image" not in TOOL_REGISTRY:
        TOOL_REGISTRY["generate_anima_image"] = (schema, anima_generate.run)
        return True
    return False


def disable_anima_tool() -> None:
    """从 TOOL_REGISTRY 中移除 Anima 画图工具。"""
    TOOL_REGISTRY.pop("generate_anima_image", None)
    anima_generate.clear_cache()


def is_anima_tool_enabled() -> bool:
    return "generate_anima_image" in TOOL_REGISTRY
