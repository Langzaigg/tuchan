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
        self._tool_summary_task = None    # 正在运行的工具摘要任务
        self._pending_overflow_text: str = ""  # 摘要任务运行期间累积的溢出文本
        self._pending_overflow_user_ids: set = None  # 溢出文本中涉及的用户 ID
        self._pending_overflow_item_ids: set = set()  # 已累积待摘要的消息 id，避免摘要任务运行中重复累积
        self._compressing_overflow_item_ids: set = set()  # 当前摘要任务正在处理的消息 id
        self._compress_failure_time: float = 0  # 上次摘要压缩失败的时间戳（用于冷却）
        self._last_interrupted_response: str = ""  # 被打断的流式回复内容
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
        self._context_buffer: List[Dict[str, Any]] = []  # 非触发消息临时缓冲

    def push_context_buffer(self, sender: str, text: str, images: Optional[List[str]] = None) -> None:
        """兼容旧路径：将非触发消息推入临时缓冲区。主路径在 matcher._recent_context_buffers。"""
        image_list = list(images or [])
        normalized_text = str(text or "").strip()
        if not normalized_text and image_list:
            normalized_text = " ".join(f"[图片{i + 1}]" for i in range(len(image_list)))
        self._context_buffer.append({
            "sender": sender,
            "text": normalized_text,
            "images": image_list,
            "timestamp": time.time(),
        })
        if hasattr(self, "_history_buffer_round_limit"):
            max_buf = self._history_buffer_round_limit()
        else:
            try:
                target_rounds = int(getattr(config, "CONTEXT_WINDOW_SIZE", 1) or 1)
            except (TypeError, ValueError):
                target_rounds = 1
            try:
                overflow_ratio = float(getattr(config, "CONTEXT_COMPRESS_THRESHOLD_RATIO", 0.5) or 0)
            except (TypeError, ValueError):
                overflow_ratio = 0.5
            max_buf = max(1, target_rounds) + int(max(1, target_rounds) * max(0.0, overflow_ratio))
        if len(self._context_buffer) > max_buf:
            self._context_buffer = self._context_buffer[-max_buf:]
        if config.DEBUG_LEVEL > 0:
            logger.info(
                f"[上下文缓冲] 已缓存非触发消息 | 会话: {self.chat_key} | "
                f"sender={sender} | text_len={len(normalized_text)} | images={len(image_list)} | "
                f"buffer={len(self._context_buffer)}/{max_buf}"
            )

    def flush_context_buffer(self) -> Tuple[str, List[str]]:
        """清空缓冲区，返回 (合并文本, 图片URL列表)。格式: [HH:MM] sender: text"""
        if not self._context_buffer:
            return "", []
        parts = []
        images = []
        img_counter = 0
        for item in self._context_buffer:
            ts = time.strftime('%H:%M', time.localtime(item["timestamp"]))
            text = item.get("text", "")
            # 用全局计数器替换每个用户各自的 [图片N]，避免不同用户的 [图片1] 混淆
            if item.get("images"):
                for i in range(len(item["images"])):
                    img_counter += 1
                    marker = f"[图片{i + 1}]"
                    replacement = f"[图片{img_counter}]"
                    if marker in text:
                        text = text.replace(marker, replacement, 1)
                    else:
                        text = f"{text} {replacement}".strip()
            parts.append(f"[{ts}] {item['sender']}: {text}")
            images.extend(item.get("images", []))
        self._context_buffer = []
        return "\n".join(parts), images

    def set_interrupted_response(self, text: str) -> None:
        """保存被中断的流式回复内容"""
        self._last_interrupted_response = text

    def pop_interrupted_response(self) -> str:
        """取出并清空被中断的回复内容（一次性读取）"""
        text = self._last_interrupted_response
        self._last_interrupted_response = ""
        return text

    def get_active_profile(self) -> str:
        """获取当前会话的 profile 名，为空时返回全局默认"""
        return self._chat_data.active_profile or config.OPENAI_ACTIVE_PROFILE or ""

    def set_active_profile(self, profile_name: str) -> None:
        """设置当前会话的 profile"""
        self._chat_data.active_profile = profile_name

    def apply_profile(self) -> bool:
        """如果当前会话的 profile 与 TextGenerator 不同，切换并返回 True"""
        from .openai_func import TextGenerator
        target = self.get_active_profile()
        profiles = config.OPENAI_PROFILES
        if not target or not profiles or target not in profiles:
            return False
        tg = TextGenerator.instance
        # 检查当前是否已经是目标 profile（通过比较 model 名判断）
        current_model = tg.config.get("model", "")
        target_model = profiles[target].get("model", "")
        if current_model == target_model:
            return False
        tg.switch_profile(target, profiles[target])
        config.OPENAI_ACTIVE_PROFILE = target
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

    @staticmethod
    def _image_is_fresh(timestamp: float) -> bool:
        """检查图片是否在有效期内（使用配置的过期时间）"""
        if not timestamp:
            return False
        fresh_seconds = max(1, config.MULTIMODAL_IMAGE_FRESH_MINUTES) * 60
        return time.time() - float(timestamp) <= fresh_seconds

    # endregion
