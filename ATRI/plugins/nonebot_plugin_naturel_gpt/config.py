from typing import Any, Dict, List
from nonebot.config import Config as NBConfig
from pydantic import BaseModel, Extra
from nonebot import get_driver
from .logger import logger
import yaml
from pathlib import Path
from .persona_loader import load_personas_from_directory

class GlobalConfig(NBConfig, extra=Extra.ignore):
    """Plugin Config Here"""
    ng_config_path: str = "config/naturel_gpt_config.yml"
    ng_dev_mode: bool = False

class PresetConfig(BaseModel, extra=Extra.ignore):
    """人格预设配置项"""
    preset_key:str
    is_locked:bool = False
    is_default:bool = False
    is_only_private:bool = False
    """此预设是否仅限私聊"""
    bot_self_introl:str = ''

class Config(BaseModel, extra=Extra.ignore):
    """ng 配置数据，默认保存为 naturel_gpt_config.yml"""
    OPENAI_API_KEYS: List[str] = []
    """OpenAI API Key 列表（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    OPENAI_TIMEOUT: int = 60
    """OpenAI 请求超时时间（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    OPENAI_PROXY_SERVER: str = ''
    """请求OpenAI的代理服务器（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    OPENAI_BASE_URL: str = 'https://api.openai.com/v1'
    """请求OpenAI的基础URL（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    OPENAI_PROFILES: Dict[str, Dict[str, Any]] = {}
    """多组 OpenAI 配置，每组包含 api_keys/base_url/proxy/timeout/model/extra_prompt 等"""
    OPENAI_ACTIVE_PROFILE: str = ""
    """当前激活的配置名；为空时使用第一个 profile"""
    REPLY_THROTTLE_TIME: int
    """回复间隔节流时间"""
    PRESETS: Dict[str, PresetConfig] = {}
    """运行时动态人格预设；不再从配置文件手写人格来源读取"""
    DEFAULT_PERSONA: str
    """默认人格名；为空或不存在时使用首个已加载人格"""
    IGNORE_PREFIX: str
    """忽略前缀 以该前缀开头的消息将不会被处理"""
    CHAT_MODEL: str = ''
    """OpenAI 模型（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    CHAT_MODEL_MINI: str = ''
    """OpenAI MINI模型（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    CHAT_TOP_P: float = 0.95
    CHAT_TEMPERATURE: float = 0.6
    """温度越高越随机"""
    CHAT_PRESENCE_PENALTY: float = 0.0
    """主题重复惩罚"""
    CHAT_FREQUENCY_PENALTY: float = 0.0
    """复读惩罚"""

    CHAT_MAX_SUMMARY_TOKENS: int = 800
    """单次总结最大token数（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    REPLY_MAX_TOKENS: int = 4096
    """单次回复最大token数（旧格式兼容，有 OPENAI_PROFILES 时可省略）"""
    CONTEXT_TOKEN_BUDGET: int
    """上下文窗口token预算，控制prompt最大token数"""
    CONTEXT_WINDOW_SIZE: int
    """上下文窗口大小（对话轮数），每轮=1条用户消息+1条回复"""
    CONTEXT_SUMMARY_ENABLED: bool
    """是否启用上下文摘要压缩，启用后超窗口的历史会被压缩为摘要"""
    CONTEXT_COMPRESS_THRESHOLD_RATIO: float
    """压缩触发阈值乘数，溢出超过窗口*此比例才触发摘要生成，默认0.5"""
    TOOL_CONTEXT_TOKEN_BUDGET: int
    """工具消息token预算，超出时全部抛弃"""
    TOOL_CONTEXT_MODE: int
    """工具上下文模式: 1=完整工具+思考, 2=仅思考, 3=仅工具调用摘要"""

    LLM_ENABLE_STREAM: bool
    """是否使用流式响应"""
    LLM_SHOW_REASONING: bool
    """是否把模型 reasoning_content 发送到聊天中"""
    LLM_ENABLE_TOOLS: bool
    """是否启用原生工具调用"""
    LLM_DISABLED_TOOLS: List[str]
    """禁用的工具列表，填写工具模块名（如 browse_url、pixiv_search）"""
    LLM_MAX_TOOL_ROUNDS: int
    """单轮回复最多工具调用轮数"""

    REPLY_ON_NAME_MENTION_PROBABILITY: float
    """是否在被提及时回复"""
    REPLY_ON_AT: bool
    """是否在被at时回复"""
    REPLY_ON_WELCOME: bool
    """是否在新成员加入时回复"""

    USER_MEMORY_SUMMARY_THRESHOLD: int
    """用户记忆阈值"""

    NG_DATA_PICKLE: bool
    """是否强制使用pickle，默认使用json"""
    NG_DATA_PATH: str
    """数据文件目录"""
    NG_LOG_PATH: str
    """日志文件目录"""

    ADMIN_USERID: List[str]
    """管理员QQ号"""
    FORBIDDEN_USERS: List[str]
    """拒绝回应的QQ号"""

    FORBIDDEN_GROUPS: List[str]
    """拒绝回应的群号"""

    WORD_FOR_WAKE_UP: List[str]
    """自定义触发词"""
    WORD_FOR_FORBIDDEN: List[str]
    """自定义禁止触发词"""

    RANDOM_CHAT_PROBABILITY: float
    """随机聊天概率"""

    NG_MSG_PRIORITY: int
    """消息响应优先级"""
    NG_BLOCK_OTHERS: bool
    """是否阻止其他插件响应"""
    NG_TO_ME: bool
    """响应命令是否需要@bot"""
    ENABLE_COMMAND_TO_IMG: bool
    """是否将rg相关指令转换为图片"""
    ENABLE_MSG_TO_IMG: bool
    """是否将机器人的回复转换成图片"""
    IMG_MAX_WIDTH: int
    """生成图片的最大宽度"""

    MEMORY_ACTIVE: bool
    """是否启用记忆功能"""
    MEMORY_MAX_LENGTH: int
    """记忆最大条数"""
    NG_ENABLE_MSG_SPLIT: bool
    """是否启用消息分割"""
    REPLY_SEGMENT_INTERVAL: float
    """分段消息发送最短间隔秒数"""
    REPLY_MAX_SEGMENTS: int
    """单次回复最多分段数，最后一段会接收剩余流式内容"""
    NG_ENABLE_AWAKE_IDENTITIES: bool
    """是否允许自动唤醒其它人格"""

    MULTIMODAL_ENABLE: bool
    """是否允许接收图片作为多模态输入"""
    MULTIMODAL_MAX_MESSAGES_WITH_IMAGES: int
    """最多保留几条消息中的图片"""
    MULTIMODAL_IMAGE_FRESH_MINUTES: int
    """图片有效期（分钟），超过此时间的图片不再作为上下文"""

    CONTEXT_BUFFER_SIZE: int
    """非触发消息缓冲区大小（消息条数），同时控制图片视野窗口"""

    BOCHA_API_KEY: str
    BOCHA_API_BASE: str
    BOCHA_SEARCH_COUNT: int
    WEB_FETCH_TIMEOUT: int
    WEB_FETCH_MAX_CHARS: int
    PLAYWRIGHT_TIMEOUT: int
    BANGUMI_ACCESS_TOKEN: str
    TOOL_PROXY: str
    """工具代理地址，如 socks5://127.0.0.1:7789，为空则不使用代理"""
    PIXIV_R18: int
    """Pixiv 搜索 R18 设置：0=关闭，1=开启，2=仅R18"""
    PIXIV_PIC_PROXY: str
    """Pixiv 图片反代地址，为空则使用原始地址"""
    PIXIV_EXCLUDE_AI: bool
    """是否排除 AI 生成的图片"""

    COMFYUI_BASE_URL: str
    """ComfyUI Anima 画图服务地址"""
    COMFYUI_ENABLED: bool
    """ComfyUI Anima 画图是否开启，启动时自动 health check 后设置"""

    NAS_GAME_ROOT_PATH: str
    """NAS Galgame 合集根目录路径"""
    NAS_GAME_UPLOAD_PATH: str
    """NAS 游戏上传目录路径"""
    NAS_GAME_BASE_URL: str
    """NAS Galgame 合集下载基础 URL"""
    NAS_GAME_WHITELIST_GROUPS: List[str]
    """NAS Galgame 合集功能白名单群号"""

    UNLOCK_CONTENT_LIMIT: bool
    """解锁内容限制"""

    GROUP_CARD:bool
    """优先读取群名片"""

    NG_CHECK_USER_NAME_HYPHEN:bool # 如果用户名中包含连字符，ChatGPT会将前半部分识别为名字，但一般情况下后半部分才是我们想被称呼的名字, eg. 策划-李华
    """检查用户名中的连字符"""

    VERSION:str
    """配置文件版本信息"""
    
    DEBUG_LEVEL: int
    """debug level, [0, 1, 2, 3], 0 为关闭，等级越高debug信息越详细"""

# 配置文件模板(把全部默认值写到Config定义里比较乱，因此保留此默认值对象,作为真实的默认值)
CONFIG_TEMPLATE = {
    "OPENAI_API_KEYS": ['sk-xxxxxxxxxxxxx'],  # OpenAI API Key（旧格式兼容，有 OPENAI_PROFILES 时可省略）
    "OPENAI_TIMEOUT": 60,   # OpenAI 请求超时时间（旧格式兼容）
    'OPENAI_PROXY_SERVER': '',  # 请求OpenAI的代理服务器（旧格式兼容）
    'OPENAI_BASE_URL': 'https://api.openai.com/v1',  # 请求OpenAI的基础URL（旧格式兼容）
    'OPENAI_PROFILES': {},  # 多组 OpenAI 配置；为空时自动从旧格式扁平键创建 default profile
    'OPENAI_ACTIVE_PROFILE': '',  # 当前激活的配置名；为空时使用第一个 profile
    "REPLY_THROTTLE_TIME": 3,   # 回复间隔节流时间
    "PRESETS": {},
    "DEFAULT_PERSONA": "",
    'IGNORE_PREFIX': '#',   # 忽略前缀 以该前缀开头的消息将不会被处理
    'CHAT_MODEL': "gpt-4o",  # 旧格式兼容，有 OPENAI_PROFILES 时可省略
    'CHAT_MODEL_MINI': "gpt-4o-mini",  # 旧格式兼容
    'CHAT_TOP_P': 1,  # 旧格式兼容
    'CHAT_TEMPERATURE': 0.4,  # 旧格式兼容
    'CHAT_PRESENCE_PENALTY': 0.4,  # 旧格式兼容
    'CHAT_FREQUENCY_PENALTY': 0.4,  # 旧格式兼容
    'CHAT_MAX_SUMMARY_TOKENS': 512,  # 旧格式兼容
    'REPLY_MAX_TOKENS': 1024,  # 旧格式兼容
    'CONTEXT_TOKEN_BUDGET': 4096,  # 上下文窗口token预算
    'CONTEXT_WINDOW_SIZE': 16,  # 上下文窗口大小（对话轮数），每轮=1条用户消息+1条回复
    'CONTEXT_SUMMARY_ENABLED': False,  # 是否启用上下文摘要压缩
    'CONTEXT_COMPRESS_THRESHOLD_RATIO': 0.5,  # 压缩触发阈值乘数，溢出超过窗口*此比例才触发摘要生成
    'TOOL_CONTEXT_TOKEN_BUDGET': 8196,  # 工具消息token预算（含思考），超出时从旧到新逐组去除
    'TOOL_CONTEXT_MODE': 3,  # 工具上下文模式: 1=完整工具+思考, 2=仅思考, 3=仅工具调用摘要

    'LLM_ENABLE_STREAM': True,
    'LLM_SHOW_REASONING': False,
    'LLM_ENABLE_TOOLS': True,
    'LLM_DISABLED_TOOLS': [],  # 禁用的工具列表，填写工具模块名（如 browse_url、pixiv_search）
    'LLM_MAX_TOOL_ROUNDS': 3,

    'REPLY_ON_NAME_MENTION_PROBABILITY': 0,  # 被提及时回复概率
    'REPLY_ON_AT': True,            # 是否在被at时回复
    'REPLY_ON_WELCOME': True,       # 是否在新成员加入时回复

    'USER_MEMORY_SUMMARY_THRESHOLD': 12,  # 用户记忆阈值

    'NG_DATA_PICKLE': False,  # 强制使用pickle
    'NG_DATA_PATH': "./data/naturel_gpt/",  # 数据文件目录
    'NG_LOG_PATH': "./data/naturel_gpt/logs/",  # 扩展目录

    'ADMIN_USERID': ['123456'],  # 管理员QQ号
    'FORBIDDEN_USERS': ['123456'],   # 拒绝回应的QQ号
    'FORBIDDEN_GROUPS': ['123456'],   # 拒绝回应的群号

    'WORD_FOR_WAKE_UP': [],  # 自定义触发词
    'WORD_FOR_FORBIDDEN': [],  # 自定义禁止触发词

    'RANDOM_CHAT_PROBABILITY': 0,   # 随机聊天概率

    'NG_MSG_PRIORITY': 99,       # 消息响应优先级
    'NG_BLOCK_OTHERS': False,    # 是否阻止其他插件响应
    'NG_TO_ME':False,           # 响应命令是否需要@bot
    'ENABLE_COMMAND_TO_IMG': True,    #是否将rg相关指令转换为图片
    'ENABLE_MSG_TO_IMG': False,     #是否将机器人的回复转换成图片
    'IMG_MAX_WIDTH': 800,

    'MEMORY_ACTIVE': True,  # 是否启用记忆功能
    'MEMORY_MAX_LENGTH': 16,  # 记忆最大条数
    'NG_ENABLE_MSG_SPLIT': True,   # 是否启用消息分割
    'REPLY_SEGMENT_INTERVAL': 1.0,
    'REPLY_MAX_SEGMENTS': 5,
    'NG_ENABLE_AWAKE_IDENTITIES': True, # 是否允许自动唤醒其它人格

    'MULTIMODAL_ENABLE': True,
    'MULTIMODAL_MAX_MESSAGES_WITH_IMAGES': 3,
    'MULTIMODAL_IMAGE_FRESH_MINUTES': 120,

    'CONTEXT_BUFFER_SIZE': 10,

    'BOCHA_API_KEY': '',
    'BOCHA_API_BASE': 'https://api.bochaai.com/v1/web-search',
    'BOCHA_SEARCH_COUNT': 5,
    'WEB_FETCH_TIMEOUT': 20,
    'WEB_FETCH_MAX_CHARS': 6000,
    'PLAYWRIGHT_TIMEOUT': 20,
    'BANGUMI_ACCESS_TOKEN': '',
    'TOOL_PROXY': '',
    'PIXIV_R18': 0,
    'PIXIV_PIC_PROXY': '',
    'PIXIV_EXCLUDE_AI': True,

    'COMFYUI_BASE_URL': 'http://127.0.0.1:8188',
    'COMFYUI_ENABLED': False,

    'NAS_GAME_ROOT_PATH': '',
    'NAS_GAME_UPLOAD_PATH': '',
    'NAS_GAME_BASE_URL': '',
    'NAS_GAME_WHITELIST_GROUPS': ['149378291', '726905061', '620260076'],

    'UNLOCK_CONTENT_LIMIT': False,  # 解锁内容限制

    'GROUP_CARD':True,
    'NG_CHECK_USER_NAME_HYPHEN': False,  # 检查用户名中的连字符

    'VERSION':'1.0',
    'DEBUG_LEVEL': 0,  # debug level, [0, 1, 2], 0 为关闭，等级越高debug信息越详细
}

driver = get_driver()
global_config = GlobalConfig.parse_obj(driver.config)
config_path = global_config.ng_config_path
config:Config = None # type: ignore

def get_config() ->Config:
    """获取config数据（为了能够reload建议使用此函数获取对象）"""
    return config


def get_persona_dir() -> Path:
    """人格目录固定为 naturel_gpt_config.yml 所在目录下的 personas 子目录。"""
    return Path(config_path).resolve().parent / "personas"


def _apply_default_persona(personas: Dict[str, PresetConfig], default_persona: str) -> None:
    """Mark one loaded persona as default, falling back to the first loaded persona."""
    if not personas:
        return
    selected = default_persona if default_persona in personas else next(iter(personas))
    for preset_key, preset in personas.items():
        preset.is_default = preset_key == selected


def load_dynamic_persona_presets() -> Dict[str, PresetConfig]:
    """从配置文件同级的 personas 子目录动态加载 md/skill 人格。"""
    persona_presets: Dict[str, PresetConfig] = {}
    for preset_key, persona_text in load_personas_from_directory(str(get_persona_dir())).items():
        persona_presets[preset_key] = PresetConfig(
            preset_key=preset_key,
            is_locked=False,
            is_default=False,
            is_only_private=False,
            bot_self_introl=persona_text,
        )
    if config:
        _apply_default_persona(persona_presets, config.DEFAULT_PERSONA)
    return persona_presets


def reload_dynamic_personas() -> int:
    """动态刷新配置文件同级 personas 子目录人格到全局 config.PRESETS。"""
    if not config:
        return 0
    persona_presets = load_dynamic_persona_presets()
    config.PRESETS.clear()
    for preset_key, preset in persona_presets.items():
        config.PRESETS[preset_key] = preset
    if not config.PRESETS:
        config.PRESETS["default"] = PresetConfig(
            preset_key="default",
            is_locked=False,
            is_default=True,
            is_only_private=False,
            bot_self_introl="你是一个自然参与群聊的聊天助手。回复要简短、直接、像真实人类一样。",
        )
    return len(persona_presets)

def _load_config_obj_from_file()->Config:
    """从配置文件加载Config对象"""
    # 读取配置文件
    with open(config_path, 'r', encoding='utf-8') as f:
        try:
            config_obj_from_file:Dict = yaml.load(f, Loader=yaml.FullLoader)
            for k in CONFIG_TEMPLATE.keys():
                if not k in config_obj_from_file.keys():
                    config_obj_from_file[k] = CONFIG_TEMPLATE[k]
                    if k not in _LEGACY_FIELDS or not config_obj_from_file.get("OPENAI_PROFILES"):
                        logger.info(f"Naturel GPT 配置文件缺少 {k} 项，将使用默认值")

            # 人格来源固定为 naturel_gpt_config.yml 同级的 personas 子目录。
            # 配置文件中的 PRESETS 不再作为输入来源，保留字段仅用于运行时承载动态人格。
            config_obj_from_file["PRESETS"] = {}

            for preset_key, persona_text in load_personas_from_directory(str(get_persona_dir())).items():
                config_obj_from_file["PRESETS"][preset_key] = {
                    "preset_key": preset_key,
                    "is_locked": False,
                    "is_default": False,
                    "is_only_private": False,
                    "bot_self_introl": persona_text,
                }
            if config_obj_from_file["PRESETS"]:
                selected_persona = config_obj_from_file.get("DEFAULT_PERSONA", "")
                if selected_persona not in config_obj_from_file["PRESETS"]:
                    selected_persona = next(iter(config_obj_from_file["PRESETS"]))
                for preset_key, preset in config_obj_from_file["PRESETS"].items():
                    preset["is_default"] = preset_key == selected_persona
            if not config_obj_from_file["PRESETS"]:
                config_obj_from_file["PRESETS"]["default"] = {
                    "preset_key": "default",
                    "is_locked": False,
                    "is_default": True,
                    "is_only_private": False,
                    "bot_self_introl": "你是一个自然参与群聊的聊天助手。回复要简短、直接、像真实人类一样。",
                }

            # 向后兼容：如果没有 OPENAI_PROFILES，从旧格式扁平键自动创建 default profile
            if not config_obj_from_file.get("OPENAI_PROFILES"):
                config_obj_from_file["OPENAI_PROFILES"] = {
                    "default": {
                        "api_keys": config_obj_from_file.get("OPENAI_API_KEYS", []),
                        "base_url": config_obj_from_file.get("OPENAI_BASE_URL", ""),
                        "proxy": config_obj_from_file.get("OPENAI_PROXY_SERVER", ""),
                        "timeout": config_obj_from_file.get("OPENAI_TIMEOUT", 60),
                        "model": config_obj_from_file.get("CHAT_MODEL", ""),
                        "model_mini": config_obj_from_file.get("CHAT_MODEL_MINI", ""),
                        "temperature": config_obj_from_file.get("CHAT_TEMPERATURE", 0.6),
                        "top_p": config_obj_from_file.get("CHAT_TOP_P", 0.95),
                        "max_tokens": config_obj_from_file.get("REPLY_MAX_TOKENS", 4096),
                        "max_summary_tokens": config_obj_from_file.get("CHAT_MAX_SUMMARY_TOKENS", 800),
                        "frequency_penalty": config_obj_from_file.get("CHAT_FREQUENCY_PENALTY", 0.0),
                        "presence_penalty": config_obj_from_file.get("CHAT_PRESENCE_PENALTY", 0.0),
                        "extra_prompt": "",
                    },
                    "kimi": {
                        "api_keys": config_obj_from_file.get("OPENAI_API_KEYS", []),
                        "base_url": "https://api.moonshot.cn/v1",
                        "proxy": "",
                        "timeout": 120,
                        "model": "kimi-k2.5",
                        "model_mini": "kimi-k2.5",
                        "temperature": 0.4,
                        "top_p": 0.95,
                        "max_tokens": 4096,
                        "max_summary_tokens": 800,
                        "frequency_penalty": 0.0,
                        "presence_penalty": 0.0,
                        "extra_prompt": (
                            "工具调用要果断，只要用户需求可能涉及搜索、资料查询或图片创作，立刻调用对应工具，不要犹豫或先文字试探。\n"
                            "不要复述用户的话，不要重复自己已经表达过的观点，每句话只出现一次。\n"
                            "减少推理和分析过程，直接输出结论和最终回答，不要展示思考步骤。"
                        ),
                    },
                }
                config_obj_from_file["OPENAI_ACTIVE_PROFILE"] = "default"
        except Exception as e:
            logger.error(f"Naturel GPT 配置文件读取失败，请检查配置文件填写是否符合yml文件格式规范，错误信息：{e}")
            raise e

        config_obj = Config.parse_obj(config_obj_from_file)
    return config_obj

# 旧格式兼容字段，有 OPENAI_PROFILES 时不需要写入 YAML
_LEGACY_FIELDS = {
    "OPENAI_API_KEYS", "OPENAI_TIMEOUT", "OPENAI_PROXY_SERVER", "OPENAI_BASE_URL",
    "CHAT_MODEL", "CHAT_MODEL_MINI", "CHAT_TOP_P", "CHAT_TEMPERATURE",
    "CHAT_PRESENCE_PENALTY", "CHAT_FREQUENCY_PENALTY",
    "CHAT_MAX_SUMMARY_TOKENS", "REPLY_MAX_TOKENS",
}


def save_config():
    # 检查数据文件夹目录、日志目录是否存在 不存在则创建
    if not Path(config.NG_DATA_PATH[:-1]).exists():
        Path(config.NG_DATA_PATH[:-1]).mkdir(parents=True)
    if not Path(config.NG_LOG_PATH[:-1]).exists():
        Path(config.NG_LOG_PATH[:-1]).mkdir(parents=True)
    get_persona_dir().mkdir(parents=True, exist_ok=True)

    # 保存配置文件（有 OPENAI_PROFILES 时剔除旧格式兼容字段）
    with open(config_path, 'w', encoding='utf-8') as f:
        config_dict = config.dict()
        config_dict["PRESETS"] = {}
        if config_dict.get("OPENAI_PROFILES"):
            for field in _LEGACY_FIELDS:
                config_dict.pop(field, None)
        yaml.dump(config_dict, f, allow_unicode=True, sort_keys=False)

def load_config_from_file_then_save():
    """加载配置文件，然后保存回文件"""
    global config
    config = _load_config_obj_from_file()

    save_config()
    logger.info('Naturel GPT 配置文件加载成功')

def reload_config():
    """重载配置文件"""
    global config
    assert(config)

    config_tmp = _load_config_obj_from_file()
    for k in config.dict():
        setattr(config, k, getattr(config_tmp,k))
    logger.info(f'Naturel GPT 配置文件重载成功! ver:{config.VERSION}')

# 检查config文件夹是否存在 不存在则创建
if not Path("config").exists():
    Path("config").mkdir()

if global_config.ng_dev_mode:  # 开发模式下不读取原配置文件，直接使用模板覆盖原配置文件
    with open(config_path, 'w', encoding='utf-8') as f:
        yaml.dump(CONFIG_TEMPLATE, f, allow_unicode=True)
else:
    # 检查配置文件是否存在 不存在则创建
    if not Path(config_path).exists():
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(CONFIG_TEMPLATE, f, allow_unicode=True)
            logger.info('Naturel GPT 配置文件创建成功')

# 加载配置文件
load_config_from_file_then_save()
