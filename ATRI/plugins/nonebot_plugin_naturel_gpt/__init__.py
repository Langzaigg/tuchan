from typing import Awaitable, Callable, Optional, Tuple
import asyncio
import httpx
from nonebot import get_driver
from .logger import logger
from nonebot.matcher import Matcher
from nonebot.adapters import Bot, Event

from .config import *
from . import utils

global_config = get_driver().config
# logger.info(config) # 这里可以打印出配置文件的内容

from .openai_func import TextGenerator
from .persistent_data_manager import PersistentDataManager
from .chat_manager import ChatManager
from . import matcher
from . import matcher_MCRcon # noqa: F401
from .llm_tool_plugins import init_tools


async def _check_proxy_connectivity(proxy_url: str) -> bool:
    """检查代理连通性"""
    test_urls = [
        "https://api.bgm.tv/calendar",
        "https://www.pixiv.net",
        "https://www.google.com",
    ]
    async with httpx.AsyncClient(proxy=proxy_url, timeout=15) as client:
        for url in test_urls:
            try:
                resp = await client.get(url)
                if resp.status_code < 500:
                    return True
            except Exception:
                continue
    return False


def _disable_proxy_tools(config) -> None:
    """禁用需要代理的工具并清空代理配置"""
    disabled = set(getattr(config, "LLM_DISABLED_TOOLS", []) or [])
    proxy_tools = ["pixiv_search", "bangumi_search"]
    for tool in proxy_tools:
        disabled.add(tool)
    config.LLM_DISABLED_TOOLS = list(disabled)
    config.TOOL_PROXY = ""  # 清空代理配置，所有工具使用直连
    logger.warning(f"[代理检查] 代理不可用，已禁用需要代理的工具: {proxy_tools}，所有工具将使用直连")


def set_permission_check_func(callback:Callable[[Matcher, Event, Bot, str, str], Awaitable[Tuple[bool,Optional[str]]]]):
    """设置Matcher的权限检查函数"""
    matcher.permission_check_func = callback

# 设置默认权限检查函数，有需求时可以覆盖
set_permission_check_func(utils.default_permission_check_func)

""" ======== 读取历史记忆数据 ======== """
PersistentDataManager.instance.load_from_file()
ChatManager.instance.create_all_chat_object() # 启动时创建所有的已有Chat对象，以便被 -all 相关指令控制

# 检查代理连通性
_proxy = getattr(config, "TOOL_PROXY", "")
if _proxy:
    logger.info(f"[代理检查] 正在检查代理连通性: {_proxy}")
    try:
        _loop = asyncio.get_event_loop()
        if _loop.is_running():
            # 如果事件循环已在运行，创建一个任务
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, _check_proxy_connectivity(_proxy))
                _proxy_ok = future.result(timeout=15)
        else:
            _proxy_ok = _loop.run_until_complete(_check_proxy_connectivity(_proxy))
    except Exception as e:
        logger.warning(f"[代理检查] 代理连通性检查失败: {e}")
        _proxy_ok = False
    
    if not _proxy_ok:
        _disable_proxy_tools(config)
    else:
        logger.info("[代理检查] 代理连通性检查通过")
else:
    logger.info("[代理检查] 未配置代理，跳过检查")

# 条件加载工具（如博查搜索需配置 key 才注册）
init_tools(config)

# Anima 画图：启动时自动 health check，成功则默认开启
from .llm_tool_plugins import anima_generate, enable_anima_tool
ok, err = anima_generate.health_check_sync()
if ok:
    ok2, err2 = anima_generate.fetch_schema_and_knowledge_sync()
    if ok2:
        if enable_anima_tool():
            logger.info("Anima 画图工具已自动开启（health check 通过）")
            config.COMFYUI_ENABLED = True
            save_config()
        else:
            logger.warning("Anima 画图 schema 已缓存但注册跳过（可能重复）")
    else:
        logger.warning(f"Anima 画图知识库加载失败: {err2}")
        config.COMFYUI_ENABLED = False
        save_config()
else:
    logger.info(f"Anima 画图服务离线，跳过自动开启: {err}")
    config.COMFYUI_ENABLED = False
    save_config()

# 读取 OpenAI 配置（优先使用 OPENAI_PROFILES 中的 active profile）
_profiles = config.OPENAI_PROFILES
_active = config.OPENAI_ACTIVE_PROFILE
if _profiles:
    if _active not in _profiles:
        _active = next(iter(_profiles))
    _profile = _profiles[_active]
    api_keys = _profile.get("api_keys", config.OPENAI_API_KEYS)
    _init_config = {
        'model': _profile.get("model", config.CHAT_MODEL),
        'model_mini': _profile.get("model_mini", config.CHAT_MODEL_MINI),
        'max_tokens': _profile.get("max_tokens", config.REPLY_MAX_TOKENS),
        'temperature': _profile.get("temperature", config.CHAT_TEMPERATURE),
        'top_p': _profile.get("top_p", config.CHAT_TOP_P),
        'frequency_penalty': _profile.get("frequency_penalty", config.CHAT_FREQUENCY_PENALTY),
        'presence_penalty': _profile.get("presence_penalty", config.CHAT_PRESENCE_PENALTY),
        'max_summary_tokens': _profile.get("max_summary_tokens", config.CHAT_MAX_SUMMARY_TOKENS),
        'timeout': _profile.get("timeout", config.OPENAI_TIMEOUT),
        'enable_stream': config.LLM_ENABLE_STREAM,
    }
    _init_proxy = _profile.get("proxy") or None
    _init_base_url = _profile.get("base_url", "")
    _init_use_socket_proxy = _profile.get("use_socket_proxy", False)
    _init_multimodal = _profile.get("multimodal", True)
    _init_extra_prompt = _profile.get("extra_prompt", "") or ""
    logger.info(f"使用 OpenAI 配置: {_active}")
else:
    api_keys = config.OPENAI_API_KEYS
    _init_config = {
        'model': config.CHAT_MODEL,
        'model_mini': config.CHAT_MODEL_MINI,
        'max_tokens': config.REPLY_MAX_TOKENS,
        'temperature': config.CHAT_TEMPERATURE,
        'top_p': config.CHAT_TOP_P,
        'frequency_penalty': config.CHAT_FREQUENCY_PENALTY,
        'presence_penalty': config.CHAT_PRESENCE_PENALTY,
        'max_summary_tokens': config.CHAT_MAX_SUMMARY_TOKENS,
        'timeout': config.OPENAI_TIMEOUT,
        'enable_stream': config.LLM_ENABLE_STREAM,
    }
    _init_proxy = config.OPENAI_PROXY_SERVER if config.OPENAI_PROXY_SERVER else None
    _init_base_url = config.OPENAI_BASE_URL if config.OPENAI_BASE_URL else ''
    _init_use_socket_proxy = False
    _init_multimodal = True
    _init_extra_prompt = ""

logger.info(f"共读取到 {len(api_keys)} 个API Key")

""" ======== 初始化对话文本生成器 ======== """
TextGenerator.instance.init(api_keys=api_keys, config=_init_config, proxy=_init_proxy, base_url=_init_base_url, extra_prompt=_init_extra_prompt)
TextGenerator.instance.use_socket_proxy = _init_use_socket_proxy
TextGenerator.instance.multimodal = _init_multimodal


