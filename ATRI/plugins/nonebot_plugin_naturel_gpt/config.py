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
    OPENAI_API_KEYS: List[str]
    """OpenAI API Key 列表"""
    OPENAI_TIMEOUT: int
    """OpenAI 请求超时时间"""
    OPENAI_PROXY_SERVER: str
    """请求OpenAI的代理服务器"""
    OPENAI_BASE_URL: str
    """请求OpenAI的基础URL"""
    REPLY_THROTTLE_TIME: int
    """回复间隔节流时间"""
    PRESETS: Dict[str, PresetConfig] = {}
    """运行时动态人格预设；不再从配置文件手写人格来源读取"""
    DEFAULT_PERSONA: str
    """默认人格名；为空或不存在时使用首个已加载人格"""
    IGNORE_PREFIX: str
    """忽略前缀 以该前缀开头的消息将不会被处理"""
    CHAT_MODEL: str
    """OpenAI 模型"""
    CHAT_MODEL_MINI: str
    """OpenAI MINI模型"""
    CHAT_TOP_P: float
    CHAT_TEMPERATURE: float
    """温度越高越随机"""
    CHAT_PRESENCE_PENALTY: float
    """主题重复惩罚"""
    CHAT_FREQUENCY_PENALTY: float
    """复读惩罚"""

    CHAT_HISTORY_MAX_TOKENS: int
    """上下文聊天记录最大token数"""
    CHAT_MAX_SUMMARY_TOKENS: int
    """单次总结最大token数"""
    REPLY_MAX_TOKENS: int
    """单次回复最大token数"""
    REQ_MAX_TOKENS: int
    """单次请求最大token数"""

    LLM_ENABLE_STREAM: bool
    """是否使用流式响应"""
    LLM_SHOW_REASONING: bool
    """是否把模型 reasoning_content 发送到聊天中"""
    LLM_ENABLE_TOOLS: bool
    """是否启用原生工具调用"""
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

    CHAT_ENABLE_RECORD_ORTHER: bool
    """是否记录其他人的对话"""
    CHAT_ENABLE_SUMMARY_CHAT: bool
    """是否启用总结对话"""
    CHAT_MEMORY_SHORT_LENGTH: int
    """短期对话记忆长度"""
    CHAT_MEMORY_MAX_LENGTH: int
    """长期对话记忆长度"""
    CHAT_SUMMARY_INTERVAL: int
    """长期对话记忆间隔"""

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
    MEMORY_ENHANCE_THRESHOLD: float
    """记忆强化阈值"""

    NG_MAX_RESPONSE_PER_MSG: int
    """每条消息最大响应次数"""
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
    MULTIMODAL_HISTORY_LENGTH: int
    """多模态聊天记录视野长度"""
    MULTIMODAL_MAX_MESSAGES_WITH_IMAGES: int
    """最多保留几条消息中的图片"""

    BOCHA_API_KEY: str
    BOCHA_API_BASE: str
    BOCHA_SEARCH_COUNT: int
    WEB_FETCH_TIMEOUT: int
    WEB_FETCH_MAX_CHARS: int
    PLAYWRIGHT_TIMEOUT: int
    LLM_TOOL_LOLICON_CONFIG: Dict[str, Any]

    UNLOCK_CONTENT_LIMIT: bool
    """解锁内容限制"""

    GROUP_CARD:bool
    """优先读取群名片"""

    NG_CHECK_USER_NAME_HYPHEN:bool # 如果用户名中包含连字符，ChatGPT会将前半部分识别为名字，但一般情况下后半部分才是我们想被称呼的名字, eg. 策划-李华
    """检查用户名中的连字符"""

    ENABLE_MC_CONNECT: bool
    """是否启用MC服务器连接"""

    MC_COMMAND_PREFIX: List[str]
    """MC服务器人格指令前缀"""

    MC_RCON_HOST: str
    """MC服务器RCON地址"""

    MC_RCON_PORT: int
    """MC服务器RCON端口"""

    MC_RCON_PASSWORD: str
    """MC服务器RCON密码"""

    VERSION:str
    """配置文件版本信息"""
    
    DEBUG_LEVEL: int
    """debug level, [0, 1, 2, 3], 0 为关闭，等级越高debug信息越详细"""

# 配置文件模板(把全部默认值写到Config定义里比较乱，因此保留此默认值对象,作为真实的默认值)
CONFIG_TEMPLATE = {
    "OPENAI_API_KEYS": [    # OpenAI API Key 列表
        'sk-xxxxxxxxxxxxx',
        'sk-xxxxxxxxxxxxx',
    ],
    "OPENAI_TIMEOUT": 60,   # OpenAI 请求超时时间
    'OPENAI_PROXY_SERVER': '',  # 请求OpenAI的代理服务器
    'OPENAI_BASE_URL': 'https://api.openai.com/v1',      # 请求OpenAI的基础URL
    "REPLY_THROTTLE_TIME": 3,   # 回复间隔节流时间
    "PRESETS": {},
    "DEFAULT_PERSONA": "",
    'IGNORE_PREFIX': '#',   # 忽略前缀 以该前缀开头的消息将不会被处理
    'CHAT_MODEL': "gpt-4o",
    'CHAT_MODEL_MINI': "gpt-4o-mini",
    'CHAT_TOP_P': 1,
    'CHAT_TEMPERATURE': 0.4,    # 温度越高越随机
    'CHAT_PRESENCE_PENALTY': 0.4,   # 主题重复惩罚
    'CHAT_FREQUENCY_PENALTY': 0.4,  # 复读惩罚

    'CHAT_HISTORY_MAX_TOKENS': 2048,    # 上下文聊天记录最大token数
    'CHAT_MAX_SUMMARY_TOKENS': 512,   # 单次总结最大token数
    'REPLY_MAX_TOKENS': 1024,   # 单次回复最大token数
    'REQ_MAX_TOKENS': 3072,  # 单次请求最大token数

    'LLM_ENABLE_STREAM': True,
    'LLM_SHOW_REASONING': False,
    'LLM_ENABLE_TOOLS': True,
    'LLM_MAX_TOOL_ROUNDS': 3,

    'REPLY_ON_NAME_MENTION_PROBABILITY': 0,  # 被提及时回复概率
    'REPLY_ON_AT': True,            # 是否在被at时回复
    'REPLY_ON_WELCOME': True,       # 是否在新成员加入时回复

    'USER_MEMORY_SUMMARY_THRESHOLD': 12,  # 用户记忆阈值

    'CHAT_ENABLE_RECORD_ORTHER': True,  # 是否记录其他人的对话
    'CHAT_ENABLE_SUMMARY_CHAT': False,   # 是否启用总结对话
    'CHAT_MEMORY_SHORT_LENGTH': 8,  # 短期对话记忆长度
    'CHAT_MEMORY_MAX_LENGTH': 16,   # 长期对话记忆长度
    'CHAT_SUMMARY_INTERVAL': 10,  # 长期对话记忆间隔

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
    'MEMORY_ENHANCE_THRESHOLD': 0.6,  # 记忆强化阈值

    'NG_MAX_RESPONSE_PER_MSG': 5,  # 每条消息最大响应次数
    'NG_ENABLE_MSG_SPLIT': True,   # 是否启用消息分割
    'REPLY_SEGMENT_INTERVAL': 1.0,
    'REPLY_MAX_SEGMENTS': 5,
    'NG_ENABLE_AWAKE_IDENTITIES': True, # 是否允许自动唤醒其它人格

    'MULTIMODAL_ENABLE': True,
    'MULTIMODAL_HISTORY_LENGTH': 4,
    'MULTIMODAL_MAX_MESSAGES_WITH_IMAGES': 2,

    'BOCHA_API_KEY': '',
    'BOCHA_API_BASE': 'https://api.bochaai.com/v1/web-search',
    'BOCHA_SEARCH_COUNT': 5,
    'WEB_FETCH_TIMEOUT': 20,
    'WEB_FETCH_MAX_CHARS': 6000,
    'PLAYWRIGHT_TIMEOUT': 20,
    'LLM_TOOL_LOLICON_CONFIG': {
        'proxy': None,
        'r18': 0,
        'pic_proxy': None,
        'exclude_ai': True,
    },

    'UNLOCK_CONTENT_LIMIT': False,  # 解锁内容限制

    'GROUP_CARD':True,
    'NG_CHECK_USER_NAME_HYPHEN': False, # 检查用户名中的连字符

    'ENABLE_MC_CONNECT': False,  # 是否启用MC服务器
    'MC_COMMAND_PREFIX': ['!', '！'],  # MC服务器指令前缀
    'MC_RCON_HOST': '127.0.0.1',  # MC服务器RCON地址
    'MC_RCON_PORT': 25575,  # MC服务器RCON端口
    'MC_RCON_PASSWORD': '',  # MC服务器RCON密码

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
        except Exception as e:
            logger.error(f"Naturel GPT 配置文件读取失败，请检查配置文件填写是否符合yml文件格式规范，错误信息：{e}")
            raise e

        config_obj = Config.parse_obj(config_obj_from_file)
    return config_obj

def save_config():
    # 检查数据文件夹目录、日志目录是否存在 不存在则创建
    if not Path(config.NG_DATA_PATH[:-1]).exists():
        Path(config.NG_DATA_PATH[:-1]).mkdir(parents=True)
    if not Path(config.NG_LOG_PATH[:-1]).exists():
        Path(config.NG_LOG_PATH[:-1]).mkdir(parents=True)
    get_persona_dir().mkdir(parents=True, exist_ok=True)

    # 保存配置文件
    with open(config_path, 'w', encoding='utf-8') as f:
        config_dict = config.dict()
        config_dict["PRESETS"] = {}
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
