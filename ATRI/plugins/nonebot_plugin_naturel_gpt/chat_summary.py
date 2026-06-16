"""摘要和印象生成模块 - 负责上下文摘要压缩和用户印象生成"""

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .logger import logger
from .config import config
from .openai_func import TextGenerator
from .persistent_data_manager import ChatMessageData, PersistentDataManager, PresetData

# 摘要压缩失败后的冷却时间（秒），避免持续触发无效请求
_COMPRESS_COOLDOWN_SECONDS = 120


def _extract_tavily_ai_answer(content: str) -> str:
    """从 tavily_search 返回的格式化结果中提取 [AI 摘要] 内容"""
    marker = "[AI 摘要]"
    idx = content.find(marker)
    if idx < 0:
        return ""
    answer = content[idx + len(marker):].strip()
    # 截断到第一个换行或结果列表开头（如 "\n1."），避免混入搜索结果
    for stop in ["\n1.", "\n2.", "\n3."]:
        stop_idx = answer.find(stop)
        if stop_idx > 0:
            answer = answer[:stop_idx].strip()
            break
    return answer


def _save_error_log(chat_key: str, prompt: str, response: str, cost_tokens: int) -> None:
    """保存摘要/印象任务的失败请求到 error log，便于排查 API 兼容性问题"""
    log_dir = Path(config.NG_LOG_PATH)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_key = chat_key.replace("/", "_").replace("\\", "_")
    log_file = log_dir / f"{safe_key}.error.json"
    data = {
        "chat_key": chat_key,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "cost_tokens": cost_tokens,
        "prompt": prompt,
        "response": response,
        "source": "summary_task",
    }
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存摘要 error 日志失败: {e!r}")


def _save_summary_log(chat_key: str, summary_type: str,
                      summary_prompt: str, summary_response: str,
                      context_summary: str, tool_call_summary: str,
                      impressions: Optional[Dict[str, str]] = None) -> None:
    """保存摘要日志：包含摘要 LLM 的请求/响应和最终摘要结果"""
    log_dir = Path(config.NG_LOG_PATH)
    log_dir.mkdir(parents=True, exist_ok=True)
    safe_key = chat_key.replace("/", "_").replace("\\", "_")
    log_file = log_dir / f"{safe_key}.summary.json"
    data = {
        "chat_key": chat_key,
        "type": summary_type,
        "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
        "summary_request": summary_prompt,
        "summary_response": summary_response,
        "context_summary": context_summary,
        "tool_call_summary": tool_call_summary,
    }
    if impressions:
        data["impressions"] = impressions
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"保存摘要日志失败: {e!r}")


class ChatSummaryMixin:
    """摘要和印象生成 Mixin，提供工具调用摘要和上下文压缩功能"""

    @staticmethod
    def _existing_message_ids(preset: PresetData, item_ids: set) -> set:
        """只保留当前仍存在于 prompt_messages 中的消息 id。"""
        if not item_ids:
            return set()
        return {
            id(m)
            for m in preset.prompt_messages
            if isinstance(m, ChatMessageData) and id(m) in item_ids
        }

    def _snapshot_request_profile(self) -> Dict[str, Any]:
        """固定后台摘要/印象任务使用的当前会话 profile。"""
        active_profile = self.get_active_profile() if hasattr(self, "get_active_profile") else config.OPENAI_ACTIVE_PROFILE
        profile = dict(config.OPENAI_PROFILES.get(active_profile, {}) or {})
        if profile:
            profile["api_keys"] = list(profile.get("api_keys", config.OPENAI_API_KEYS) or [""])
            profile["enable_stream"] = config.LLM_ENABLE_STREAM
            return profile
        return {
            "api_keys": list(config.OPENAI_API_KEYS or [""]),
            "base_url": config.OPENAI_BASE_URL or "",
            "proxy": config.OPENAI_PROXY_SERVER or None,
            "use_socket_proxy": False,
            "multimodal": True,
            "model": config.CHAT_MODEL,
            "model_mini": config.CHAT_MODEL_MINI,
            "max_tokens": config.REPLY_MAX_TOKENS,
            "temperature": config.CHAT_TEMPERATURE,
            "top_p": config.CHAT_TOP_P,
            "frequency_penalty": config.CHAT_FREQUENCY_PENALTY,
            "presence_penalty": config.CHAT_PRESENCE_PENALTY,
            "max_summary_tokens": config.CHAT_MAX_SUMMARY_TOKENS,
            "timeout": config.OPENAI_TIMEOUT,
            "enable_stream": config.LLM_ENABLE_STREAM,
        }

    async def generate_tool_call_summary(
        self,
        tool_messages: List[Dict[str, Any]],
        max_chars: int = 200,
        trigger_text: str = "",
        target_msg: Optional[ChatMessageData] = None,
    ) -> None:
        """模式3: 异步生成工具调用摘要。搜索类工具生成摘要，其他工具保留原始结果。"""
        if config.TOOL_CONTEXT_MODE != 3 or not tool_messages:
            return

        SEARCH_TOOLS = {"tavily_search", "bocha_search", "fetch_url", "browse_url"}
        IGNORED_TOOLS = {"generate_anima_image"}  # 不保留在历史上下文中的工具，避免 LLM 产生已调用的错觉

        search_entries: List[Dict[str, Any]] = []
        other_entries: List[Dict[str, Any]] = []
        tavily_ai_answers: List[str] = []  # 收集 tavily_search 返回的 AI 摘要
        for msg in tool_messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    if name in IGNORED_TOOLS:
                        continue
                    try:
                        args = json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                    except Exception:
                        args = {}
                    entry = {"name": name, "args": args}
                    if name in SEARCH_TOOLS:
                        search_entries.append(entry)
                    else:
                        other_entries.append(entry)
            elif msg.get("role") == "tool":
                name = msg.get("name", "")
                if name in IGNORED_TOOLS:
                    continue
                content = msg.get("content", "")
                entry = {"name": name, "result": content[:300]}
                if name in SEARCH_TOOLS:
                    search_entries.append(entry)
                else:
                    other_entries.append(entry)
                if name == "tavily_search":
                    ai_answer = _extract_tavily_ai_answer(content)
                    if ai_answer:
                        tavily_ai_answers.append(ai_answer)

        if not search_entries and not other_entries:
            return

        if not target_msg or not target_msg.tool_calls:
            logger.warning(f"[会话: {self.chat_key}] 工具摘要缺少绑定的 assistant tool_calls 消息，已跳过")
            return

        # 同步构建 fallback（other 结果 + search 摘要），立即写入
        combined_parts = []

        if other_entries:
            raw_parts = []
            for entry in other_entries:
                if "result" in entry:
                    raw_parts.append(f"{entry['name']}: {entry['result'][:80]}")
                else:
                    raw_parts.append(f"{entry['name']}({json.dumps(entry.get('args', {}), ensure_ascii=False)[:60]})")
            combined_parts.append(f"[调用结果] {'; '.join(raw_parts)[:max_chars]}")

        if search_entries:
            search_raw = []
            for entry in search_entries:
                if "result" in entry:
                    search_raw.append(f"{entry['name']}: {entry['result'][:80]}")
                else:
                    search_raw.append(f"{entry['name']}({json.dumps(entry.get('args', {}), ensure_ascii=False)[:60]})")
            search_fallback = "；".join(search_raw)[:max_chars]
            combined_parts.append(f"[搜索工具摘要] {search_fallback}")

        fallback = "\n".join(combined_parts)
        target_msg.tool_call_summary = fallback

        # 无搜索工具则无需 LLM 摘要
        if not search_entries:
            return

        # tavily_search 已返回 AI 摘要时，直接使用，跳过 LLM 调用
        # 仅当所有搜索工具都是 tavily_search 时才跳过，混合其他搜索工具时仍走 LLM
        non_tavily_search = [e for e in search_entries if e.get("name") != "tavily_search"]
        if tavily_ai_answers and not non_tavily_search:
            tavily_summary = "；".join(tavily_ai_answers)[:max_chars]
            tavily_part = f"[搜索工具摘要] {tavily_summary}"
            if other_entries:
                other_raw = []
                for entry in other_entries:
                    if "result" in entry:
                        other_raw.append(f"{entry['name']}: {entry['result'][:80]}")
                    else:
                        other_raw.append(f"{entry['name']}({json.dumps(entry.get('args', {}), ensure_ascii=False)[:60]})")
                other_part_str = f"[调用结果] {'; '.join(other_raw)[:max_chars]}"
                target_msg.tool_call_summary = other_part_str + "\n" + tavily_part
            else:
                target_msg.tool_call_summary = tavily_part
            if config.DEBUG_LEVEL > 0:
                logger.info(f"[会话: {self.chat_key}] 工具调用摘要(Tavily AI): {target_msg.tool_call_summary}")
            _save_summary_log(self.chat_key, "tool", "", tavily_summary,
                              self.chat_preset.context_summary, target_msg.tool_call_summary)
            return

        # 如果已有任务在运行，跳过 LLM 调用（fallback 已就位）
        if self._tool_summary_task and not self._tool_summary_task.done():
            if config.DEBUG_LEVEL > 0:
                logger.info(f"[会话: {self.chat_key}] 工具摘要任务运行中，跳过本次 LLM 摘要")
            return

        # 启动后台 LLM 摘要任务（仅针对搜索工具）
        # 混合场景：tavily AI 摘要单独拼接，LLM 只总结其他搜索工具
        llm_search_entries = non_tavily_search if tavily_ai_answers else search_entries
        summary_input = json.dumps(llm_search_entries, ensure_ascii=False)
        other_part = f"[调用结果] {json.dumps(other_entries, ensure_ascii=False)}" if other_entries else ""
        trigger_part = f"\n触发问题: {trigger_text}" if trigger_text else ""
        tavily_part = f"[搜索工具摘要] {'；'.join(tavily_ai_answers)[:max_chars]}" if tavily_ai_answers else ""
        chat_key = self.chat_key
        request_profile = self._snapshot_request_profile()

        async def _do_tool_summary():
            prompt = (
                f"[工具调用记录]\n{summary_input}\n{trigger_part}\n\n"
                f"请以\"[搜索工具摘要]\"为开头，用一句话概括上述工具调用的用途和结果，不超过{max_chars}字。"
                f"不要加其他前缀或标签。"
            )
            tg = TextGenerator.instance
            summary_response = ""
            try:
                res, success = await tg.get_response(prompt, type='summarize', request_profile=request_profile)
                summary_response = res or ""
                if success and res and res.strip():
                    new_summary = res.strip()[:max_chars]
                    if not new_summary.startswith("[搜索工具摘要]"):
                        new_summary = f"[搜索工具摘要] {new_summary}"
                    # 拼接：other 结果 + tavily AI 摘要 + LLM 搜索摘要
                    parts = []
                    if other_part:
                        parts.append(other_part)
                    if tavily_part:
                        parts.append(tavily_part)
                    parts.append(new_summary)
                    target_msg.tool_call_summary = "\n".join(parts)
                    if config.DEBUG_LEVEL > 0:
                        logger.info(f"[会话: {chat_key}] 工具调用摘要(LLM): {target_msg.tool_call_summary}")
                    _save_summary_log(chat_key, "tool", prompt, summary_response,
                                      self.chat_preset.context_summary, target_msg.tool_call_summary)
                    return
            except Exception as e:
                summary_response = f"[异常] {e!r}"
                logger.warning(f"[会话: {chat_key}] 工具调用摘要 LLM 异常: {e!r}")
            if config.DEBUG_LEVEL > 0:
                logger.info(f"[会话: {chat_key}] 工具调用摘要 LLM 失败，保留 fallback")
            _save_summary_log(chat_key, "tool", prompt, summary_response,
                              self.chat_preset.context_summary, target_msg.tool_call_summary)

        self._tool_summary_task = asyncio.create_task(_do_tool_summary())

    @staticmethod
    def _message_user_id_for_impression(preset: PresetData, msg: ChatMessageData) -> str:
        """从结构化消息反查用户 ID；兼容旧数据中只存 sender 昵称的情况。"""
        user_id = str(getattr(msg, "user_id", "") or "").strip()
        if user_id:
            return user_id
        sender = (msg.sender or "").strip()
        if not sender:
            return ""
        if sender in preset.chat_impressions:
            return sender
        matches = [
            uid for uid, imp in preset.chat_impressions.items()
            if (imp.nickname or "").strip() == sender
        ]
        return matches[0] if len(matches) == 1 else ""

    async def _compress_prompt_messages_if_needed(self, preset: PresetData) -> None:
        """压缩对话历史：异步生成摘要，完成后截断窗口。摘要未完成前保留完整上下文用于对话。"""
        max_rounds = self._target_context_round_limit()
        buffer_rounds = self._history_buffer_round_limit()
        
        all_messages = [m for m in preset.prompt_messages if isinstance(m, ChatMessageData)]
        current_rounds = self._count_rounds(all_messages)
        overflow_rounds = current_rounds - max_rounds
        threshold = max(0, buffer_rounds - max_rounds)
        has_pending = bool(self._pending_overflow_text.strip())
        
        # 触发条件：当前有溢出，或之前截断时累积了待摘要文本
        if overflow_rounds <= threshold and not has_pending:
            return

        # 冷却检查：如果上次压缩失败，短时间内不再触发
        if self._compress_failure_time > 0:
            elapsed = time.time() - self._compress_failure_time
            if elapsed < _COMPRESS_COOLDOWN_SECONDS:
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"[会话: {self.chat_key}] 摘要压缩冷却中，剩余 {int(_COMPRESS_COOLDOWN_SECONDS - elapsed)} 秒")
                return
            # 冷却期已过，重置
            self._compress_failure_time = 0

        # 找到溢出轮的截断点：第 overflow_rounds+1 个真实 user 之前的完整消息段需要摘要/裁剪。
        user_count = 0
        cut_index = 0
        for i, msg in enumerate(preset.prompt_messages):
            if isinstance(msg, ChatMessageData) and msg.role == "user" and not msg.context_only:
                user_count += 1
                if user_count > overflow_rounds:
                    cut_index = i
                    break

        overflow_span = [
            m for m in preset.prompt_messages[:cut_index]
            if isinstance(m, ChatMessageData)
        ]
        pending_item_ids = set(self._pending_overflow_item_ids or set())
        compressing_item_ids = set(self._compressing_overflow_item_ids or set())
        new_overflow_messages = [
            m for m in overflow_span
            if id(m) not in pending_item_ids and id(m) not in compressing_item_ids
        ]
        new_remove_item_ids = {
            id(m) for m in new_overflow_messages
            if not m.context_only
        }

        # 提取本次溢出中实际产生互动的用户 ID
        current_active_ids = set()
        for msg in new_overflow_messages:
            if msg.role == "user" and not msg.context_only:
                uid = self._message_user_id_for_impression(preset, msg)
                if uid:
                    current_active_ids.add(uid)

        if not config.CONTEXT_SUMMARY_ENABLED:
            if cut_index > 0:
                # 保留 context_only 消息，只删除溢出的 user/assistant/tool 轮次
                del_indices = [i for i in range(cut_index) if not preset.prompt_messages[i].context_only]
                for i in sorted(del_indices, reverse=True):
                    del preset.prompt_messages[i]
                preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)
            self._pending_overflow_text = ""
            self._pending_overflow_item_ids = set()
            return

        # 不在此处截断消息，保留完整上下文供对话使用
        # 摘要任务完成后再删除已总结的溢出消息

        # 构建本次需要摘要的文本（新溢出 + 之前累积的 pending）
        new_overflow_text = "\n".join(
            self._format_prompt_message_for_summary(item)
            for item in new_overflow_messages
        )

        # 如果没有需要摘要的文本，直接返回
        if not new_overflow_text.strip() and not has_pending:
            self._pending_overflow_text = ""
            self._pending_overflow_item_ids = set()
            return

        # 如果有摘要任务正在运行，累积溢出文本后返回
        if self._compress_task and not self._compress_task.done():
            if new_overflow_text.strip():
                parts = [self._pending_overflow_text.strip(), new_overflow_text.strip()]
                self._pending_overflow_text = "\n".join(p for p in parts if p)
                self._pending_overflow_item_ids = pending_item_ids | new_remove_item_ids
                # 合并活跃用户 ID
                prev_ids = self._pending_overflow_user_ids or set()
                self._pending_overflow_user_ids = prev_ids | current_active_ids
            if config.DEBUG_LEVEL > 0:
                logger.info(f"[会话: {self.chat_key}] 摘要任务运行中，溢出文本已累积")
            return

        overflow_text = new_overflow_text
        if self._pending_overflow_text:
            overflow_text = self._pending_overflow_text + "\n" + new_overflow_text if new_overflow_text else self._pending_overflow_text
        remove_item_ids = pending_item_ids | new_remove_item_ids

        # 清除 pending（已合并到 overflow_text，由异步任务负责成功/失败时的管理）
        self._pending_overflow_text = ""
        self._pending_overflow_item_ids = set()
        active_user_ids = current_active_ids | (self._pending_overflow_user_ids or set())
        self._pending_overflow_user_ids = None

        # 捕获溢出消息 id，供任务完成后删除（使用 identity 而非 index，避免新消息导致偏移）
        self._compressing_overflow_item_ids = set(remove_item_ids)

        if config.DEBUG_LEVEL > 0:
            logger.info(f"[会话: {self.chat_key}][预设: {preset.preset_key}] 后台生成摘要中... (溢出文本 {len(overflow_text)} 字)")

        # 启动后台摘要任务
        chat_key = self.chat_key
        preset_key = preset.preset_key
        # 闭包捕获活跃用户 ID
        _active_user_ids = active_user_ids
        request_profile = self._snapshot_request_profile()

        async def _do_compress():
            tg = TextGenerator.instance
            max_retries = 2
            new_summary = None
            summary_prompt = ""
            summary_response = ""
            # 从配置读取摘要字数限制（中文约 1 token ≈ 1 字）
            max_summary_chars = max(200, tg.config.get('max_summary_tokens', 800))
            hard_summary_limit = max_summary_chars * 2
            # 读取最新的 previous_summary（可能已被前一个任务更新）
            latest_previous = preset.context_summary.strip()
            for attempt in range(max_retries):
                prompt = (
                    f"[已有压缩摘要]\n{latest_previous or '无'}\n\n"
                    f"[本次需要压缩的旧对话]\n{overflow_text}\n\n"
                    "请把旧对话压缩成一段持续可用的上下文摘要，全面保留事实、用户偏好、未完成事项、重要图片描述、已达成结论和关键对话细节。"
                    f"不要加入不存在的信息，控制在{max_summary_chars}字以内。"
                )
                summary_prompt = prompt
                try:
                    res, success = await tg.get_response(prompt, type='summarize', request_profile=request_profile)
                    summary_response = res or ""
                    if success and res and res.strip():
                        new_summary = res.strip()
                        break
                    logger.warning(f"[会话: {chat_key}] 摘要生成失败 (尝试 {attempt + 1}/{max_retries}): {res}")
                except Exception as e:
                    summary_response = f"[异常] {e!r}"
                    logger.warning(f"[会话: {chat_key}] 摘要生成异常 (尝试 {attempt + 1}/{max_retries}): {e!r}")
                if attempt < max_retries - 1:
                    await asyncio.sleep(0.5)

            if new_summary:
                # 硬截断：超出软限制2倍时才截断
                if len(new_summary) > hard_summary_limit:
                    new_summary = new_summary[:hard_summary_limit]
                preset.context_summary = new_summary
                self._compress_failure_time = 0  # 成功，重置冷却
                # 摘要成功，删除已总结的溢出消息（保留 context_only 消息）
                removed_count = 0
                if remove_item_ids:
                    before_count = len(preset.prompt_messages)
                    preset.prompt_messages = [
                        m for m in preset.prompt_messages
                        if id(m) not in remove_item_ids
                    ]
                    preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)
                    removed_count = before_count - len(preset.prompt_messages)
                # 不清空摘要任务运行期间新积累的 pending；这些消息尚未进入本次 overflow_text。
                self._pending_overflow_item_ids = (
                    self._existing_message_ids(preset, set(self._pending_overflow_item_ids or set()))
                    - set(remove_item_ids)
                )
                self._compressing_overflow_item_ids = set()
                if config.DEBUG_LEVEL > 0:
                    logger.info(
                        f"[会话: {chat_key}][预设: {preset_key}] 摘要生成完成 | "
                        f"摘要tokens={tg.cal_token_count(new_summary)} | "
                        f"已清理 {removed_count} 条溢出消息"
                    )
            else:
                # 失败时保留旧摘要和溢出消息（消息仍在 prompt_messages 中，下次重试时会重新捕获）
                # 恢复溢出文本以便下次重试，合并而非覆盖活跃用户 ID
                if self._pending_overflow_text:
                    self._pending_overflow_text = overflow_text + "\n" + self._pending_overflow_text
                else:
                    self._pending_overflow_text = overflow_text
                prev_ids = self._pending_overflow_user_ids or set()
                self._pending_overflow_user_ids = prev_ids | _active_user_ids
                self._pending_overflow_item_ids = (
                    set(self._pending_overflow_item_ids or set())
                    | self._existing_message_ids(preset, remove_item_ids)
                )
                self._compressing_overflow_item_ids = set()
                self._compress_failure_time = time.time()
                logger.warning(f"[会话: {chat_key}] 摘要生成失败，保留旧摘要和溢出消息（{_COMPRESS_COOLDOWN_SECONDS}秒冷却）")
                _save_error_log(chat_key, summary_prompt, summary_response, tg.cal_token_count(summary_prompt + summary_response))

            _save_summary_log(chat_key, "context", summary_prompt, summary_response,
                              preset.context_summary, preset.tool_call_summary)

            # 并入印象生成：仅对本次溢出中实际产生互动的用户生成印象（结合老印象）
            impression_results: Dict[str, str] = {}
            for uid in _active_user_ids:
                imp = preset.chat_impressions.get(uid)
                if not imp or not imp.chat_history:
                    continue
                nickname_info = f"（群昵称: {imp.nickname}）" if imp.nickname else ""
                imp_prompt = (
                    f"[用户{nickname_info}]\n"
                    f"[已有印象]\n{imp.chat_impression or '无'}\n\n"
                    f"[近期对话]\n{chr(10).join(imp.chat_history[-20:])}\n\n"
                    f"请以{preset_key}的视角简要更新对该用户的印象，包括该用户的性格特点、兴趣爱好、说话风格等关键信息，300字内，只输出印象文本。"
                )
                imp_response = ""
                try:
                    imp_res, imp_success = await tg.get_response(imp_prompt, type='summarize', request_profile=request_profile)
                    imp_response = imp_res or ""
                    if imp_success and imp_res and imp_res.strip():
                        imp_text = imp_res.strip()
                        # 硬截断：超出软限制2倍时才截断
                        if len(imp_text) > 600:
                            imp_text = imp_text[:600]
                        imp.chat_impression = imp_text
                        impression_results[uid] = imp.chat_impression
                except Exception as e:
                    imp_response = f"[异常] {e!r}"
                    _save_error_log(chat_key, imp_prompt, imp_response, tg.cal_token_count(imp_prompt + str(e)))
                    pass  # 印象生成失败不影响主流程

            # 保存印象日志
            if impression_results:
                _save_summary_log(chat_key, "impression", "", "",
                                  preset.context_summary, preset.tool_call_summary,
                                  impressions=impression_results)
                if config.DEBUG_LEVEL > 0:
                    logger.info(f"[会话: {chat_key}] 已生成 {len(impression_results)} 条用户印象")

            # 摘要和印象生成完毕后持久化，避免重启丢失
            PersistentDataManager.instance.save_to_file()

        def _on_compress_done(task: asyncio.Task) -> None:
            self._compressing_overflow_item_ids = set()
            try:
                task.result()
            except asyncio.CancelledError:
                if overflow_text.strip():
                    if self._pending_overflow_text:
                        self._pending_overflow_text = overflow_text + "\n" + self._pending_overflow_text
                    else:
                        self._pending_overflow_text = overflow_text
                    self._pending_overflow_item_ids = (
                        set(self._pending_overflow_item_ids or set())
                        | self._existing_message_ids(preset, remove_item_ids)
                    )
                    prev_ids = self._pending_overflow_user_ids or set()
                    self._pending_overflow_user_ids = prev_ids | _active_user_ids
                logger.warning(f"[会话: {chat_key}] 摘要任务被取消，已恢复待摘要溢出")
            except Exception as e:
                remaining_ids = {
                    id(m)
                    for m in preset.prompt_messages
                    if isinstance(m, ChatMessageData) and id(m) in remove_item_ids
                }
                if remaining_ids and overflow_text.strip():
                    if self._pending_overflow_text:
                        self._pending_overflow_text = overflow_text + "\n" + self._pending_overflow_text
                    else:
                        self._pending_overflow_text = overflow_text
                    self._pending_overflow_item_ids = (
                        set(self._pending_overflow_item_ids or set())
                        | set(remaining_ids)
                    )
                    prev_ids = self._pending_overflow_user_ids or set()
                    self._pending_overflow_user_ids = prev_ids | _active_user_ids
                self._compress_failure_time = time.time()
                logger.warning(
                    f"[会话: {chat_key}] 摘要任务异常退出，已清理压缩状态并保留可重试溢出: {e!r}"
                )

        self._compress_task = asyncio.create_task(_do_compress())
        self._compress_task.add_done_callback(_on_compress_done)
