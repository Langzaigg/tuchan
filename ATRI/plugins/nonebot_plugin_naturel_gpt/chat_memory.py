"""记忆管理模块 - 负责群记忆和用户个人记忆的管理"""

from typing import Dict, Optional
from .logger import logger
from .config import config
from .persistent_data_manager import PersistentDataManager


class ChatMemoryMixin:
    """记忆管理 Mixin，提供群记忆和用户个人记忆的管理功能"""

    def _get_chat_memory(self) -> Dict[str, str]:
        """获取当前有效的群记忆（global 或按人格）。"""
        if self._chat_data.global_memory_enabled:
            return self._chat_data.global_chat_memory
        return self.chat_preset.chat_memory

    def _get_user_memory(self, userid: str) -> Dict[str, str]:
        """获取当前有效的用户记忆（global 或按人格）。"""
        if self._chat_data.global_memory_enabled:
            return PersistentDataManager.instance.get_global_user_memories(userid)
        if userid not in self.chat_preset.user_memories:
            self.chat_preset.user_memories[userid] = {}
        return self.chat_preset.user_memories[userid]

    def set_memory(self, mem_key: str, mem_value: str = '') -> None:
        """为当前预设设置记忆，支持智能淘汰"""
        if not mem_key:
            return
        mem_key = mem_key.replace(' ', '_')  # 将空格替换为下划线
        chat_memory = self._get_chat_memory()
        # 如果没有指定mem_value，则删除该记忆
        if not mem_value:
            if mem_key in chat_memory:
                del chat_memory[mem_key]
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"忘记了: {mem_key}")
            else:
                logger.warning(f"尝试删除不存在的记忆 {mem_key}")
        else:  # 否则设置该记忆，并将其移到在最后
            if mem_key in chat_memory:
                del chat_memory[mem_key]
            chat_memory[mem_key] = mem_value
            if config.DEBUG_LEVEL > 0:
                logger.info(f"记住了: {mem_key} -> {mem_value}")

            # 超出上限时仅记录警告，不再自动删除；由 LLM 通过 consolidate 主动整理
            if len(chat_memory) > config.MEMORY_MAX_LENGTH:
                logger.warning(f"群记忆已超出上限: {len(chat_memory)}/{config.MEMORY_MAX_LENGTH}，等待 LLM 调用 consolidate 整理")
