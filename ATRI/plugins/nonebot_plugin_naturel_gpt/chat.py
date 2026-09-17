"""会话核心模块 - 负责会话管理、属性定义和基本操作"""

import time
from typing import Any, Dict, List, Optional, Tuple

from .logger import logger
from .config import config, PresetConfig, get_persona_dir
from .openai_func import TextGenerator
from .persistent_data_manager import ImpressionData, ChatData, PresetData, ChatMessageData, PersistentDataManager
from .persona_loader import load_personas_from_directory

# 导入 Mixin 类
from .chat_memory import ChatMemoryMixin
from .chat_summary import ChatSummaryMixin
from .chat_history import ChatHistoryMixin
from .chat_prompt import ChatPromptMixin


class Chat(ChatMemoryMixin, ChatSummaryMixin, ChatHistoryMixin, ChatPromptMixin):
    """ ======== 定义会话类 ======== """
    _chat_data: ChatData         # 此chat_key关联的聊天数据
    _preset_key = ''             # 预设标识
    _last_msg_time = 0           # 上次对话时间
    _last_send_time = 0          # 上次发送时间
    _last_gen_time = 0           # 上次生成对话时间
    is_insilence = False         # 是否处于沉默状态
    chat_attitude = 0            # 对话态度
    silence_time = 0             # 沉默时长

    def __init__(self, chat_data: ChatData, preset_key: str = ''):
        if not isinstance(chat_data, ChatData):
            raise Exception(f'chat_data 参数不是ChatData类型,实际类型为:{type(chat_data).__name__}')
        self._chat_data = chat_data  # 当前对话关联的数据
        # 实例变量初始化（避免类变量共享状态）
        self._compress_task = None        # 正在运行的消息摘要任务
        self._pending_overflow_text: str = ""  # 摘要任务运行期间累积的溢出文本
        self._pending_overflow_user_ids: set = None  # 溢出文本中涉及的用户 ID
        self._pending_overflow_item_ids: set = set()  # 已累积待摘要的消息 id，避免摘要任务运行中重复累积
        self._compressing_overflow_item_ids: set = set()  # 当前摘要任务正在处理的消息 id
        self._compress_failure_time: float = 0  # 上次摘要压缩失败的时间戳（用于冷却）
        preset_key = preset_key or self._chat_data.active_preset  # 参数没有设置时尝试查找上次使用的preset
        if not self.chat_preset_dicts:
            fallback_preset = PresetData(
                preset_key="default",
                bot_self_introl="你是一个自然参与群聊的聊天助手。回复要简短、直接、像真实人类一样。",
                is_default=True,
            )
            self.chat_preset_dicts[fallback_preset.preset_key] = fallback_preset

        if not preset_key:  # 如果没有预设，选择默认预设
            for (pk, preset) in self.chat_preset_dicts.items():
                if preset.is_default:
                    preset_key = pk
                    break
            else:   # 如果没有默认预设，则选择第一个预设
                preset_key = list(self.chat_preset_dicts.keys())[0]
        self.change_presettings(preset_key)

    def get_active_profile(self) -> str:
        """获取当前会话实际生效的 profile 名。

        会话自己选的（rg model）→ OPENAI_PROFILES.default 指针 → 第一个真实 profile，
        返回的恒定是真实 profile 名（不会是指针键），空配置时返回 ""。"""
        return config.resolve_profile_name(self._chat_data.active_profile)

    def set_active_profile(self, profile_name: str) -> None:
        """设置当前会话的 profile"""
        self._chat_data.active_profile = profile_name

    def get_unlock_content_limit(self) -> bool:
        """获取当前会话的内容限制解锁开关（None 时回退到配置默认值）"""
        val = self._chat_data.unlock_content_limit
        return bool(config.UNLOCK_CONTENT_LIMIT) if val is None else val

    def set_unlock_content_limit(self, value: bool) -> None:
        """设置当前会话的内容限制解锁开关"""
        self._chat_data.unlock_content_limit = value

    def apply_profile(self) -> bool:
        """如果当前会话的 profile 与 TextGenerator 不同，切换并返回 True。

        只跟随本会话的 profile，不改写全局默认指针——全局默认是配置文件的
        `default` 指针说了算，任何群消息与 rg model 都不会把它带跑。"""
        from .openai_func import TextGenerator
        target = self.get_active_profile()
        profile = config.get_profile(target)
        if not target or not profile:
            return False
        tg = TextGenerator.instance
        # 检查当前是否已经是目标 profile（通过比较 model 名判断）
        current_model = tg.config.get("model", "")
        target_model = profile.get("model", "")
        if current_model == target_model:
            return False
        tg.switch_profile(target, profile)
        if config.DEBUG_LEVEL > 0:
            logger.info(f"[会话: {self.chat_key}] 自动切换 profile: {target} ({target_model})")
        return True

    def generate_description(self, hide_chat_key: bool = False) -> str:
        """获取当前会话描述"""
        if hide_chat_key:
            return f"[{'启用' if self.is_enable else '禁用'}] 会话: {self.chat_key[:-6]+('*'*6)} 预设: {self.preset_key}\n"
        else:
            return f"[{'启用' if self.is_enable else '禁用'}] 会话: {self.chat_key} 预设: {self.preset_key}\n"

    # region --------------------以下为只读属性定义--------------------

    @property
    def chat_key(self) -> str:
        """获取当前会话 chat_key"""
        return self._chat_data.chat_key
    
    @property
    def preset_key(self) -> str:
        """获取当前对话bot的预设键"""
        return self._preset_key
    
    @property
    def chat_preset_dicts(self) -> Dict[str, PresetData]:
        """获取当前预设数据字典"""
        return self._chat_data.preset_datas

    @property
    def chat_preset(self) -> PresetData:
        """获取当前正在使用的预设的数据，并热加载 md 文件内容（带缓存，TTL 5秒）"""
        preset = self.chat_preset_dicts[self.preset_key]
        now = time.time()
        # 缓存命中：5秒内不重复加载
        if hasattr(self, '_persona_cache_time') and now - self._persona_cache_time < 5.0:
            if hasattr(self, '_persona_cache') and self.preset_key in self._persona_cache:
                preset.bot_self_introl = self._persona_cache[self.preset_key]
                return preset
        # 热加载：从 md 文件实时读取人设内容
        try:
            personas = load_personas_from_directory(str(get_persona_dir()))
            if self.preset_key in personas:
                preset.bot_self_introl = personas[self.preset_key]
                # 更新缓存
                if not hasattr(self, '_persona_cache'):
                    self._persona_cache = {}
                self._persona_cache[self.preset_key] = personas[self.preset_key]
                self._persona_cache_time = now
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"[热加载] 已更新预设 '{self.preset_key}' 的人格设定")
        except Exception as e:
            if config.DEBUG_LEVEL > 0:
                logger.warning(f"[热加载] 加载预设 '{self.preset_key}' 失败: {e}")
        return preset

    @property
    def is_using_default_preset(self) -> bool:
        """当前使用的预设是否是默认预设"""
        return self.chat_preset.is_default
    
    @property
    def is_enable(self):
        """当前会话是否已启用"""
        return self._chat_data.is_enable

    @property
    def enable_auto_switch_identity(self):
        """当前会话是否已启用自动切换人格"""
        return self._chat_data.enable_auto_switch_identity

    @property
    def chat_data(self) -> ChatData:
        """获取chat_data, 请慎重操作"""
        return self._chat_data
    
    @property
    def active_preset(self) -> PresetData:
        """获取当前正在使用的chat_preset, 请慎重操作"""
        return self.chat_preset
    
    @property
    def preset_keys(self) -> List[str]:
        """获取当前会话的所有预设名称列表"""
        return list(self.chat_preset_dicts.keys())
    
    @property
    def last_msg_time(self) -> float:
        """获取上一条消息的时间"""
        return self._last_msg_time
    
    @property
    def last_send_time(self) -> float:
        """获取上一条发送的时间"""
        return self._last_send_time
    
    @property
    def last_gen_time(self) -> float:
        """获取上一条生成的时间"""
        return self._last_gen_time
    
    # endregion 

    # region --------------------以下为数据获取和处理相关功能--------------------

    def toggle_chat(self, enabled: bool = True) -> None:
        """开关当前会话"""
        self._chat_data.is_enable = enabled

    def toggle_auto_switch(self, enabled: bool = True) -> None:
        """开关当前会话自动切换人格"""
        self._chat_data.enable_auto_switch_identity = enabled
    
    def change_presettings(self, preset_key: str) -> Tuple[bool, Optional[str]]:
        """修改对话预设，切换时保留当前预设的历史，加载目标预设的历史"""
        if preset_key not in self.chat_preset_dicts:  # 如果聊天预设字典中没有该预设，则从全局预设字典中拷贝一个
            preset_config = config.PRESETS.get(preset_key, None)
            if not preset_config:
                return (False, '预设不存在')
            self.add_preset_from_config(preset_key, preset_config)
            if config.DEBUG_LEVEL > 0:
                logger.info(f"从全局预设中拷贝预设 {preset_key} 到聊天预设字典")
        
        if preset_key != self._preset_key:
            # 不再清理历史，而是切换到目标预设的历史
            # 每个预设的历史保存在 preset_datas[preset_key] 中
            if config.DEBUG_LEVEL > 0:
                old_preset = self.chat_preset_dicts.get(self._preset_key)
                new_preset = self.chat_preset_dicts.get(preset_key)
                old_prompt_len = len(old_preset.prompt_messages) if old_preset else 0
                new_prompt_len = len(new_preset.prompt_messages) if new_preset else 0
                logger.info(f"切换预设 [{self._preset_key}] → [{preset_key}] | "
                          f"旧预设结构化历史: {old_prompt_len}条 | "
                          f"新预设结构化历史: {new_prompt_len}条")
        
        self._chat_data.active_preset = preset_key
        self._preset_key = preset_key
        return (True, None)
    
    def add_preset(self, preset_key: str, bot_self_introl: str) -> Tuple[bool, Optional[str]]:
        """添加新人格"""
        if preset_key in self.chat_preset_dicts:
            return (False, '同名预设已存在')

        self.chat_preset_dicts[preset_key] = PresetData(preset_key=preset_key, bot_self_introl=bot_self_introl)
        return (True, None)
    
    def add_preset_from_config(self, preset_key: str, preset_config: PresetConfig) -> Tuple[bool, Optional[str]]:
        """从配置添加新人格, config_preset为config中的全局配置"""
        if preset_key in self.chat_preset_dicts:
            return (False, '同名预设已存在')

        self.chat_preset_dicts[preset_key] = PresetData.create_from_config(preset_config)
        # 更新默认值
        if preset_config.is_default:
            for v in self.chat_preset_dicts.values():
                v.is_default = v.preset_key == preset_key
        return (True, None)
    
    def del_preset(self, preset_key: str) -> Tuple[bool, Optional[str]]:
        """删除指定人格预设(允许删除系统人格)"""
        if len(self.chat_preset_dicts) <= 1:
            return (False, '当前会话只有一个预设，不允许删除')
        if preset_key not in self.chat_preset_dicts:
            return (False, f'当前会话不存在预设 [{preset_key}]')
        
        default_preset_key = [preset for preset in self.chat_preset_dicts.values() if preset.is_default][0].preset_key

        if preset_key == default_preset_key:
            return (False, '默认预设不允许删除')
        
        if self._preset_key == preset_key:
            # 删除当前正在使用的preset时切换到默认预设
            self.change_presettings(default_preset_key)
        del self.chat_preset_dicts[preset_key]
        return (True, None)
    
    def update_preset(self, preset_key: str, bot_self_introl: str) -> Tuple[bool, Optional[str]]:
        """修改指定人格预设"""
        if preset_key not in self.chat_preset_dicts:
            return (False, f'预设 [{preset_key}] 不存在')
        
        self.chat_preset_dicts[preset_key].bot_self_introl = bot_self_introl
        return (True, None)
    
    def rename_preset(self, old_preset_key: str, new_preset_key: str) -> Tuple[bool, Optional[str]]:
        """改名指定预设, 对话历史将全部丢失！"""
        if old_preset_key not in self.chat_preset_dicts:
            return (False, '原预设名不存在')
        
        if new_preset_key in self.chat_preset_dicts:
            return (False, '目标预设名已存在')
        
        old_preset_data = self.chat_preset_dicts[old_preset_key]
        if old_preset_data.is_default:
            return (False, '默认预设不允许改名')
        
        bot_self_introl = old_preset_data.bot_self_introl
        success, err_msg = self.del_preset(old_preset_key)
        if not success:
            return (False, err_msg)
        
        success, err_msg = self.add_preset(new_preset_key, bot_self_introl)
        return (success, err_msg)
    
    def reset_preset(self, preset_key: str) -> Tuple[int, Optional[str]]:
        """重置指定预设，将丢失对用户的对话历史和印象数据"""
        preset_config = config.PRESETS.get(preset_key, None)
        
        if preset_key not in self.chat_preset_dicts:
            return (False, f'预设 [{preset_key}] 不存在')
        self.chat_preset_dicts[preset_key].reset_to_default(preset_config)
        return (True, None)
    
    def reset_chat(self) -> Tuple[bool, Optional[str]]:
        """重置当前会话所有预设，将丢失性格或历史数据"""
        self._chat_data.reset()
        return (True, None)
    
    def update_send_time(self) -> None:
        """更新上次发送消息的时间"""
        self._last_send_time = time.time()

    def update_gen_time(self) -> None:
        """更新上次生成消息的时间"""
        self._last_gen_time = time.time()

    @staticmethod
    def _is_supported_image_url(url: str) -> bool:
        """检查图片 URL 是否受支持"""
        if not url:
            return False
        url = str(url).strip()
        return url.startswith(("http://", "https://", "data:image/", "file:///"))

    # 图片有效期判定统一在 ChatPromptMixin._image_expiry_cutoff（30 分钟量化），此处不再单独实现

    # endregion
