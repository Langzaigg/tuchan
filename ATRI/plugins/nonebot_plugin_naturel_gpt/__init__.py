from typing import Awaitable, Callable, Optional, Tuple
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


def set_permission_check_func(callback:Callable[[Matcher, Event, Bot, str, str], Awaitable[Tuple[bool,Optional[str]]]]):
    """设置Matcher的权限检查函数"""
    matcher.permission_check_func = callback

# 设置默认权限检查函数，有需求时可以覆盖
set_permission_check_func(utils.default_permission_check_func)

""" ======== 读取历史记忆数据 ======== """
PersistentDataManager.instance.load_from_file()
ChatManager.instance.create_all_chat_object() # 启动时创建所有的已有Chat对象，以便被 -all 相关指令控制

# 条件加载工具（如博查搜索需配置 key 才注册）
init_tools(config)

# 读取ApiKeys
api_keys = config.OPENAI_API_KEYS
logger.info(f"共读取到 {len(api_keys)} 个API Key")

""" ======== 初始化对话文本生成器 ======== """
TextGenerator.instance.init(api_keys=api_keys, config={
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
}, 
proxy=config.OPENAI_PROXY_SERVER if config.OPENAI_PROXY_SERVER else None, # 代理服务器配置
base_url=config.OPENAI_BASE_URL if config.OPENAI_BASE_URL else '', # OpenAI API的base_url
)


