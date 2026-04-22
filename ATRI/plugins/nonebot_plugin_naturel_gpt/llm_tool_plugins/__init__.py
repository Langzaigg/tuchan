from nonebot import logger

from .browse_url import schema as browse_url_schema, run as run_browse_url
from .fetch_url import schema as fetch_url_schema, run as run_fetch_url
from .pixiv_search import schema as pixiv_search_schema, run as run_pixiv_search
from . import anima_generate

TOOL_REGISTRY: dict = {
    "pixiv_search": (pixiv_search_schema, run_pixiv_search),
    "fetch_url": (fetch_url_schema, run_fetch_url),
    "browse_url": (browse_url_schema, run_browse_url),
}


def init_tools(config) -> None:
    """启动时根据配置条件注册工具。"""
    # 博查：有非空 key 才加载
    if getattr(config, "BOCHA_API_KEY", None):
        from .bocha_search import schema as bocha_search_schema, run as run_bocha_search
        TOOL_REGISTRY["bocha_search"] = (bocha_search_schema, run_bocha_search)
        logger.info("博查搜索工具已加载")
    else:
        logger.info("博查搜索未配置 BOCHA_API_KEY，已跳过加载")


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
