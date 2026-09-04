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


def _save_error_log(chat_key: str, prompt: Any, response: str, cost_tokens: int) -> None:
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
                      summary_prompt: Any, summary_response: str,
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
            profile["name"] = active_profile  # 稳定标识，供 per-profile 多 key 轮换索引用
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
        """同步生成工具调用摘要并一次写入终稿：搜索类工具生成摘要，其他工具保留原始结果。
        tavily 有 AI answer 时直接使用；其余搜索场景同步 await 一次 mini 模型调用，失败保留截断
        fallback。禁止 fallback→LLM 异步覆写两步写——异步回写会让后续请求的 prompt 前缀漂移。"""
        if not tool_messages:
            return

        SEARCH_TOOLS = {"tavily_search", "browse_url"}  # bocha 已改为 tavily 内部 fallback，不再产生独立工具结果
        IGNORED_TOOLS = {"generate_anima_image"}  # 工具结果不进历史；调用本身转为 [作画记录] 轻量留痕（见下）

        search_entries: List[Dict[str, Any]] = []
        other_entries: List[Dict[str, Any]] = []
        draw_descs: List[str] = []  # 作画留痕：直接取调用的 tags/nltags 作为历史作画内容摘要（不调 LLM）
        tavily_ai_answers: List[str] = []  # 收集 tavily_search 返回的 AI 摘要
        for msg in tool_messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    func = tc.get("function", {})
                    name = func.get("name", "")
                    try:
                        args = json.loads(func.get("arguments", "{}")) if isinstance(func.get("arguments"), str) else func.get("arguments", {})
                    except Exception:
                        args = {}
                    if name in IGNORED_TOOLS:
                        if name == "generate_anima_image" and isinstance(args, dict):
                            desc_parts = [str(args.get(k)).strip() for k in ("tags", "nltags") if str(args.get(k) or "").strip()]
                            draw_descs.append("；".join(desc_parts) if desc_parts else "（无画面描述）")
                        continue
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

        if not search_entries and not other_entries and not draw_descs:
            return

        if not target_msg or not target_msg.tool_calls:
            logger.warning(f"[会话: {self.chat_key}] 工具摘要缺少绑定的 assistant tool_calls 消息，已跳过")
            return

        # 作画轻量留痕：说明只完成了上述任务，后续新的作画请求必须重新调用画图工具。
        # 替代原先"画图轮在历史中零痕迹"的做法——模型看不到任何作画记录时容易模仿
        # 纯文本的「说画了→说图来了」模式而不实际调用工具。
        draw_line = ""
        if draw_descs:
            draw_line = (
                f"[作画记录] 以上作画任务已完成并发送，画面内容：{'；'.join(draw_descs)}。"
                "该记录仅表示上述任务已完成；之后任何新的作画请求（含修改/重画/再画一张）"
                "都必须重新调用 generate_anima_image 工具。"
            )

        def _raw_part(entries: List[Dict[str, Any]], sep: str = "; ") -> str:
            raw = []
            for entry in entries:
                if "result" in entry:
                    raw.append(f"{entry['name']}: {entry['result'][:80]}")
                else:
                    raw.append(f"{entry['name']}({json.dumps(entry.get('args', {}), ensure_ascii=False)[:60]})")
            return sep.join(raw)[:max_chars]

        # 截断 fallback（other 结果 + search 原文截断），任何路径失败都以此为终稿
        combined_parts = []
        if draw_line:
            combined_parts.append(draw_line)
        if other_entries:
            combined_parts.append(f"[调用结果] {_raw_part(other_entries)}")
        if search_entries:
            combined_parts.append(f"[搜索工具摘要] {_raw_part(search_entries, sep='；')}")
        fallback = "\n".join(combined_parts)

        # 无搜索工具则无需摘要，fallback 即终稿
        if not search_entries:
            target_msg.tool_call_summary = fallback
            return

        # tavily_search 已返回 AI 摘要时，直接使用，跳过 LLM 调用
        # 仅当所有搜索工具都是 tavily_search 时才跳过，混合其他搜索工具时仍走 LLM
        non_tavily_search = [e for e in search_entries if e.get("name") != "tavily_search"]
        if tavily_ai_answers and not non_tavily_search:
            tavily_summary = "；".join(tavily_ai_answers)[:max_chars]
            parts = []
            if draw_line:
                parts.append(draw_line)
            if other_entries:
                parts.append(f"[调用结果] {_raw_part(other_entries)}")
            parts.append(f"[搜索工具摘要] {tavily_summary}")
            target_msg.tool_call_summary = "\n".join(parts)
            if config.DEBUG_LEVEL > 0:
                logger.info(f"[会话: {self.chat_key}] 工具调用摘要(Tavily AI): {target_msg.tool_call_summary}")
            _save_summary_log(self.chat_key, "tool", "", tavily_summary,
                              self.chat_preset.context_summary, target_msg.tool_call_summary)
            return

        # 其余搜索场景：同步 await 一次 mini 模型摘要调用（调用方在回复落库时 await 本函数），
        # 成功写终稿，失败保留 fallback。单飞由调用方的回复处理串行保证，不再需要任务去重。
        chat_key = self.chat_key
        llm_search_entries = non_tavily_search if tavily_ai_answers else search_entries
        summary_input = json.dumps(llm_search_entries, ensure_ascii=False)
        other_part = f"[调用结果] {json.dumps(other_entries, ensure_ascii=False)}" if other_entries else ""
        trigger_part = f"\n触发问题: {trigger_text}" if trigger_text else ""
        tavily_part = f"[搜索工具摘要] {'；'.join(tavily_ai_answers)[:max_chars]}" if tavily_ai_answers else ""
        request_profile = self._snapshot_request_profile()
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
                parts = []
                if draw_line:
                    parts.append(draw_line)
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
        # LLM 失败：保留截断 fallback 为终稿
        target_msg.tool_call_summary = fallback
        if config.DEBUG_LEVEL > 0:
            logger.info(f"[会话: {chat_key}] 工具调用摘要 LLM 失败，保留 fallback")
        _save_summary_log(chat_key, "tool", prompt, summary_response,
                          self.chat_preset.context_summary, target_msg.tool_call_summary)

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

        # 找到溢出轮的截断点：第 overflow_rounds+1 个真实 user 是保留的最旧轮，
        # 其之前的完整消息段需要摘要/裁剪。注意保留轮 user 前的前导印象 system 与
        # 本轮 flush 的 context_only 均属于保留轮，需回溯排除，避免误纳入溢出部分。
        user_count = 0
        cut_index = 0
        for i, msg in enumerate(preset.prompt_messages):
            if isinstance(msg, ChatMessageData) and msg.role == "user" and not msg.context_only:
                user_count += 1
                if user_count > overflow_rounds:
                    cut_index = i
                    while cut_index > 0 and isinstance(preset.prompt_messages[cut_index - 1], ChatMessageData) and (
                        preset.prompt_messages[cut_index - 1].is_impression or preset.prompt_messages[cut_index - 1].context_only
                    ):
                        cut_index -= 1
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
        # context_only 已取消摘要豁免（append-only 普通历史条目），随溢出区间一并删除
        new_remove_item_ids = {id(m) for m in new_overflow_messages}

        # 提取本次溢出中实际产生互动的用户 ID
        current_active_ids = set()
        for msg in new_overflow_messages:
            if msg.role == "user" and not msg.context_only:
                uid = self._message_user_id_for_impression(preset, msg)
                if uid:
                    current_active_ids.add(uid)

        if not config.CONTEXT_SUMMARY_ENABLED:
            if cut_index > 0:
                # context_only 视为普通历史条目，随溢出区间一并删除
                del preset.prompt_messages[:cut_index]
                preset.prompt_messages = self._cleanup_orphan_history_messages(preset.prompt_messages)
            self._pending_overflow_text = ""
            self._pending_overflow_item_ids = set()
            return

        # 不在此处截断消息，保留完整上下文供对话使用
        # 摘要任务完成后再删除已总结的溢出消息

        # 构建本次需要摘要的文本（新溢出 + 之前累积的 pending）
        # 印象 system 不是对话内容，跳过它，避免把旧印象文本混入对话摘要
        new_overflow_text = "\n".join(
            self._format_prompt_message_for_summary(item)
            for item in new_overflow_messages
            if not getattr(item, "is_impression", False)
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
            # 字数软目标（中文约 1 token ≈ 1 字）：profile 显式设置的 max_summary_tokens 优先，
            # 否则回退到全局 CONTEXT_SUMMARY_TARGET_CHARS；硬截断固定为软目标的 2 倍
            max_summary_chars = max(100, int(request_profile.get('max_summary_tokens') or config.CONTEXT_SUMMARY_TARGET_CHARS))
            hard_summary_limit = max_summary_chars * 2
            # 读取最新的 previous_summary（可能已被前一个任务更新）
            latest_previous = preset.context_summary.strip()
            # 已长期保存的群记忆：交给摘要模型，避免摘要重复记录
            memory_block = ""
            if config.MEMORY_ACTIVE:
                memory_lines = [
                    f"{k}: {str(v)[:60]}"
                    for k, v in (self._get_chat_memory() or {}).items()
                    if v
                ]
                if memory_lines:
                    memory_block = "[已保存的长期记忆，无需在摘要中重复]\n" + "\n".join(memory_lines) + "\n\n"
            for attempt in range(max_retries):
                current_date = time.strftime('%Y-%m-%d')
                summary_prompt_text = (
                    f"{memory_block}"
                    f"[已有摘要]\n{latest_previous or '无'}\n\n"
                    f"[待合并的旧对话]\n{overflow_text}\n\n"
                    "把旧对话合并进已有摘要，按格式输出新的摘要。"
                )
                prompt = [
                    {"role": "system", "content": (
                        "你是上下文摘要助手。把旧对话合并进已有摘要，产出供后续对话使用的会话速查。\n\n"
                        "本摘要只负责会话/群层面的内容：\n"
                        "- 当前话题：正在讨论什么、进展到哪一步、各方观点和未解决的问题；保持话题间的连贯，能看出话题如何演变。\n"
                        "- 群历史：按日期（到天）记录对后续仍有价值的群事件、共同约定和关键决策，只留高信号条目。\n\n"
                        "不要记录：\n"
                        "- 用户的性格、爱好、说话风格等个人特质（由用户印象单独维护）。\n"
                        "- 已保存的长期记忆内容（称呼、生日等事实由 remember 记忆工具负责）。\n"
                        "- 功能调用过程本身（画图/搜索指令等）；但从中反映出的群体氛围或共同偏好可简记。\n\n"
                        "要求：\n"
                        "- 合并而非堆叠：新信息覆盖旧摘要中重复或过时的部分。\n"
                        "- 不编造，只保留对后续对话有价值的内容。\n"
                        f"- 总长控制在 {max_summary_chars} 字以内；超限时优先压缩 [群历史] 中的低价值旧条目。\n\n"
                        "输出格式：\n\n"
                        "[当前话题]\n"
                        "当前讨论焦点、进展和未决问题。\n\n"
                        "[群历史]\n"
                        f"按日期（到天）列出事件、约定和决策。当前日期为 {current_date}。"
                    )},
                    {"role": "user", "content": summary_prompt_text},
                ]
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
                # 摘要成功，删除已总结的溢出消息（context_only 不豁免，一并删除）
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
                _save_error_log(chat_key, summary_prompt, summary_response, tg.cal_token_count(summary_prompt) + tg.cal_token_count(summary_response))

            _save_summary_log(chat_key, "context", summary_prompt, summary_response,
                              preset.context_summary, preset.tool_call_summary)

            # 并入印象生成：仅对本次溢出中实际产生互动的用户生成印象（结合老印象）。
            # 更新依据为本次溢出部分中该用户的对话（而非累积的 chat_history）：
            # 摘要裁剪掉溢出轮次（含其绑定的旧印象 system）后，下次该用户触发时
            # 上下文中不再有他的印象，自然注入此处更新后的新印象，避免旧印象残留。
            impression_results: Dict[str, str] = {}
            # 先串行构建各用户的印象 prompt（纯本地计算），再并发请求 LLM，
            # 多用户溢出时印象生成总耗时从 Σt 降为 max(t)
            imp_tasks: List[tuple] = []  # (uid, imp, imp_prompt, imp_hard_limit)
            for uid in _active_user_ids:
                imp = preset.chat_impressions.get(uid)
                if not imp:
                    continue
                # 从溢出部分提取该用户的 user 消息作为印象更新依据
                user_lines: List[str] = []
                for msg in new_overflow_messages:
                    if getattr(msg, "is_impression", False):
                        continue
                    if msg.role == "user" and not msg.context_only:
                        msg_uid = self._message_user_id_for_impression(preset, msg)
                        if msg_uid == uid:
                            sender = (msg.sender or "").strip() or "用户"
                            text = (msg.text or "").strip()
                            user_lines.append(f"{sender}: {text}")
                if not user_lines:
                    continue
                nickname_info = f"（群昵称: {imp.nickname}）" if imp.nickname else ""
                # 字数软目标：全局 IMPRESSION_TARGET_CHARS；硬截断固定为软目标的 2 倍
                imp_target_chars = max(100, int(config.IMPRESSION_TARGET_CHARS))
                imp_hard_limit = imp_target_chars * 2
                # 已保存的用户记忆会与印象并列注入上下文，提示印象避免重复记录
                user_mem_block = ""
                if config.MEMORY_ACTIVE:
                    user_mem_lines = [
                        f"{k}: {str(v)[:60]}"
                        for k, v in (self._get_user_memory(uid) or {}).items()
                        if v
                    ]
                    if user_mem_lines:
                        user_mem_block = "[已保存的用户记忆，无需在印象中重复]\n" + "\n".join(user_mem_lines) + "\n\n"
                imp_prompt = [
                    {"role": "system", "content": (
                        f"你是{preset_key}。根据近期对话更新对某用户的印象，供后续对话参考。\n\n"
                        "印象只负责用户个人层面的内容：\n"
                        "- 性格与说话风格、爱好与兴趣、习惯与偏好倾向、与你的关系和互动模式。\n\n"
                        "不要记录：\n"
                        "- 群话题、事件、约定（由上下文摘要负责）。\n"
                        "- 已保存的用户记忆内容（称呼、生日等事实会与印象并列注入，无需重复）。\n"
                        "- 功能调用过程本身（如画图/搜索指令）；但指令反映出的偏好倾向可以记，"
                        "用户对画图结果的评价（\"好看\"\"太暗了\"等）可反映审美偏好。\n\n"
                        "要求：\n"
                        "- 更新而非追加：新观察覆盖或修正旧印象，删掉不再适用的描述。\n"
                        "- 只写有依据的内容，不编造。\n"
                        f"- {imp_target_chars} 字以内，直接输出印象文本，不要前缀或标签。"
                    )},
                    {"role": "user", "content": (
                        f"[用户{nickname_info}]\n"
                        f"{user_mem_block}"
                        f"[已有印象]\n{imp.chat_impression or '无'}\n\n"
                        f"[近期对话]\n{chr(10).join(user_lines)}\n\n"
                        f"请以{preset_key}的视角更新对该用户的印象，{imp_target_chars} 字以内，只输出印象文本。"
                    )},
                ]
                imp_tasks.append((uid, imp, imp_prompt, imp_hard_limit))

            async def _gen_impression(uid: str, imp_prompt: list, imp_hard_limit: int):
                """单个用户的印象生成请求；失败记录 error 日志并返回 None，不影响其他用户。"""
                imp_response = ""
                try:
                    imp_res, imp_success = await tg.get_response(imp_prompt, type='summarize', request_profile=request_profile)
                    imp_response = imp_res or ""
                    if imp_success and imp_res and imp_res.strip():
                        imp_text = imp_res.strip()
                        # 硬截断：超出软目标 2 倍时才截断
                        if len(imp_text) > imp_hard_limit:
                            imp_text = imp_text[:imp_hard_limit]
                        return uid, imp_text
                except Exception as e:
                    imp_response = f"[异常] {e!r}"
                    _save_error_log(chat_key, imp_prompt, imp_response, tg.cal_token_count(imp_prompt) + tg.cal_token_count(imp_response))
                return None

            if imp_tasks:
                for result in await asyncio.gather(*(
                    _gen_impression(uid, imp_prompt, imp_hard_limit)
                    for uid, _imp, imp_prompt, imp_hard_limit in imp_tasks
                )):
                    if not result:
                        continue
                    uid, imp_text = result
                    imp = preset.chat_impressions.get(uid)
                    if imp is None:
                        continue
                    imp.chat_impression = imp_text
                    impression_results[uid] = imp.chat_impression

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
