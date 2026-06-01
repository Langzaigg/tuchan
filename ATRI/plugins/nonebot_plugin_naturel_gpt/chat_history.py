"""对话历史管理模块 - 负责对话历史的添加、截断和清理"""

import time
from typing import Any, Dict, List, Optional

from .logger import logger
from .config import config
from .openai_func import TextGenerator
from .persistent_data_manager import ChatMessageData, ImpressionData, PresetData
from .llm_tool_plugins import TOOL_REGISTRY


class ChatHistoryMixin:
    """对话历史管理 Mixin，提供对话历史的添加、截断和清理功能"""

    async def update_chat_history_row(
        self,
        sender: str,
        msg: str,
        require_summary: bool = False,
        record_time=False,
        images: Optional[List[str]] = None,
        is_bot_reply: bool = False,
        record_for_prompt: bool = False,
        content_is_labeled: bool = False,
        context_only: bool = False,
    ) -> None:
        """更新当前预设的结构化对话历史。"""
        tg = TextGenerator.instance
        messageunit = tg.generate_msg_template(sender=sender, msg=msg, time_str=f"[{time.strftime('%H:%M:%S %p', time.localtime())}] ")
        
        # 获取当前预设的数据
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            logger.error(f"[会话: {self.chat_key}] 无法获取当前预设 '{self._preset_key}' 的数据")
            return
        
        message_index = self._chat_data.next_message_index
        self._chat_data.next_message_index += 1
        
        valid_images = [url for url in (images or []) if self._is_supported_image_url(url)]
        dropped_image_count = len(images or []) - len(valid_images)
        if dropped_image_count and config.DEBUG_LEVEL > 0:
            logger.warning(f"[会话: {self.chat_key}] 已忽略 {dropped_image_count} 个不支持的图片 URL")

        if config.DEBUG_LEVEL > 0: 
            logger.info(
                f"[会话: {self.chat_key}][预设: {self._preset_key}]添加结构化历史: {messageunit} | "
                f"prompt_messages={len(preset.prompt_messages)} | images={len(valid_images)}"
            )

        if record_for_prompt or is_bot_reply or context_only:
            if context_only:
                # 移除所有已有的 context_only 消息，保证仅保留最新一条
                preset.prompt_messages = [
                    m for m in preset.prompt_messages
                    if not (isinstance(m, ChatMessageData) and m.context_only)
                ]
            # context_only 消息使用 system 角色，不进入持久化存储
            if context_only:
                role = "system"
            elif is_bot_reply:
                role = "assistant"
            else:
                role = "user"
            preset.prompt_messages.append(ChatMessageData(
                role=role,
                sender=sender,
                text=msg,
                images=valid_images,
                content_is_labeled=content_is_labeled,
                context_only=context_only,
                timestamp=time.time(),
                triggered=record_for_prompt,
            ))
        
        if record_time:
            self._last_msg_time = time.time()   # 更新上次对话时间
        
        if require_summary:
            await self._compress_prompt_messages_if_needed(preset)
        else:
            self._trim_prompt_messages_without_summary(preset)

    async def save_tool_messages(self, tool_messages: List[Dict[str, Any]]) -> None:
        """保存工具调用消息到内存中的prompt_messages（不持久化）"""
        if not tool_messages:
            return
        
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            logger.error(f"[会话: {self.chat_key}] 无法获取当前预设 '{self._preset_key}' 的数据")
            return
        
        valid_tool_names = set(TOOL_REGISTRY.keys())

        for msg in tool_messages:
            role = msg.get("role", "")
            if role == "assistant" and msg.get("tool_calls"):
                # 校验工具调用：修复双拼函数名，剔除无效调用
                fixed_calls = []
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    if not name:
                        continue
                    if name not in valid_tool_names:
                        # 尝试修复双拼名（如 generate_anima_imagegenerate_anima_image）
                        half = len(name) // 2
                        if half > 0 and name[:half] == name[half:] and name[:half] in valid_tool_names:
                            logger.warning(f"[会话: {self.chat_key}] 修复双拼工具名: {name} -> {name[:half]}")
                            func["name"] = name[:half]
                            fixed_calls.append(tc)
                        else:
                            logger.warning(f"[会话: {self.chat_key}] 剔除无效工具调用: {name}")
                    else:
                        fixed_calls.append(tc)
                if not fixed_calls:
                    continue
                msg["tool_calls"] = fixed_calls
                preset.prompt_messages.append(ChatMessageData(
                    role="assistant",
                    sender=self._preset_key,
                    text=msg.get("content", ""),
                    tool_calls=fixed_calls,
                    reasoning_content=msg.get("reasoning_content", ""),
                    timestamp=time.time(),
                ))
            elif role == "tool":
                preset.prompt_messages.append(ChatMessageData(
                    role="tool",
                    sender=self._preset_key,
                    text=msg.get("content", ""),
                    tool_call_id=msg.get("tool_call_id", ""),
                    tool_name=msg.get("name", ""),
                    timestamp=time.time(),
                ))
        
        if config.DEBUG_LEVEL > 0:
            logger.info(f"[会话: {self.chat_key}] 已保存 {len(tool_messages)} 条工具消息到内存")

    async def update_chat_history_row_for_user(self, sender: str, msg: str, userid: str, username: str, require_summary: bool = False) -> None:
        """更新对特定用户的对话历史行（仅累积，印象由摘要任务统一生成）"""
        if userid not in self.chat_preset.chat_impressions:
            impression_data = ImpressionData(user_id=userid)
            self.chat_preset.chat_impressions[userid] = impression_data
        else:
            impression_data = self.chat_preset.chat_impressions[userid]
        tg = TextGenerator.instance
        messageunit = tg.generate_msg_template(sender=sender, msg=msg)
        impression_data.chat_history.append(messageunit)
        if config.DEBUG_LEVEL > 0:
            logger.info(f"添加对话历史行: {messageunit}  |  当前对话历史行数: {len(impression_data.chat_history)}")
        # 保证对话历史不超过最大长度，超出时丢弃最早的
        max_history = max(1, config.USER_MEMORY_SUMMARY_THRESHOLD * 2)
        if len(impression_data.chat_history) > max_history:
            impression_data.chat_history = impression_data.chat_history[-max_history:]

    def remove_last_prompt_user_message(self) -> None:
        """移除最后一条用户消息（用于并发控制时合并消息）"""
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            return
        for idx in range(len(preset.prompt_messages) - 1, -1, -1):
            item = preset.prompt_messages[idx]
            if isinstance(item, ChatMessageData) and item.role == "user":
                del preset.prompt_messages[idx]
                return

    def cleanup_after_bad_request(self, keep_history: int = 5) -> None:
        """清理最容易导致 400 的上下文，尤其是已过期的图片 URL。"""
        preset = self.chat_preset_dicts.get(self._preset_key)
        if preset:
            preset.prompt_messages = preset.prompt_messages[-keep_history:]
            for item in preset.prompt_messages:
                if isinstance(item, ChatMessageData):
                    item.images = []
        if config.DEBUG_LEVEL > 0:
            logger.warning(
                f"[会话: {self.chat_key}] 已清理 400 后上下文: "
                f"prompt_messages={len(preset.prompt_messages) if preset else 0}"
            )

    def _trim_prompt_messages_without_summary(self, preset: PresetData) -> None:
        """滑动窗口截断：保留最近 CONTEXT_WINDOW_SIZE 轮对话，保护工具调用链完整性。
        截断时保存溢出消息到 _pending_overflow_text，供后续摘要任务使用。"""
        max_rounds = max(1, config.CONTEXT_WINDOW_SIZE)
        # 先清理孤立的tool消息
        preset.prompt_messages = self._cleanup_orphan_tool_messages(preset.prompt_messages)
        # 从末尾向前数 max_rounds 轮，找到截断点（排除 context_only）
        rounds = 0
        cut_index = 0
        for i in range(len(preset.prompt_messages) - 1, -1, -1):
            if preset.prompt_messages[i].role == "user" and not preset.prompt_messages[i].context_only:
                rounds += 1
                if rounds > max_rounds:
                    cut_index = i + 1  # +1 确保包含最旧的溢出轮本身
                    break
        else:
            # 不足 max_rounds 轮，不截断
            return
        if cut_index > 0:
            # 保存溢出消息用于后续摘要（排除工具消息，避免干扰摘要质量）
            overflow_messages = [
                m for m in preset.prompt_messages[:cut_index]
                if not (isinstance(m, ChatMessageData) and m.role in {"tool", "assistant"} and m.tool_calls)
            ]
            if overflow_messages:
                overflow_text = "\n".join(self._format_prompt_message_for_summary(item) for item in overflow_messages)
                if overflow_text:
                    if self._pending_overflow_text:
                        self._pending_overflow_text += "\n" + overflow_text
                    else:
                        self._pending_overflow_text = overflow_text
                    if config.DEBUG_LEVEL > 0:
                        logger.info(f"[会话: {self.chat_key}] 截断时保存 {len(overflow_messages)} 条溢出消息到 pending")
            del preset.prompt_messages[:cut_index]
            # 截断后，如果开头是孤立的 assistant（其对应的 user 已被截断），删除它
            while preset.prompt_messages and preset.prompt_messages[0].role == "assistant":
                preset.prompt_messages.pop(0)
            # 截断可能在 assistant+tool_calls 和 tool 结果之间切断，产生新的孤立 tool 消息
            preset.prompt_messages = self._cleanup_orphan_tool_messages(preset.prompt_messages)

    @staticmethod
    def _cleanup_orphan_tool_messages(messages: List[ChatMessageData]) -> List[ChatMessageData]:
        """清理孤立的tool消息，确保tool_calls和tool消息配对"""
        result = []
        i = 0
        while i < len(messages):
            item = messages[i]
            if item.role == "assistant" and item.tool_calls:
                # 找到assistant的tool_calls，收集对应的tool消息
                tool_call_ids = {tc.get("id") for tc in item.tool_calls if tc.get("id")}
                result.append(item)
                i += 1
                # 收集紧随其后的tool消息
                while i < len(messages) and messages[i].role == "tool" and messages[i].tool_call_id in tool_call_ids:
                    result.append(messages[i])
                    tool_call_ids.discard(messages[i].tool_call_id)
                    i += 1
            elif item.role == "tool":
                # 孤立的tool消息，跳过
                i += 1
            else:
                result.append(item)
                i += 1
        return result

    @staticmethod
    def _count_rounds(messages: List[ChatMessageData]) -> int:
        """统计消息列表中的对话轮数（以 user 消息计数，排除 context_only）"""
        return sum(1 for m in messages if m.role == "user" and not m.context_only)
