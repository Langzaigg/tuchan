import asyncio
import json
import os
import re
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from tiktoken import Encoding, encoding_for_model

from .llm_tools import execute_tool, get_tool_schemas
from .logger import logger
from .singleton import Singleton

# 工具调用限制常量
MAX_TOTAL_TOOL_CALLS = 7  # 单轮总工具调用次数限制
MAX_SEARCH_TOOL_CALLS = 3  # 单轮博查搜索工具调用次数限制
SEARCH_TOOL_NAMES = {"bocha_search"}  # 博查搜索工具名称

# 伪造任务编号检测正则
# 匹配带前缀的格式（任务编号/单号 + 可选分隔符 + 可选markdown加粗 + 可选draw- + 6位字母数字）
_FAKE_TASK_ID_PREFIX_RE = re.compile(
    r'(?:任务编号|单号)[：:\s]*\*{0,2}(?:draw-)?[A-Za-z0-9]{6}\b\*{0,2}'
)
# 匹配不带前缀的格式（必须有draw- + 6位字母数字，可选markdown加粗）
_FAKE_TASK_ID_DRAW_RE = re.compile(
    r'\*{0,2}draw-[A-Za-z0-9]{6}\b\*{0,2}'
)


# 历史上下文中隐去单号的占位符（与 chat_prompt.py 一致）
_TASK_ID_PLACEHOLDER = '[请调用 generate_anima_image 画图工具获取编号]'


def _clean_fake_task_ids(content: str) -> str:
    """检测并清除伪造的任务编号，静默清理占位符回显。返回清理后的文本。"""
    original = content
    if _FAKE_TASK_ID_PREFIX_RE.search(content) or _FAKE_TASK_ID_DRAW_RE.search(content):
        content = _FAKE_TASK_ID_PREFIX_RE.sub('', content)
        content = _FAKE_TASK_ID_DRAW_RE.sub('', content)
    # 占位符是系统注入的，LLM 回显时静默移除（不告警）
    if _TASK_ID_PLACEHOLDER in content:
        content = content.replace(_TASK_ID_PLACEHOLDER, '')
    if content != original:
        if content.strip() == original.strip():
            # 仅移除了占位符，不告警
            return content.strip()
        logger.warning(f"[伪造任务编号] 已从回复中清除")
        return content.strip()
    return content

enc_cache: Dict[str, Encoding] = {}
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ChunkCallback = Callable[[str], Awaitable[None]]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _message_to_dict(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        d = dict(message)
    elif hasattr(message, "model_dump"):
        d = message.model_dump(exclude_none=True)
    elif hasattr(message, "dict"):
        d = message.dict(exclude_none=True)
    else:
        d = {
            "role": _get(message, "role", "assistant"),
            "content": _get(message, "content", ""),
            "tool_calls": _get(message, "tool_calls", None),
        }
        # 保留 reasoning_content（thinking 模式需要）
        reasoning_content = _get(message, "reasoning_content", None)
        if reasoning_content:
            d["reasoning_content"] = reasoning_content
    # 确保 content 不是 None；OpenAI API 要求 assistant 消息必须有 content
    if d.get("content") is None:
        d["content"] = ""
    return d


class TextGenerator(Singleton["TextGenerator"]):
    def init(self, api_keys: list, config: dict, proxy=None, base_url="", extra_prompt: str = ""):
        self.api_keys = api_keys or [""]
        self.key_index = 0
        self.config = config
        self.proxy = proxy
        self.base_url = base_url
        self.extra_prompt = extra_prompt or ""
        self.last_tool_outputs: List[Dict[str, Any]] = []
        self._tool_calling: bool = False  # 标记是否正在执行工具调用（受保护阶段）
        self._on_tool_done: Optional[Callable[[], None]] = None  # 工具调用完成回调
        self._current_chat_key: str = ""  # 当前会话的chat_key，供工具使用
        self._current_trigger_userid: str = ""  # 当前触发用户的userid，供工具使用
        self._pending_merge_input: Dict[str, Dict[str, Any]] = {}  # chat_key → 待合并的输入

    def switch_profile(self, profile_name: str, profile: Dict[str, Any]) -> str:
        """切换 OpenAI 配置 profile，返回切换结果描述"""
        self.api_keys = profile.get("api_keys", [""]) or [""]
        self.key_index = 0
        self.base_url = profile.get("base_url", "")
        self.use_socket_proxy = profile.get("use_socket_proxy", False)
        self.proxy = profile.get("proxy") or None
        self.multimodal = profile.get("multimodal", True)
        self.extra_prompt = profile.get("extra_prompt", "") or ""
        self.config = {
            "model": profile.get("model", ""),
            "model_mini": profile.get("model_mini", ""),
            "max_tokens": profile.get("max_tokens", 4096),
            "temperature": profile.get("temperature", 0.6),
            "top_p": profile.get("top_p"),
            "frequency_penalty": profile.get("frequency_penalty"),
            "presence_penalty": profile.get("presence_penalty"),
            "max_summary_tokens": profile.get("max_summary_tokens", 800),
            "timeout": profile.get("timeout", 60),
            "enable_stream": self.config.get("enable_stream", True),
        }
        proxy_info = f"socks:{self.proxy}" if self.use_socket_proxy and self.proxy else ("直连" if not self.proxy else self.proxy)
        return f"模型: {self.config['model']} | mini: {self.config['model_mini']} | base_url: {self.base_url or '默认'} | 代理: {proxy_info}"

    def _current_key(self) -> str:
        return self.api_keys[self.key_index % len(self.api_keys)]

    def _rotate_key(self) -> None:
        self.key_index = (self.key_index + 1) % len(self.api_keys)

    def _completion_kwargs(self, messages: List[Dict[str, Any]], type: str, stream: bool, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        # 当前 profile 不支持多模态时，剥离 image_url 内容
        if not getattr(self, 'multimodal', True):
            for msg in messages:
                content = msg.get("content")
                if isinstance(content, list):
                    text_parts = []
                    has_image = False
                    for item in content:
                        if isinstance(item, dict):
                            if item.get("type") == "text":
                                text_parts.append(item.get("text", ""))
                            elif item.get("type") == "image_url":
                                has_image = True
                    if has_image:
                        msg["content"] = "\n".join(text_parts) if text_parts else "[图片已省略]"
        model_key = "model_mini" if type in {"summarize", "impression"} else "model"
        # model_mini 为空时回退到 model
        model_name = self.config.get(model_key, "") or self.config.get("model", "")
        kwargs: Dict[str, Any] = {
            "model": self.config[model_key],
            "messages": messages,
            "temperature": self.config.get("temperature", 0.6),
            "max_tokens": self.config.get("max_summary_tokens" if type in {"summarize", "impression"} else "max_tokens", 1024),
            "timeout": self.config.get("timeout", 30),
            "stream": stream,
        }
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty"):
            value = self.config.get(optional_key)
            if value is not None:
                kwargs[optional_key] = value
        if self.base_url:
            kwargs["base_url"] = self.base_url
        # 代理：use_socket_proxy=True 时将 proxy 作为 socks 代理地址
        effective_proxy = self.proxy if (self.proxy and getattr(self, 'use_socket_proxy', False)) else None
        if effective_proxy:
            kwargs["proxy"] = effective_proxy
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _normalize_prompt(self, prompt, custom: dict = {}) -> List[Dict[str, Any]]:
        if isinstance(prompt, list):
            return prompt
        return [
            {"role": "system", "content": f"You must strictly follow the user's instructions to give {custom.get('bot_name', 'bot')}'s response."},
            {"role": "user", "content": prompt},
        ]

    async def _request_openai_compatible(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """直接调用 OpenAI-compatible API，不依赖 litellm"""
        import httpx

        model = kwargs.pop("model")
        messages = kwargs.pop("messages")
        stream = kwargs.pop("stream", False)
        base_url = kwargs.pop("base_url", "https://api.openai.com/v1")
        proxy = kwargs.pop("proxy", None)
        timeout = kwargs.pop("timeout", 30)
        tools = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)

        # 构建 URL
        url = f"{base_url.rstrip('/')}/chat/completions"

        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._current_key()}",
        }

        # 构建请求体
        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }

        # 添加可选参数
        if "temperature" in kwargs:
            body["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty"):
            if optional_key in kwargs:
                body[optional_key] = kwargs[optional_key]
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice

        # 构建 httpx 客户端
        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(url, headers=headers, json=body)
            if response.status_code >= 400:
                body_text = response.text[:1000]
                raise RuntimeError(f"HTTP {response.status_code}: {body_text}")
            return response.json()

    async def _stream_iter_openai(self, kwargs: Dict[str, Any]):
        """流式调用 OpenAI-compatible API YIELD 每个 SSE chunk。
        read 超时在每个 chunk 到达时重置，总体响应时间硬上限 5 分钟。
        """
        import httpx

        model = kwargs.pop("model")
        messages = kwargs.pop("messages")
        base_url = kwargs.pop("base_url", "https://api.openai.com/v1")
        proxy = kwargs.pop("proxy", None)
        timeout = kwargs.pop("timeout", 30)
        tools = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self._current_key()}",
        }

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
        }

        if "temperature" in kwargs:
            body["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty"):
            if optional_key in kwargs:
                body[optional_key] = kwargs[optional_key]
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice

        # connect/write/pool 用固定值，read 用配置值（每个 chunk 重置）
        http_timeout = httpx.Timeout(
            connect=10.0,
            read=float(timeout),
            write=30.0,
            pool=10.0,
        )
        client_kwargs: Dict[str, Any] = {"timeout": http_timeout}
        if proxy:
            client_kwargs["proxy"] = proxy

        MAX_TOTAL_SECONDS = 300.0  # 总体响应时间硬上限 5 分钟
        start_time = time.monotonic()

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code >= 400:
                    body_text = (await response.aread()).decode("utf-8", errors="replace")[:1000]
                    raise RuntimeError(f"HTTP {response.status_code}: {body_text}")
                async for line in response.aiter_lines():
                    # 总体时间硬上限
                    if time.monotonic() - start_time > MAX_TOTAL_SECONDS:
                        raise RuntimeError(f"流式响应超过总时间上限 {MAX_TOTAL_SECONDS:.0f}s，已中断")
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith("data: "):
                        line = line[6:]
                    if line == "[DONE]":
                        break
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue

    async def _acompletion(self, **kwargs) -> Dict[str, Any]:
        return await self._request_openai_compatible(kwargs)

    async def _stream_once(
        self,
        messages: List[Dict[str, Any]],
        type: str,
        tools: Optional[List[Dict[str, Any]]],
        on_text: Optional[ChunkCallback],
        on_reasoning: Optional[ChunkCallback],
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        kwargs = self._completion_kwargs(messages, type, True, tools)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_call_chunks: Dict[int, Dict[str, Any]] = {}

        async for chunk in self._stream_iter_openai(kwargs):
            choices = _get(chunk, "choices", [])
            if not choices:
                continue
            delta = _get(choices[0], "delta", {})

            reasoning = _get(delta, "reasoning_content") or _get(delta, "reasoning") or ""
            if reasoning:
                reasoning_parts.append(str(reasoning))
                if on_reasoning:
                    await on_reasoning(str(reasoning))

            content = _get(delta, "content") or ""
            if content:
                content = str(content)
                content_parts.append(content)
                if on_text:
                    await on_text(content)

            for tool_call in _get(delta, "tool_calls", []) or []:
                idx = int(_get(tool_call, "index", 0))
                state = tool_call_chunks.setdefault(
                    idx,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                call_id = _get(tool_call, "id", "")
                if call_id:
                    state["id"] = str(call_id)
                function = _get(tool_call, "function", {}) or {}
                if _get(function, "name"):
                    state["function"]["name"] += str(_get(function, "name"))
                    # 修复 provider 重复发送 name chunk 导致的双拼（如 generate_anima_imagegenerate_anima_image）
                    name = state["function"]["name"]
                    half = len(name) // 2
                    if half > 0 and name[:half] == name[half:]:
                        state["function"]["name"] = name[:half]
                if _get(function, "arguments"):
                    state["function"]["arguments"] += str(_get(function, "arguments"))

        return (
            "".join(content_parts),
            [v for _, v in sorted(tool_call_chunks.items()) if v["function"]["name"]],
            "".join(reasoning_parts),
        )

    async def _complete_once(self, messages: List[Dict[str, Any]], type: str, tools: Optional[List[Dict[str, Any]]]) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        kwargs = self._completion_kwargs(messages, type, False, tools)
        response = await self._acompletion(**kwargs)
        message = _get(_get(response, "choices", [])[0], "message", {})
        message_dict = _message_to_dict(message)
        content = str(message_dict.get("content") or "")
        tool_calls = message_dict.get("tool_calls") or []
        return content, tool_calls, message_dict

    async def _execute_tool_calls(self, messages: List[Dict[str, Any]], tool_calls: List[Dict[str, Any]], plugin_config) -> None:
        for idx, tool_call in enumerate(tool_calls):
            function = _get(tool_call, "function", {}) or {}
            name = _get(function, "name", "")
            raw_args = _get(function, "arguments", "{}") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {"raw": raw_args}

            logger.info(f"[工具调用] {name}({json.dumps(args, ensure_ascii=False)})")
            tool_call_id = _get(tool_call, "id", "") or ""
            if not tool_call_id:
                tool_call_id = f"call_{idx}"
            tool_content, attachments = await execute_tool(name, args, plugin_config)
            logger.info(f"[工具返回] {name} → {tool_content[:200]}{'...' if len(tool_content) > 200 else ''}")
            self.last_tool_outputs.extend(attachments)
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": tool_content})

    async def stream_response(
        self,
        prompt,
        type: str = "chat",
        custom: dict = {},
        plugin_config=None,
        on_text: Optional[ChunkCallback] = None,
        on_reasoning: Optional[ChunkCallback] = None,
    ) -> Tuple[str, bool, List[Dict[str, Any]], str]:
        messages = self._normalize_prompt(prompt, custom)
        self.last_tool_outputs = []
        tool_schemas = get_tool_schemas(plugin_config, self._current_chat_key) if plugin_config and type == "chat" else []
        max_rounds = getattr(plugin_config, "LLM_MAX_TOOL_ROUNDS", 0) if plugin_config else 0

        intermediate_texts: List[str] = []
        tool_messages: List[Dict[str, Any]] = []
        final_reasoning_content = ""

        # 工具调用计数器
        total_tool_calls = 0  # 总工具调用次数
        search_tool_calls = 0  # 联网搜索工具调用次数
        has_anima_call = False  # 是否已调用过 generate_anima_image 画图工具

        # 检测用户消息中是否包含画图相关关键词
        _DRAWING_KEYWORDS = ("画", "draw", "改图", "重画", "来一张", "整一张")
        _has_draw_request = False
        for msg in reversed(messages):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, list):
                    c = " ".join(item.get("text", "") for item in c if isinstance(item, dict) and item.get("type") == "text")
                _has_draw_request = any(kw in c.lower() for kw in _DRAWING_KEYWORDS)
                break

        # 获取画图模式并决定工具注入和拦截策略
        from .llm_tool_plugins import anima_generate as _ag
        _draw_mode = _ag.get_chat_mode(self._current_chat_key) if self._current_chat_key else "auto"
        if _draw_mode == "auto" and not _has_draw_request:
            # auto 模式：无画图关键词时过滤掉画图工具
            tool_schemas = [s for s in tool_schemas if s.get("function", {}).get("name") != "generate_anima_image"]
        # force 模式：画图关键词时启用拦截；其他模式不拦截
        _enable_intercept = (_draw_mode == "force" and _has_draw_request)
        # force 模式 + 画图关键词：预先注入引导消息，减少模型编造编号的概率
        if _enable_intercept:
            messages.append({
                "role": "system",
                "content": (
                    "用户要求画画，你意图画图时必须通过 tool_calls 调用 generate_anima_image 画图工具。"
                    "在 content 中回应的同时，必须附带 tool_calls。"
                    "任务编号只能由工具返回，禁止在 content 中编造。"
                ),
            })

        _fake_retry_count = 0  # 伪造任务编号重试次数
        _force_tools_next = False  # 强制下一轮提供工具定义

        round_idx = 0
        while True:
            try:
                # 最后一轮不带工具定义，强制模型直接回复；重试轮忽略此限制
                is_last_round = round_idx >= max_rounds and not _force_tools_next
                current_tools = tool_schemas if (not is_last_round or _force_tools_next) else None
                if _force_tools_next:
                    _force_tools_next = False

                # 最后一轮前，若有中间文本，注入提醒避免最终回复重复
                if is_last_round and intermediate_texts:
                    hint = "你在工具调用阶段已说过以下内容，请在最终回复中不要重复，只补充新信息：\n" + "\n".join(intermediate_texts)
                    messages.append({"role": "system", "content": hint})

                # 中间轮用缓冲回调，不输出给用户；最后一轮用真实回调
                buf_text: List[str] = []
                buf_reasoning: List[str] = []

                async def _buf_text(chunk: str) -> None:
                    buf_text.append(chunk)

                async def _buf_reasoning(chunk: str) -> None:
                    buf_reasoning.append(chunk)

                # 中间轮也输出给用户（最终会和最终轮分段）
                round_on_text = on_text
                round_on_reasoning = on_reasoning

                def _merge_intermediate(content: str) -> str:
                    """合并中间轮文本与最终文本，用双换行分隔，保持原始输出结构"""
                    parts = [t for t in intermediate_texts if t.strip()]
                    if content and content.strip():
                        parts.append(content.strip())
                    result = "\n\n".join(parts) if parts else (content or "")
                    # 清除伪造任务编号和占位符回显
                    result = _clean_fake_task_ids(result)
                    return result

                if self.config.get("enable_stream", True):
                    # 拦截模式：当工具仍可用但模型未调用时，缓冲流式内容以检查伪造编号
                    _intercept_final = (
                        _enable_intercept and not has_anima_call and _fake_retry_count == 0
                        and not is_last_round  # 工具仍可用（非最后一轮），模型本可调用但未调用
                    )
                    _draw_buf: Optional[List[str]] = None
                    if _intercept_final:
                        _draw_buf = []
                        async def _draw_on_text(chunk: str):
                            _draw_buf.append(chunk)
                        content, tool_calls, reasoning_content = await self._stream_once(
                            messages, type, current_tools, _draw_on_text, round_on_reasoning
                        )
                    else:
                        content, tool_calls, reasoning_content = await self._stream_once(
                            messages, type, current_tools, round_on_text, round_on_reasoning
                        )
                    if not tool_calls:
                        final_reasoning_content = reasoning_content or ""
                        merged = _merge_intermediate(content)
                        # 拦截模式：检查伪造编号
                        if _intercept_final:
                            has_fake = bool(
                                _FAKE_TASK_ID_PREFIX_RE.search(merged)
                                or _FAKE_TASK_ID_DRAW_RE.search(merged)
                                or _TASK_ID_PLACEHOLDER in merged
                                or 'generate_anima_image' in merged
                            )
                            if has_fake:
                                _fake_retry_count += 1
                                logger.warning(f"[伪造任务编号] 整轮结束后拦截伪造回复，触发重试")
                                _force_tools_next = True
                                intermediate_texts.clear()
                                messages.append({
                                    "role": "system",
                                    "content": (
                                        "你在回复中编造了任务编号但没有调用 generate_anima_image 画图工具。"
                                        "任务编号只能由画图工具返回给你，不能凭空编造。"
                                        "你必须通过 tool_calls 调用画图工具。重新回复。"
                                    ),
                                })
                                continue
                            # 通过检查，发送缓冲内容给用户
                            if on_text and _draw_buf:
                                await on_text("".join(_draw_buf))
                        return merged, True, tool_messages, final_reasoning_content
                    if is_last_round:
                        final_reasoning_content = reasoning_content or ""
                        return _merge_intermediate(content) if content or intermediate_texts else "工具调用已达上限，请基于已有结果回复。", True, tool_messages, final_reasoning_content
                    for i, tc in enumerate(tool_calls):
                        if not tc.get("id"):
                            tc["id"] = f"call_{i}"
                    assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    messages.append(assistant_msg)
                    tool_messages.append(assistant_msg)
                else:
                    content, tool_calls, message_dict = await self._complete_once(messages, type, current_tools)
                    if not tool_calls:
                        # 检测伪造任务编号并重试（拦截不发送）
                        # 仅在工具仍可用时拦截（非最后一轮），最后一轮模型无法调工具则跳过
                        if (_enable_intercept and not has_anima_call and _fake_retry_count == 0
                                and not is_last_round):
                            merged = _merge_intermediate(content)
                            has_fake = bool(
                                _FAKE_TASK_ID_PREFIX_RE.search(merged)
                                or _FAKE_TASK_ID_DRAW_RE.search(merged)
                                or _TASK_ID_PLACEHOLDER in merged
                                or 'generate_anima_image' in merged
                            )
                            if has_fake:
                                _fake_retry_count += 1
                                logger.warning(f"[伪造任务编号] 整轮结束后拦截伪造回复，触发重试")
                                _force_tools_next = True
                                intermediate_texts.clear()
                                messages.append({
                                    "role": "system",
                                    "content": (
                                        "你在回复中编造了任务编号但没有调用 generate_anima_image 画图工具。"
                                        "任务编号只能由画图工具返回给你，不能凭空编造。"
                                        "你必须通过 tool_calls 调用画图工具。重新回复。"
                                    ),
                                })
                                continue
                        final_reasoning_content = message_dict.get("reasoning_content", "")
                        if on_text and content:
                            await on_text(content)
                        return _merge_intermediate(content), True, tool_messages, final_reasoning_content
                    if is_last_round:
                        if on_text and content:
                            await on_text(content)
                        return _merge_intermediate(content) if content or intermediate_texts else "工具调用已达上限，请基于已有结果回复。", True, tool_messages, final_reasoning_content
                    tool_calls_from_dict = message_dict.get("tool_calls") or []
                    for i, tc in enumerate(tool_calls_from_dict):
                        if isinstance(tc, dict) and not tc.get("id"):
                            tc["id"] = f"call_{i}"
                    messages.append(message_dict)
                    tool_messages.append(message_dict)

                # 收集中间轮次的文本
                if content and content.strip():
                    intermediate_texts.append(content.strip())

                # 检测是否调用了画图工具
                if not is_last_round:
                    anima_in_tool_calls = any(
                        _get(_get(tc, "function", {}), "name", "") == "generate_anima_image"
                        for tc in tool_calls
                    )
                    if anima_in_tool_calls:
                        has_anima_call = True

                # 检查工具调用限制
                current_tool_count = len(tool_calls)
                current_search_count = sum(1 for tc in tool_calls if _get(_get(tc, "function", {}), "name", "") in SEARCH_TOOL_NAMES)
                
                # 检查总工具调用次数限制
                if total_tool_calls + current_tool_count > MAX_TOTAL_TOOL_CALLS:
                    logger.warning(f"单轮总工具调用次数超过限制: {total_tool_calls + current_tool_count} > {MAX_TOTAL_TOOL_CALLS}")
                    return f"单轮工具调用次数已达上限（{MAX_TOTAL_TOOL_CALLS}次），请基于已有结果回复。", True, tool_messages, final_reasoning_content
                
                # 检查联网搜索工具调用次数限制
                if search_tool_calls + current_search_count > MAX_SEARCH_TOOL_CALLS:
                    logger.warning(f"单轮联网搜索工具调用次数超过限制: {search_tool_calls + current_search_count} > {MAX_SEARCH_TOOL_CALLS}")
                    # 添加临时提示词，提醒大模型不要继续调用搜索工具
                    messages.append({
                        "role": "system",
                        "content": f"博查搜索工具调用次数已达上限（{MAX_SEARCH_TOOL_CALLS}次），请基于已有搜索结果回复，不要再调用搜索工具。"
                    })
                
                # 更新计数器
                total_tool_calls += current_tool_count
                search_tool_calls += current_search_count
                
                # 通知外部：工具调用阶段开始（受保护，不打断）
                self._tool_calling = True
                try:
                    await self._execute_tool_calls(messages, tool_calls, plugin_config)
                    # 收集tool消息
                    for msg in messages:
                        if msg.get("role") == "tool" and msg not in tool_messages:
                            tool_messages.append(msg)
                finally:
                    self._tool_calling = False
                    if self._on_tool_done:
                        self._on_tool_done()

                # 中间轮结束后输出分隔符，与最终轮分段
                if on_text and not is_last_round:
                    await on_text("\n\n")

                # 判断是否计入轮数：只有包含非 memory 工具时才增加轮数计数
                tool_names = {_get(_get(tc, "function", {}), "name", "") for tc in tool_calls}
                if tool_names - {"remember"}:
                    round_idx += 1
            except Exception as e:
                self._tool_calling = False
                if self._on_tool_done:
                    self._on_tool_done()
                logger.warning(f"LLM 请求失败: {e!r}")
                self._rotate_key()
                return f"请求大模型时发生错误: {e!r}", False, tool_messages, ""
        return "工具调用轮数过多，已停止本次回复。", False, tool_messages, ""

    async def get_response(self, prompt, type: str = "chat", custom: dict = {}) -> Tuple[str, bool]:
        chunks: List[str] = []

        async def collect(chunk: str):
            chunks.append(chunk)

        result = await self.stream_response(prompt, type=type, custom=custom, plugin_config=None, on_text=collect)
        return result[0], result[1]

    def consume_tool_outputs(self) -> List[Dict[str, Any]]:
        outputs = self.last_tool_outputs
        self.last_tool_outputs = []
        return outputs

    def set_pending_merge_input(self, chat_key: str, input_data: Dict[str, Any]) -> None:
        """设置待合并的输入"""
        if chat_key in self._pending_merge_input:
            # 已有待合并的输入，合并文本和图片
            existing = self._pending_merge_input[chat_key]
            old_text = existing.get("text", "")
            new_text = input_data.get("text", "")
            old_sender = existing.get("sender", "")
            new_sender = input_data.get("sender", "")
            
            # 合并文本，保留发送者信息
            merged_parts = []
            if old_text:
                merged_parts.append(f"{old_sender}: {old_text}" if old_sender else old_text)
            if new_text:
                merged_parts.append(f"{new_sender}: {new_text}" if new_sender else new_text)
            
            existing["text"] = "\n\n".join(merged_parts)
            existing["images"] = list(existing.get("images") or []) + list(input_data.get("images") or [])
            # 更新matcher为最新的
            existing["matcher"] = input_data.get("matcher")
            logger.info(f"[工具调用] 已合并输入到待处理: {chat_key}")
        else:
            self._pending_merge_input[chat_key] = input_data
            logger.info(f"[工具调用] 设置待合并输入: {chat_key}")

    def get_pending_merge_input(self, chat_key: str) -> Optional[Dict[str, Any]]:
        """获取并清除待合并的输入"""
        return self._pending_merge_input.pop(chat_key, None)

    def has_pending_merge_input(self, chat_key: str) -> bool:
        """检查是否有待合并的输入"""
        return chat_key in self._pending_merge_input

    @staticmethod
    def generate_msg_template(sender: str, msg: str, time_str: str = "") -> str:
        return f"{time_str}{sender}: {msg}"

    @staticmethod
    def _cal_text_tokens(text: str, model: str = "gpt-3.5-turbo") -> int:
        """计算纯文本的token数"""
        try:
            if model in enc_cache:
                enc = enc_cache[model]
            else:
                enc = encoding_for_model(model)
                enc_cache[model] = enc
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 2)

    @staticmethod
    def _cal_messages_tokens(messages: List[Dict[str, Any]], model: str = "gpt-3.5-turbo") -> int:
        """计算消息列表的token数，包括图片估算"""
        total = 0
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                total += TextGenerator._cal_text_tokens(content, model)
            elif isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            total += TextGenerator._cal_text_tokens(item.get("text", ""), model)
                        elif item.get("type") == "image_url":
                            total += 85  # OpenAI vision图片token估算
            total += 4  # 消息格式开销 (role, etc.)
        return total

    @staticmethod
    def cal_token_count(text_or_messages: Any, model: str = "gpt-3.5-turbo") -> int:
        """统一的token计算方法，支持字符串和消息列表"""
        if isinstance(text_or_messages, str):
            return TextGenerator._cal_text_tokens(text_or_messages, model)
        elif isinstance(text_or_messages, list):
            return TextGenerator._cal_messages_tokens(text_or_messages, model)
        return 0
