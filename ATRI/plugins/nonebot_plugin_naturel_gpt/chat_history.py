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
        image_meta: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[ChatMessageData]:
        """更新当前预设的结构化对话历史。返回新写入的消息对象（未写入时返回 None），供调用方按需回滚。
        image_meta 与 images 平行（[{sender, timestamp}]），仅 context_only 块传入，供按张过期判定。"""
        tg = TextGenerator.instance
        messageunit = tg.generate_msg_template(sender=sender, msg=msg, time_str=f"[{time.strftime('%H:%M:%S %p', time.localtime())}] ")

        # 获取当前预设的数据
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            logger.error(f"[会话: {self.chat_key}] 无法获取当前预设 '{self._preset_key}' 的数据")
            return None
        
        message_index = self._chat_data.next_message_index
        self._chat_data.next_message_index += 1
        
        valid_images: List[str] = []
        valid_meta: List[Dict[str, Any]] = []
        for idx, url in enumerate(images or []):
            if self._is_supported_image_url(url):
                valid_images.append(url)
                if image_meta and idx < len(image_meta) and isinstance(image_meta[idx], dict):
                    valid_meta.append(dict(image_meta[idx]))
        if len(valid_meta) != len(valid_images):
            valid_meta = []
        dropped_image_count = len(images or []) - len(valid_images)
        if dropped_image_count and config.DEBUG_LEVEL > 0:
            logger.warning(f"[会话: {self.chat_key}] 已忽略 {dropped_image_count} 个不支持的图片 URL")

        if config.DEBUG_LEVEL > 0: 
            logger.info(
                f"[会话: {self.chat_key}][预设: {self._preset_key}]添加结构化历史: {messageunit} | "
                f"prompt_messages={len(preset.prompt_messages)} | images={len(valid_images)}"
            )

        history_item: Optional[ChatMessageData] = None
        if record_for_prompt or is_bot_reply or context_only:
            # context_only 消息使用 system 角色，不进入持久化存储。
            # append-only：不再删除旧 context_only，每轮 flush 追加一条新消息，
            # 旧 context_only 随所在区间被窗口裁剪/摘要删除自然淘汰。
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
                image_meta=valid_meta,
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
                # 触发 user 消息：若上下文中尚无该用户的个人印象 system，则在其前注入一条。
                # 一个角色的印象在整个上下文中只注入一次（绑定到首次触发的轮次）；
                # 裁剪/摘要删除该轮次后，下次该用户触发时自然注入更新后的印象，
                # 既避免同一用户印象重复注入，又保证历史前缀稳定以命中 prompt 缓存。
                if role == "user" and user_id:
                    _uid = str(user_id)
                    _has_imp = any(
                        isinstance(m, ChatMessageData) and m.is_impression and m.impression_user_id == _uid
                        for m in preset.prompt_messages
                    )
                    if not _has_imp:
                        _imp_data = preset.chat_impressions.get(_uid)
                        _imp_text = (_imp_data.chat_impression.strip() if _imp_data else "")
                        # Fetch user memory and append to impression text
                        _user_mem = self._get_user_memory(_uid)
                        _user_mem_filtered = {k: v for k, v in _user_mem.items() if v}
                        _user_memory_text = ""
                        if _user_mem_filtered and config.MEMORY_ACTIVE:
                            _mem_lines = []
                            for _idx, (_k, _v) in enumerate(_user_mem_filtered.items(), 1):
                                _mem_lines.append(f"{_idx}. {_k}: {_v}")
                            _user_memory_text = "\n[你的记忆]\n" + "\n".join(_mem_lines)
                        # Add memory reminder if user memory is near limit
                        _user_mem_count = len(_user_mem_filtered)
                        _max_len_mem = config.MEMORY_MAX_LENGTH
                        _threshold = _max_len_mem * 4 // 5
                        _memory_reminder = ""
                        if _user_mem_count >= _threshold:
                            _memory_reminder = (
                                f"\n[记忆提醒] 用户记忆已达 {_user_mem_count}/{_max_len_mem}。"
                                "请主动调用 remember 工具（action=consolidate）批量整理：合并重复或同类条目、压缩冗长表述，"
                                "在尽量少占条数的前提下尽量保留完整信息，关键事实、称呼与设定细节不得丢失。"
                            )
                        # Only inject if there's content (impression or memory)
                        if _imp_text or _user_memory_text:
                            _combined_text = _imp_text + _user_memory_text + _memory_reminder
                            preset.prompt_messages.append(ChatMessageData(
                                role="system",
                                user_id=_uid,
                                sender="",
                                text=_combined_text.strip(),
                                context_only=False,
                                timestamp=time.time(),
                                is_impression=True,
                                impression_user_id=_uid,
                            ))
                preset.prompt_messages.append(history_item)
        
        if record_time:
            self._last_msg_time = time.time()   # 更新上次对话时间

        if require_summary:
            await self._compress_prompt_messages_if_needed(preset)
        elif not config.CONTEXT_SUMMARY_ENABLED:
            self._trim_prompt_messages_without_summary(preset)
        return history_item

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

    def remove_last_prompt_user_message(self, expected: Optional[ChatMessageData] = None) -> None:
        """移除最后一条用户消息（用于并发控制时合并消息）。
        传入 expected 时，仅当最后一条用户消息就是该对象时才删除——
        用于节流放弃后的回滚，避免误删并发合并场景下后写入的新消息。"""
        preset = self.chat_preset_dicts.get(self._preset_key)
        if not preset:
            return
        for idx in range(len(preset.prompt_messages) - 1, -1, -1):
            item = preset.prompt_messages[idx]
            if isinstance(item, ChatMessageData) and item.role == "user":
                if expected is not None and item is not expected:
                    return
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
        context_only 视为普通历史条目（append-only），随所在区间一并删除，不再有裁剪豁免。"""
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
            # 截断点之前的消息（含 context_only）全部删除
            del preset.prompt_messages[:cut_index]
            # 截断可能切断 user -> assistant/tool 链，产生新的孤立消息
            preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)

    @staticmethod
    def _cleanup_orphan_history_messages(messages: List[ChatMessageData]) -> List[ChatMessageData]:
        """清理没有真实 user 轮次承接的 assistant/tool 历史，context_only 原样保留（append-only，可存在多条）。
        印象 system 绑定到紧随其后的 user 轮：若该 user 被清理则印象一并丢弃，避免孤立印象残留。"""
        cleaned = ChatHistoryMixin._cleanup_orphan_tool_messages(messages)
        result: List[ChatMessageData] = []
        # 计数版轮次不变量：user +1、最终 assistant -1；循环邮箱会产生
        # user(A) user(B) assistant(答A) assistant(答B)，布尔"首条 assistant 关轮"会误删答 B。
        open_users = 0
        active_tool_call_ids = set()
        pending_impression: Optional[ChatMessageData] = None
        for item in cleaned:
            if item.context_only:
                result.append(item)
                continue
            if item.is_impression:
                # 印象 system 暂存，等下一个 user 决定去留
                pending_impression = item
                continue
            if item.role == "user":
                open_users += 1
                active_tool_call_ids = set()
                if pending_impression is not None:
                    # 保持写入顺序 [印象, context_only, user]：context_only 是在印象随 user 落位后
                    # 才插入到 user 之前的，清理时将印象插回末尾连续 context_only 之前，
                    # 避免尾部状态块插入位置与摘要裁剪边界错位
                    insert_at = len(result)
                    while insert_at > 0 and result[insert_at - 1].context_only:
                        insert_at -= 1
                    result.insert(insert_at, pending_impression)
                    pending_impression = None
                result.append(item)
                continue
            # assistant / tool：前面挂着未消化的印象说明印象后没跟 user，丢弃该孤立印象
            if pending_impression is not None:
                pending_impression = None
            if item.role == "assistant":
                if open_users <= 0:
                    continue
                if is_model_request_error_text(item.text):
                    open_users -= 1
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
                    open_users -= 1
                    active_tool_call_ids = set()
                continue
            if item.role == "tool":
                if open_users > 0 and item.tool_call_id and item.tool_call_id in active_tool_call_ids:
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
