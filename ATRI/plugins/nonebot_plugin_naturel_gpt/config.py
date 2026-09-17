from typing import Any, ClassVar, Dict, List, Optional
from nonebot.config import Config as NBConfig
from pydantic import BaseModel, Extra
from nonebot import get_driver
from .logger import logger
import yaml
from pathlib import Path
from .persona_loader import load_personas_from_directory

class GlobalConfig(NBConfig, extra=Extra.allow):
    """Plugin Config Here

    注意：extra 必须为 allow。nonebot2 >= 2.4 的 BaseSettings.__init__ 会写入
    `_env_file` 等实例属性，pydantic v1 在 extra != allow 时会拒绝该赋值。
    """
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
    OPENAI_PROFILES: Dict[str, Any] = {}
    """多组 OpenAI 配置：键=profile 名，值=模型配置 dict；另有 `default: <profile 名>` 作默认指针"""
    OPENAI_ACTIVE_PROFILE: str = ""
    """（旧字段）默认配置名；现在优先读 OPENAI_PROFILES 的 `default` 指针，本字段仅作兼容回落"""

    # ---- OPENAI_PROFILES 默认配置指针 ----
    # OPENAI_PROFILES 的键是 profile 名，值有两种形态：
    #   1. dict —— 真实模型配置（api_keys/base_url/model/...）；
    #   2. str  —— 仅 DEFAULT_PROFILE_KEY 这一个键，值是**默认模型配置的名字**（指针）。
    # 指针取代了过去「一个名叫 default 的独立模型配置」的写法：默认模型只维护一份，
    # 想换默认就改指针的值。指针对 dict 形态的 `default`（老配置）保持兼容。
    # ⚠️ 下面这些 helper 必须是 Config 的方法：各模块里 `config` 拿到的是 Config 实例
    # （`from .config import config`），不是 config 模块，写成模块级函数调用会 AttributeError。
    DEFAULT_PROFILE_KEY: ClassVar[str] = "default"
    """OPENAI_PROFILES 内的默认指针键：值为真实 profile 名，指向默认使用的模型配置。"""

    def get_profile_names(self) -> List[str]:
        """全部真实 profile 名（跳过 default 指针这类非 dict 值）"""
        return [name for name, value in (self.OPENAI_PROFILES or {}).items() if isinstance(value, dict)]

    def get_default_profile_name(self) -> str:
        """解析默认 profile 名：default 指针 → 旧字段 OPENAI_ACTIVE_PROFILE → 第一个真实 profile。

        指针值无效（指向不存在的 profile，或指向自己）时自动往后回落，取不到返回空串。"""
        names = self.get_profile_names()
        pointer = (self.OPENAI_PROFILES or {}).get(self.DEFAULT_PROFILE_KEY)
        if isinstance(pointer, str) and pointer in names:
            return pointer
        legacy = self.OPENAI_ACTIVE_PROFILE or ""
        if legacy in names:
            return legacy
        return names[0] if names else ""

    def resolve_profile_name(self, name: str = "") -> str:
        """把会话里存的 profile 名解析成真实 profile 名。

        空值 / 存的是指针键名（如历史数据里的 "default"）/ 配置已改名删名的旧名字，
        一律回落到默认 profile。"""
        names = self.get_profile_names()
        candidate = str(name or "").strip()
        if candidate in names:
            return candidate
        if candidate and candidate != self.DEFAULT_PROFILE_KEY:
            logger.warning(f"profile '{candidate}' 不存在，回落到默认 profile")
        return self.get_default_profile_name()

    def get_profile(self, name: str = "") -> Dict[str, Any]:
        """按名字取真实 profile 配置（指针名/失效名会自动解析）；无可用配置返回空 dict。

        注意：返回值恒为 dict，调用方不需要再判断 OPENAI_PROFILES 里存的是不是指针。"""
        value = (self.OPENAI_PROFILES or {}).get(self.resolve_profile_name(name))
        return dict(value) if isinstance(value, dict) else {}

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
    CHAT_TOP_P: Optional[float] = None
    CHAT_TEMPERATURE: Optional[float] = None
    """温度越高越随机，不定义则不传入API"""
    CHAT_PRESENCE_PENALTY: Optional[float] = None
    """主题重复惩罚，不定义则不传入API"""
    CHAT_FREQUENCY_PENALTY: Optional[float] = None
    """复读惩罚，不定义则不传入API"""

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
    CONTEXT_SUMMARY_TARGET_CHARS: int = 800
    """上下文摘要目标字数（提示词软限制，硬截断为2倍）；profile 的 max_summary_tokens 可覆盖"""
    IMPRESSION_TARGET_CHARS: int = 200
    """用户印象目标字数（提示词软限制，硬截断为2倍）"""
    TOOL_CONTEXT_TOKEN_BUDGET: int
    """工具消息token预算（工具循环内），超出时进入终端轮收尾"""

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
    LLM_MAX_TOTAL_TOOL_CALLS: int
    """单轮总工具调用次数上限"""
    LLM_TOOL_LOOP_MAX_SECONDS: int
    """工具调用循环总耗时上限（秒），超时进入终端收尾轮"""

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
    THINK_LEAK_THRESHOLD: int
    """思考泄漏兜底阈值：思考模式下模型跳过思考标签、把思考混入 content 时，content 字符数超过此值且含双换行，则触发兜底切分，前段视为思考"""
    THINK_LEAK_SHORT_SEGMENT: int
    """兜底切分短段阈值：按双换行分段后，从第一个长度小于此值的段落开始（含）视为真实回复，之前视为思考；所有段落都不短时回退取最后一段"""
    NG_ENABLE_AWAKE_IDENTITIES: bool
    """是否允许自动唤醒其它人格"""

    MULTIMODAL_ENABLE: bool
    """是否允许接收图片作为多模态输入"""
    MULTIMODAL_MAX_IMAGES: int
    """上下文中可见图片总数上限（触发消息自身图片不参与剥离）；超限按最旧优先剥离到一半（滞后回收，减少前缀缓存失效）"""
    MULTIMODAL_IMAGE_FRESH_MINUTES: int
    """图片有效期（分钟），统一适用于历史 / 群聊上下文 / 触发消息；过期判定按 30 分钟量化，整点和半点批量退场并重编号"""

    CONTEXT_BUFFER_SIZE: int
    """旧版非触发消息缓冲区大小（兼容字段；主路径窗口由 CONTEXT_WINDOW_SIZE 和 CONTEXT_COMPRESS_THRESHOLD_RATIO 计算）"""
    CONTEXT_BUFFER_MAX_AGE_MINUTES: int
    """非触发消息缓冲的时间衰减（分钟）：flush 时丢弃更早的条目，但至少保留 CONTEXT_BUFFER_MIN_LINES 条；0 表示不衰减"""
    CONTEXT_BUFFER_MIN_LINES: int
    """非触发消息缓冲时间衰减后至少保留的条数（保证话题连续性）"""

    TAVILY_API_KEY: List[str]
    """Tavily 搜索 API Key 列表，启动时自动选用额度剩余最多的 key"""
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

    MANGA_IDLE_MINUTES: int
    """漫画模式下多少分钟未画图触发自动画图"""
    MANGA_IDLE_ROUNDS: int
    """漫画模式下多少轮对话未画图触发自动画图"""

    NAS_GAME_ROOT_PATH: str
    """NAS Galgame 合集根目录路径"""
    NAS_GAME_UPLOAD_PATH: str
    """NAS 游戏上传目录路径"""
    NAS_GAME_BASE_URL: str
    """NAS Galgame 合集下载基础 URL"""
    NAS_GAME_WHITELIST_GROUPS: List[str]
    """NAS Galgame 合集功能白名单群号"""
    NAS_GAME_SYNC_RECORDS_PATH: str
    """NAS 游戏同步服务记录文件路径（sync_records.json）"""

    UNLOCK_CONTENT_LIMIT: bool
    """解锁内容限制（全局默认值；每群可通过 rg nolimit on/off 独立覆盖，持久化存储）"""

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
    'OPENAI_PROFILES': {},  # 多组模型配置：键=profile 名，值=配置 dict；`default: <profile 名>` 为默认指针
    'OPENAI_ACTIVE_PROFILE': '',  # （旧字段）默认配置名；已由 OPENAI_PROFILES 的 default 指针取代，仅兼容回落
    "REPLY_THROTTLE_TIME": 3,   # 回复间隔节流时间
    "PRESETS": {},
    "DEFAULT_PERSONA": "",
    'IGNORE_PREFIX': '#',   # 忽略前缀 以该前缀开头的消息将不会被处理
    'CHAT_MODEL': "gpt-4o",  # 旧格式兼容，有 OPENAI_PROFILES 时可省略
    'CHAT_MODEL_MINI': "gpt-4o-mini",  # 旧格式兼容
    'CHAT_TOP_P': None,  # 旧格式兼容，不定义则不传入API
    'CHAT_TEMPERATURE': None,  # 旧格式兼容，不定义则不传入API
    'CHAT_PRESENCE_PENALTY': None,  # 旧格式兼容，不定义则不传入API
    'CHAT_FREQUENCY_PENALTY': None,  # 旧格式兼容，不定义则不传入API
    'CHAT_MAX_SUMMARY_TOKENS': 512,  # 旧格式兼容
    'REPLY_MAX_TOKENS': 1024,  # 旧格式兼容
    'CONTEXT_TOKEN_BUDGET': 4096,  # 上下文窗口token预算
    'CONTEXT_WINDOW_SIZE': 16,  # 上下文窗口大小（对话轮数），每轮=1条用户消息+1条回复
    'CONTEXT_SUMMARY_ENABLED': False,  # 是否启用上下文摘要压缩
    'CONTEXT_COMPRESS_THRESHOLD_RATIO': 0.5,  # 压缩触发阈值乘数，溢出超过窗口*此比例才触发摘要生成
    'CONTEXT_SUMMARY_TARGET_CHARS': 800,  # 上下文摘要目标字数（提示词软限制，硬截断为2倍）；profile 的 max_summary_tokens 可覆盖
    'IMPRESSION_TARGET_CHARS': 200,  # 用户印象目标字数（提示词软限制，硬截断为2倍）
    'TOOL_CONTEXT_TOKEN_BUDGET': 16384,  # 工具循环内工具+思考 token 预算，超出时进入终端轮收尾

    'LLM_ENABLE_STREAM': True,
    'LLM_SHOW_REASONING': False,
    'LLM_ENABLE_TOOLS': True,
    'LLM_DISABLED_TOOLS': [],  # 禁用的工具列表，填写工具模块名（如 browse_url、pixiv_search）
    'LLM_MAX_TOOL_ROUNDS': 3,
    'LLM_MAX_TOTAL_TOOL_CALLS': 15,
    'LLM_TOOL_LOOP_MAX_SECONDS': 180,  # 工具调用循环总耗时上限（秒），超时进入终端收尾轮

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
    'THINK_LEAK_THRESHOLD': 150,  # 思考泄漏兜底阈值（字符数），思考模式下 content 超过此值且含双换行则触发兜底切分
    'THINK_LEAK_SHORT_SEGMENT': 50,  # 兜底切分短段阈值（字符数）：分段后从第一个短于该值的段落开始视为真实回复，避免回复开头的短句被误判为思考
    'NG_ENABLE_AWAKE_IDENTITIES': True, # 是否允许自动唤醒其它人格

    'MULTIMODAL_ENABLE': True,
    'MULTIMODAL_MAX_IMAGES': 8,  # 上下文可见图片总数上限，超限按最旧剥离到一半（滞后回收）
    'MULTIMODAL_IMAGE_FRESH_MINUTES': 60,  # 图片统一有效期（分钟），按 30 分钟量化批量过期

    'CONTEXT_BUFFER_SIZE': 10,
    'CONTEXT_BUFFER_MAX_AGE_MINUTES': 15,  # 非触发消息缓冲时间衰减（分钟）
    'CONTEXT_BUFFER_MIN_LINES': 3,  # 时间衰减后至少保留的条数

    'TAVILY_API_KEY': [],
    'BOCHA_API_KEY': '',
    'BOCHA_API_BASE': 'https://api.bochaai.com/v1/web-search',
    'BOCHA_SEARCH_COUNT': 20,
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

    'MANGA_IDLE_MINUTES': 5,
    'MANGA_IDLE_ROUNDS': 5,

    'NAS_GAME_ROOT_PATH': '',
    'NAS_GAME_UPLOAD_PATH': '',
    'NAS_GAME_BASE_URL': '',
    'NAS_GAME_WHITELIST_GROUPS': [],  # NAS Galgame 功能白名单群号，如 ['123456789', '987654321']
    'NAS_GAME_SYNC_RECORDS_PATH': '',  # NAS 游戏同步服务记录文件路径（sync_records.json），为空则不读取同步记录

    'UNLOCK_CONTENT_LIMIT': False,  # 解锁内容限制（全局默认值，每群可通过 rg nolimit on/off 独立覆盖）

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
            config_obj_from_file:Dict = yaml.safe_load(f)
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

            # 向后兼容：如果没有 OPENAI_PROFILES，从旧格式扁平键自动创建 main profile，
            # 并用 default 指针指向它（指针形态取代了过去名叫 default 的独立配置）
            if not config_obj_from_file.get("OPENAI_PROFILES"):
                config_obj_from_file["OPENAI_PROFILES"] = {
                    Config.DEFAULT_PROFILE_KEY: "main",
                    "main": {
                        "api_keys": config_obj_from_file.get("OPENAI_API_KEYS", []),
                        "base_url": config_obj_from_file.get("OPENAI_BASE_URL", ""),
                        "proxy": config_obj_from_file.get("OPENAI_PROXY_SERVER", ""),
                        "timeout": config_obj_from_file.get("OPENAI_TIMEOUT", 60),
                        "model": config_obj_from_file.get("CHAT_MODEL", ""),
                        "model_mini": config_obj_from_file.get("CHAT_MODEL_MINI", ""),
                        "temperature": config_obj_from_file.get("CHAT_TEMPERATURE"),
                        "top_p": config_obj_from_file.get("CHAT_TOP_P"),
                        "max_tokens": config_obj_from_file.get("REPLY_MAX_TOKENS", 4096),
                        "max_summary_tokens": config_obj_from_file.get("CHAT_MAX_SUMMARY_TOKENS", 800),
                        "frequency_penalty": config_obj_from_file.get("CHAT_FREQUENCY_PENALTY"),
                        "presence_penalty": config_obj_from_file.get("CHAT_PRESENCE_PENALTY"),
                        "extra_prompt": "",
                        # 视觉工具：纯文本主模型可委托视觉模型理解图片（仅文档化默认值，读取处用 .get 兜底）
                        "model_vision": "deepseek-flash",
                        # 关闭思考：true 时响应规则注入 /no_think 指令（仅文档化默认值，读取处用 .get 兜底）
                        "no_think": False,
                        # 跨轮历史携带 reasoning_content：true 时持久化历史中的思考字段随请求发送
                        #（仅文档化默认值，读取处用 .get 兜底；provider 400 拒绝时自动剥离重试一次）
                        "keep_reasoning": False,
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
                config_obj_from_file["OPENAI_ACTIVE_PROFILE"] = ""  # 默认由 default 指针决定，旧字段留空
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
    Path(config.NG_DATA_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(config.NG_LOG_PATH).parent.mkdir(parents=True, exist_ok=True)
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
    # 直接替换整个 config 对象，避免 setattr 逐字段覆盖丢失新字段
    config = config_tmp
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
