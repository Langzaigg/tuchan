"""对话历史管理模块 - 负责对话历史的添加、截断和清理"""

import copy
import time
from typing import Any, Dict, List, Optional

from .logger import logger
from .config import config
from .openai_func import TextGenerator, is_model_request_error_text
from .persistent_data_manager import ChatMessageData, ImpressionData, PresetData
from .llm_tool_plugins import TOOL_REGISTRY


class ChatHistoryMixin:
    """对话历史管理 Mixin，提供对话历史的添加、截断和清理功能"""

    @staticmethod
    def _target_context_round_limit() -> int:
        """摘要完成后的目标上下文轮数。"""
        return max(1, int(getattr(config, "CONTEXT_WINDOW_SIZE", 1) or 1))

    @staticmethod
    def _context_overflow_round_limit() -> int:
        """摘要触发前允许额外保留的溢出轮数。"""
        try:
            ratio = float(getattr(config, "CONTEXT_COMPRESS_THRESHOLD_RATIO", 0.5) or 0)
        except (TypeError, ValueError):
            ratio = 0.5
        return int(ChatHistoryMixin._target_context_round_limit() * max(0.0, ratio))

    @staticmethod
    def _history_buffer_round_limit() -> int:
        """请求构造和未摘要硬裁剪使用的独立对话缓冲窗口。"""
        return ChatHistoryMixin._target_context_round_limit() + ChatHistoryMixin._context_overflow_round_limit()

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
        user_id: str = "",
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
            history_item = ChatMessageData(
                role=role,
                user_id=str(user_id or ""),
                sender=sender,
                text=msg,
                images=valid_images,
                content_is_labeled=content_is_labeled,
                context_only=context_only,
                timestamp=time.time(),
                triggered=record_for_prompt,
            )
            if context_only:
                insert_at = len(preset.prompt_messages)
                for idx in range(len(preset.prompt_messages) - 1, -1, -1):
                    item = preset.prompt_messages[idx]
                    if isinstance(item, ChatMessageData) and item.role == "user" and not item.context_only:
                        insert_at = idx
                        break
                preset.prompt_messages.insert(insert_at, history_item)
            else:
                preset.prompt_messages.append(history_item)
        
        if record_time:
            self._last_msg_time = time.time()   # 更新上次对话时间
        
        if require_summary:
            await self._compress_prompt_messages_if_needed(preset)
        elif not config.CONTEXT_SUMMARY_ENABLED:
            self._trim_prompt_messages_without_summary(preset)

    async def save_tool_messages(self, tool_messages: List[Dict[str, Any]]) -> Optional[ChatMessageData]:
        """保存工具调用消息到内存中的prompt_messages（不持久化）"""
        if not tool_messages:
            return None
        
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            logger.error(f"[会话: {self.chat_key}] 无法获取当前预设 '{self._preset_key}' 的数据")
            return None
        
        valid_tool_names = set(TOOL_REGISTRY.keys())
        last_assistant_msg: Optional[ChatMessageData] = None
        valid_tool_call_names: Dict[str, str] = {}

        for msg in tool_messages:
            role = msg.get("role", "")
            if role == "assistant" and msg.get("tool_calls"):
                # 校验工具调用：修复双拼函数名，剔除无效调用
                fixed_calls = []
                for tc in msg["tool_calls"]:
                    tc = copy.deepcopy(tc)
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
                for i, tc in enumerate(fixed_calls):
                    call_id = str(tc.get("id") or f"call_{i}")
                    tc["id"] = call_id
                    func = tc.get("function", {})
                    valid_tool_call_names[call_id] = str(func.get("name", "") or "")
                # 创建新字典而非修改原始 msg
                msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                msg["tool_calls"] = fixed_calls
                assistant_history_item = ChatMessageData(
                    role="assistant",
                    sender=self._preset_key,
                    text=msg.get("content", ""),
                    tool_calls=fixed_calls,
                    reasoning_content=msg.get("reasoning_content", ""),
                    timestamp=time.time(),
                )
                preset.prompt_messages.append(assistant_history_item)
                last_assistant_msg = assistant_history_item
            elif role == "tool":
                tool_call_id = str(msg.get("tool_call_id", "") or "")
                if tool_call_id not in valid_tool_call_names:
                    if config.DEBUG_LEVEL > 0:
                        logger.warning(f"[会话: {self.chat_key}] 剔除无对应 tool_call 的工具结果: {tool_call_id}")
                    continue
                preset.prompt_messages.append(ChatMessageData(
                    role="tool",
                    sender=self._preset_key,
                    text=msg.get("content", ""),
                    tool_call_id=tool_call_id,
                    tool_name=valid_tool_call_names.get(tool_call_id, str(msg.get("name", "") or "")),
                    timestamp=time.time(),
                ))
        
        if config.DEBUG_LEVEL > 0:
            logger.info(f"[会话: {self.chat_key}] 已保存 {len(tool_messages)} 条工具消息到内存")
        return last_assistant_msg

    async def update_chat_history_row_for_user(self, sender: str, msg: str, userid: str, username: str, require_summary: bool = False) -> None:
        """更新对特定用户的对话历史行（仅累积，印象由摘要任务统一生成）"""
        if userid not in self.chat_preset.chat_impressions:
            impression_data = ImpressionData(user_id=userid, nickname=username or "")
            self.chat_preset.chat_impressions[userid] = impression_data
        else:
            impression_data = self.chat_preset.chat_impressions[userid]
            if username:
                impression_data.nickname = username
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
            # 只清理超过有效期的图片，保留新鲜图片
            now = time.time()
            fresh_seconds = max(1, config.MULTIMODAL_IMAGE_FRESH_MINUTES) * 60
            for item in preset.prompt_messages:
                if isinstance(item, ChatMessageData) and item.images:
                    item.images = [
                        url for url in item.images
                        if not item.timestamp or (now - item.timestamp <= fresh_seconds)
                    ]
            preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)
        if config.DEBUG_LEVEL > 0:
            logger.warning(
                f"[会话: {self.chat_key}] 已清理 400 后上下文: "
                f"prompt_messages={len(preset.prompt_messages) if preset else 0}"
            )

    def _trim_prompt_messages_without_summary(self, preset: PresetData) -> None:
        """滑动窗口截断：保留最近独立缓冲窗口内的对话，保护工具调用链完整性。
        保留 context_only 消息（非触发上下文），只删除溢出的 user/assistant 轮次。"""
        max_rounds = self._history_buffer_round_limit()
        # 先清理没有真实 user 承接的 assistant/tool 消息
        preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)
        # 从末尾向前数 max_rounds 轮，找到截断点（排除 context_only）
        rounds = 0
        cut_index = 0
        for i in range(len(preset.prompt_messages) - 1, -1, -1):
            if preset.prompt_messages[i].role == "user" and not preset.prompt_messages[i].context_only:
                rounds += 1
                if rounds > max_rounds:
                    cut_index = i + 1
                    break
        else:
            # 不足 max_rounds 轮，不截断
            return
        if cut_index > 0:
            # 保留 context_only 消息，只删除溢出的 user/assistant/tool 轮次
            del_indices = [i for i in range(cut_index) if not preset.prompt_messages[i].context_only]
            for i in sorted(del_indices, reverse=True):
                del preset.prompt_messages[i]
            if config.DEBUG_LEVEL > 0:
                preserved_ctx = sum(1 for i in range(min(cut_index, len(preset.prompt_messages))) if preset.prompt_messages[i].context_only)
                if preserved_ctx:
                    logger.info(f"[会话: {self.chat_key}] 截断时保留了 {preserved_ctx} 条 context_only 消息")
            # 截断可能切断 user -> assistant/tool 链，产生新的孤立消息
            preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)

    @staticmethod
    def _cleanup_orphan_history_messages(messages: List[ChatMessageData]) -> List[ChatMessageData]:
        """清理没有真实 user 轮次承接的 assistant/tool 历史，保留 context_only。"""
        cleaned = ChatHistoryMixin._cleanup_orphan_tool_messages(messages)
        result: List[ChatMessageData] = []
        round_open = False
        active_tool_call_ids = set()
        for item in cleaned:
            if item.context_only:
                result.append(item)
                continue
            if item.role == "user":
                round_open = True
                active_tool_call_ids = set()
                result.append(item)
                continue
            if item.role == "assistant":
                if not round_open:
                    continue
                if is_model_request_error_text(item.text):
                    round_open = False
                    active_tool_call_ids = set()
                    continue
                result.append(item)
                if item.tool_calls:
                    active_tool_call_ids = {
                        tc.get("id")
                        for tc in item.tool_calls
                        if isinstance(tc, dict) and tc.get("id")
                    }
                else:
                    round_open = False
                    active_tool_call_ids = set()
                continue
            if item.role == "tool":
                if round_open and item.tool_call_id and item.tool_call_id in active_tool_call_ids:
                    result.append(item)
                    active_tool_call_ids.discard(item.tool_call_id)
                continue
        return ChatHistoryMixin._cleanup_orphan_tool_messages(result)

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
