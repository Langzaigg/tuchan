import asyncio
import copy
import json
import os
import re
import time
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Dict, List, Optional, Set, Tuple

from tiktoken import Encoding, encoding_for_model

from .llm_tools import execute_tool, get_tool_schemas
from .logger import logger
from .singleton import Singleton

# 工具调用限制常量（从配置读取，这些是默认值）
MAX_TOTAL_TOOL_CALLS = 15  # 单轮总工具调用次数限制，实际值从 LLM_MAX_TOTAL_TOOL_CALLS 配置读取
MAX_SEARCH_TOOL_CALLS = 3  # 单轮搜索工具调用次数限制
TERMINAL_TOOLS = {"generate_anima_image", "remember"}  # 终端工具：超限后仍允许最后一次调用
SEARCH_TOOL_NAMES = {"bocha_search", "tavily_search"}  # 搜索工具名称

_CURRENT_CHAT_KEY: ContextVar[str] = ContextVar("naturel_gpt_current_chat_key", default="")
_CURRENT_TRIGGER_USERID: ContextVar[str] = ContextVar("naturel_gpt_current_trigger_userid", default="")

_TOTAL_TOOL_LIMIT_TEXT = f"工具调用次数已达上限（{MAX_TOTAL_TOOL_CALLS}次）。停止继续调用工具，基于已有工具结果直接回答当前用户。"
_SEARCH_TOOL_LIMIT_TEXT = f"搜索工具调用次数已达上限（{MAX_SEARCH_TOOL_CALLS}次），请基于已有搜索结果回复，不要再调用搜索工具。"
_INTERNAL_CONTROL_PATTERNS = (
    re.compile(r"单轮?工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有结果回复[。.]?"),
    re.compile(r"工具调用次数已达上限（?\d+次）?[。，,]?\s*停止继续调用工具，基于已有工具结果直接回答当前用户[。.]?"),
    re.compile(r"博查搜索工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有搜索结果回复，不要再调用搜索工具[。.]?"),
    re.compile(r"搜索工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有搜索结果回复，不要再调用搜索工具[。.]?"),
)
_MODEL_REQUEST_ERROR_PREFIX = "请求大模型时发生错误:"

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
_TASK_ID_PLACEHOLDER_RE = re.compile(
    r'\[[^\]\n]*(?:编号已隐藏|请调用\s*generate_anima_image|generate_anima_image\s*画图工具)[^\]\n]*\]'
)


def _normalize_draw_cleanup(content: str) -> str:
    content = re.sub(r'[，,]\s*[，,]+', '，', content)
    content = re.sub(r'\s+([，。,.!?！？])', r'\1', content)
    content = re.sub(r'([，,])\s*([。.!?！？])', r'\2', content)
    content = re.sub(r'\n{3,}', '\n\n', content)
    return content.strip()


def sanitize_internal_control_text(content: str) -> str:
    """清理仅供模型内部遵循的控制提示，防止其进入群消息和历史。"""
    if not content:
        return content
    content = content.replace(_TOTAL_TOOL_LIMIT_TEXT, "")
    content = content.replace(_SEARCH_TOOL_LIMIT_TEXT, "")
    for pattern in _INTERNAL_CONTROL_PATTERNS:
        content = pattern.sub("", content)
    # 过滤 LLM 输出的 tool_call XML 标签（模型可能在 content 中输出 tool_call 格式）
    content = re.sub(r'<tool_call>.*?</tool_call>', '', content, flags=re.DOTALL)
    return _normalize_draw_cleanup(content)


def is_model_request_error_text(content: str) -> bool:
    """判断文本是否为插件内部的大模型请求异常，而不是可进入对话历史的 assistant 回复。"""
    if not content:
        return False
    text = str(content).strip()
    lower_text = text.lower()
    return (
        text.startswith(_MODEL_REQUEST_ERROR_PREFIX)
        or ("runtimeerror('http " in lower_text and "error from provider" in lower_text)
        or ("runtimeerror(\"http " in lower_text and "error from provider" in lower_text)
    )


def _clean_placeholder_echo(content: str) -> str:
    """静默清理占位符回显（始终调用）。返回清理后的文本。"""
    if not content:
        return content
    content = content.replace(_TASK_ID_PLACEHOLDER, '')
    content = _TASK_ID_PLACEHOLDER_RE.sub('', content)
    return _normalize_draw_cleanup(content)


def _clean_fake_task_ids(content: str, warn: bool = False) -> str:
    """检测并清除伪造的任务编号（仅在未调用画图工具时调用）。返回清理后的文本。"""
    original = content
    if _FAKE_TASK_ID_PREFIX_RE.search(content) or _FAKE_TASK_ID_DRAW_RE.search(content):
        content = _FAKE_TASK_ID_PREFIX_RE.sub('', content)
        content = _FAKE_TASK_ID_DRAW_RE.sub('', content)
        if warn and content.strip() != original.strip():
            logger.warning(f"[伪造任务编号] 已从回复中清除")
    return _normalize_draw_cleanup(content)


def _contains_fake_draw_reply(content: str) -> bool:
    """判断模型是否在未调用画图工具时伪造了画图确认信息。"""
    return bool(
        _FAKE_TASK_ID_PREFIX_RE.search(content)
        or _FAKE_TASK_ID_DRAW_RE.search(content)
        or _TASK_ID_PLACEHOLDER_RE.search(content)
        or _TASK_ID_PLACEHOLDER in content
        or "generate_anima_image" in content
    )


def sanitize_draw_reply_text(content: str, allow_task_ids: bool = True) -> str:
    """清理不应直接出现在聊天中的画图占位符；未调用工具时也清理伪任务编号。"""
    content = _clean_placeholder_echo(content)
    if not allow_task_ids:
        content = _clean_fake_task_ids(content)
    content = sanitize_internal_control_text(content)
    return content.strip()

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
    # 修复 content 列表中 text 项缺失 text 字段的问题（Xiaomi/mimo 等 provider 可能返回 {"type":"text"} 无 text）
    # 纯文本列表简化为字符串，避免 provider 兼容性问题
    if isinstance(d.get("content"), list):
        has_non_text = any(
            isinstance(item, dict) and item.get("type") != "text"
            for item in d["content"]
        )
        for item in d["content"]:
            if isinstance(item, dict) and item.get("type") == "text" and "text" not in item:
                item["text"] = ""
        if not has_non_text:
            d["content"] = "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in d["content"]
            )
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
        self._last_tool_outputs_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        self._tool_calling: bool = False  # 兼容旧读取：任意会话是否正在执行工具调用
        self._tool_calling_chat_keys: Set[str] = set()  # chat_key → 正在执行工具调用（受保护阶段）
        self._on_tool_done_callbacks: Dict[str, Callable[[], None]] = {}  # chat_key → 工具调用完成回调
        self._current_chat_key: str = ""  # 当前会话的chat_key，供工具使用
        self._current_trigger_userid: str = ""  # 当前触发用户的userid，供工具使用
        self._pending_merge_input: Dict[str, Dict[str, Any]] = {}  # chat_key → 待合并的输入

    @property
    def _current_chat_key(self) -> str:
        return _CURRENT_CHAT_KEY.get()

    @_current_chat_key.setter
    def _current_chat_key(self, value: str) -> None:
        _CURRENT_CHAT_KEY.set(str(value or ""))

    @property
    def _current_trigger_userid(self) -> str:
        return _CURRENT_TRIGGER_USERID.get()

    @_current_trigger_userid.setter
    def _current_trigger_userid(self, value: str) -> None:
        _CURRENT_TRIGGER_USERID.set(str(value or ""))

    def is_tool_calling(self, chat_key: Optional[str] = None) -> bool:
        if not hasattr(self, "_tool_calling_chat_keys"):
            self._tool_calling_chat_keys = set()
        if chat_key:
            return chat_key in self._tool_calling_chat_keys
        return bool(self._tool_calling_chat_keys)

    def set_tool_done_callback(self, chat_key: str, callback: Callable[[], None]) -> None:
        if not hasattr(self, "_on_tool_done_callbacks"):
            self._on_tool_done_callbacks = {}
        self._on_tool_done_callbacks[chat_key] = callback

    def _set_tool_calling(self, chat_key: str, active: bool) -> None:
        if not hasattr(self, "_tool_calling_chat_keys"):
            self._tool_calling_chat_keys = set()
        if chat_key:
            if active:
                self._tool_calling_chat_keys.add(chat_key)
            else:
                self._tool_calling_chat_keys.discard(chat_key)
        self._tool_calling = bool(self._tool_calling_chat_keys)

    def _notify_tool_done(self, chat_key: str) -> None:
        if not hasattr(self, "_on_tool_done_callbacks"):
            self._on_tool_done_callbacks = {}
        callback = self._on_tool_done_callbacks.get(chat_key)
        if callback:
            callback()

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

    def _request_state(self, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if profile:
            api_keys = profile.get("api_keys", [""]) or [""]
            request_config = {
                "model": profile.get("model", ""),
                "model_mini": profile.get("model_mini", ""),
                "max_tokens": profile.get("max_tokens", 4096),
                "temperature": profile.get("temperature", 0.6),
                "top_p": profile.get("top_p"),
                "frequency_penalty": profile.get("frequency_penalty"),
                "presence_penalty": profile.get("presence_penalty"),
                "max_summary_tokens": profile.get("max_summary_tokens", 800),
                "timeout": profile.get("timeout", 60),
                "enable_stream": profile.get("enable_stream", self.config.get("enable_stream", True)),
            }
            return {
                "api_key": api_keys[0],
                "config": request_config,
                "base_url": profile.get("base_url", ""),
                "proxy": profile.get("proxy") or None,
                "use_socket_proxy": profile.get("use_socket_proxy", False),
                "multimodal": profile.get("multimodal", True),
            }
        return {
            "api_key": self._current_key(),
            "config": dict(self.config),
            "base_url": self.base_url,
            "proxy": self.proxy,
            "use_socket_proxy": getattr(self, "use_socket_proxy", False),
            "multimodal": getattr(self, "multimodal", True),
        }

    def _completion_kwargs(
        self,
        messages: List[Dict[str, Any]],
        type: str,
        stream: bool,
        tools: Optional[List[Dict[str, Any]]] = None,
        request_state: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        state = request_state or self._request_state()
        request_config = state.get("config") or self.config
        # 当前 profile 不支持多模态时，剥离 image_url 内容
        if not state.get("multimodal", True):
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
        model_name = request_config.get(model_key, "") or request_config.get("model", "")
        kwargs: Dict[str, Any] = {
            "model": model_name,
            "messages": messages,
            "temperature": request_config.get("temperature", 0.6),
            "max_tokens": request_config.get("max_summary_tokens" if type in {"summarize", "impression"} else "max_tokens", 1024),
            "timeout": request_config.get("timeout", 30),
            "stream": stream,
            "api_key": state.get("api_key", ""),
        }
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty"):
            value = request_config.get(optional_key)
            if value is not None:
                kwargs[optional_key] = value
        if state.get("base_url"):
            kwargs["base_url"] = state["base_url"]
        # 代理：use_socket_proxy=True 时将 proxy 作为 socks 代理地址
        effective_proxy = state.get("proxy") if (state.get("proxy") and state.get("use_socket_proxy")) else None
        if effective_proxy:
            kwargs["proxy"] = effective_proxy
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        return kwargs

    def _normalize_prompt(self, prompt, custom: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        custom = custom or {}
        if isinstance(prompt, list):
            for msg in prompt:
                if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                    # 修复 content 列表中 text 项缺失 text 字段的问题
                    for item in msg["content"]:
                        if isinstance(item, dict) and item.get("type") == "text" and "text" not in item:
                            item["text"] = ""
                    has_non_text = any(
                        isinstance(item, dict) and item.get("type") != "text"
                        for item in msg["content"]
                    )
                    if not has_non_text:
                        # 如果 content 列表中只有 text 项（没有 image_url 等），转换为纯字符串
                        # 提高与不支持多部分内容格式的 API（如 Xiaomi MiMo）的兼容性
                        text_parts = [
                            item.get("text", "")
                            for item in msg["content"]
                            if isinstance(item, dict) and item.get("type") == "text"
                        ]
                        msg["content"] = "\n".join(text_parts) if text_parts else ""
                    else:
                        # 有非 text 项（如图片）时，确保 text 项不为空，避免 Xiaomi 等 provider 报错
                        text_items = [
                            item for item in msg["content"]
                            if isinstance(item, dict) and item.get("type") == "text"
                        ]
                        if text_items and all(not (item.get("text") or "").strip() for item in text_items):
                            text_items[0]["text"] = "[图片]"
                        elif text_items:
                            for ti in text_items:
                                if not (ti.get("text") or "").strip():
                                    ti["text"] = "[图片]"
            return prompt
        return [
            {"role": "system", "content": f"You must strictly follow the user's instructions to give {custom.get('bot_name', 'bot')}'s response."},
            {"role": "user", "content": prompt},
        ]

    async def _request_openai_compatible(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """直接调用 OpenAI-compatible API，不依赖 litellm"""
        import httpx

        # 浅拷贝避免 pop 污染调用方的 kwargs
        kwargs = dict(kwargs)

        model = kwargs.pop("model")
        messages = kwargs.pop("messages")
        stream = kwargs.pop("stream", False)
        base_url = kwargs.pop("base_url", "https://api.openai.com/v1")
        proxy = kwargs.pop("proxy", None)
        timeout = kwargs.pop("timeout", 30)
        api_key = kwargs.pop("api_key", None) or self._current_key()
        tools = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)

        # 构建 URL
        url = f"{base_url.rstrip('/')}/chat/completions"

        # 构建请求头
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
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

        # 浅拷贝避免 pop 污染调用方的 kwargs
        kwargs = dict(kwargs)

        model = kwargs.pop("model")
        messages = kwargs.pop("messages")
        base_url = kwargs.pop("base_url", "https://api.openai.com/v1")
        proxy = kwargs.pop("proxy", None)
        timeout = kwargs.pop("timeout", 30)
        api_key = kwargs.pop("api_key", None) or self._current_key()
        tools = kwargs.pop("tools", None)
        tool_choice = kwargs.pop("tool_choice", None)

        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
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
        request_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        kwargs = self._completion_kwargs(messages, type, True, tools, request_state)
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

    async def _complete_once(
        self,
        messages: List[Dict[str, Any]],
        type: str,
        tools: Optional[List[Dict[str, Any]]],
        request_state: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        kwargs = self._completion_kwargs(messages, type, False, tools, request_state)
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
            chat_key = self._current_chat_key
            if chat_key:
                if not hasattr(self, "_last_tool_outputs_by_chat"):
                    self._last_tool_outputs_by_chat = {}
                self._last_tool_outputs_by_chat.setdefault(chat_key, []).extend(attachments)
            else:
                self.last_tool_outputs.extend(attachments)
            messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": tool_content})

    async def stream_response(
        self,
        prompt,
        type: str = "chat",
        custom: Optional[Dict[str, Any]] = None,
        plugin_config=None,
        request_profile: Optional[Dict[str, Any]] = None,
        on_text: Optional[ChunkCallback] = None,
        on_reasoning: Optional[ChunkCallback] = None,
    ) -> Tuple[str, bool, List[Dict[str, Any]], str]:
        custom = custom or {}
        messages = copy.deepcopy(self._normalize_prompt(prompt, custom))
        request_chat_key = self._current_chat_key
        request_trigger_userid = self._current_trigger_userid
        request_state = self._request_state(request_profile)
        self.last_tool_outputs = []
        if request_chat_key:
            self._last_tool_outputs_by_chat[request_chat_key] = []
        tool_schemas = get_tool_schemas(plugin_config, request_chat_key) if plugin_config and type == "chat" else []
        max_rounds = getattr(plugin_config, "LLM_MAX_TOOL_ROUNDS", 0) if plugin_config else 0
        max_total_tool_calls = getattr(plugin_config, "LLM_MAX_TOTAL_TOOL_CALLS", MAX_TOTAL_TOOL_CALLS) if plugin_config else MAX_TOTAL_TOOL_CALLS

        intermediate_texts: List[str] = []
        tool_messages: List[Dict[str, Any]] = []
        final_reasoning_content = ""

        # 工具调用计数器
        total_tool_calls = 0  # 总工具调用次数
        search_tool_calls = 0  # 联网搜索工具调用次数
        has_anima_call = False  # 是否已调用过 generate_anima_image 画图工具
        _allow_terminal_tools = False  # 工具超限后是否允许终端工具（画图/记忆）作为最后一轮
        internal_control_injected = False  # 是否向模型注入过内部控制提示

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
        _draw_mode = _ag.get_chat_mode(request_chat_key) if request_chat_key else "auto"
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
                # 工具超限后，最后一轮仍提供终端工具（画图/记忆）供模型完成关键操作
                if is_last_round and _allow_terminal_tools and not _force_tools_next:
                    current_tools = [s for s in tool_schemas if s.get("function", {}).get("name") in TERMINAL_TOOLS]
                else:
                    current_tools = tool_schemas if (not is_last_round or _force_tools_next) else None
                if _force_tools_next:
                    _force_tools_next = False

                # 最后一轮前，若有中间文本，注入提醒避免最终回复重复（终端工具轮不注入）
                if is_last_round and not _allow_terminal_tools and intermediate_texts:
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

                def _join_intermediate(content: str) -> str:
                    """合并中间轮文本与最终文本，用双换行分隔，保持原始输出结构"""
                    parts = [t for t in intermediate_texts if t.strip()]
                    if content and content.strip():
                        parts.append(content.strip())
                    return "\n\n".join(parts) if parts else (content or "")

                def _merge_intermediate(content: str) -> str:
                    raw = _join_intermediate(content)
                    return sanitize_draw_reply_text(raw, allow_task_ids=has_anima_call)

                control_stream_buf: Optional[List[str]] = None
                effective_on_text = round_on_text
                if internal_control_injected and round_on_text:
                    control_stream_buf = []

                    async def _buffer_control_text(chunk: str) -> None:
                        control_stream_buf.append(chunk)

                    effective_on_text = _buffer_control_text

                async def _flush_control_stream_buffer() -> None:
                    if not on_text or not control_stream_buf:
                        return
                    safe_text = sanitize_draw_reply_text("".join(control_stream_buf), allow_task_ids=has_anima_call)
                    if safe_text:
                        await on_text(safe_text)

                if (request_state.get("config") or {}).get("enable_stream", True):
                    # 画图请求且尚未调用画图工具时先缓冲，避免伪编号/占位符在流式发送中泄漏。
                    # force 模式额外触发一次重试；其他模式只发送清理后的文本。
                    _intercept_final = (
                        _has_draw_request and not has_anima_call
                        and not is_last_round  # 工具仍可用（非最后一轮），模型本可调用但未调用
                    )
                    _draw_buf: Optional[List[str]] = None
                    if _intercept_final:
                        _draw_buf = []
                        async def _draw_on_text(chunk: str):
                            _draw_buf.append(chunk)
                        content, tool_calls, reasoning_content = await self._stream_once(
                            messages, type, current_tools, _draw_on_text, round_on_reasoning, request_state
                        )
                    else:
                        content, tool_calls, reasoning_content = await self._stream_once(
                            messages, type, current_tools, effective_on_text, round_on_reasoning, request_state
                        )
                    if not tool_calls:
                        final_reasoning_content = reasoning_content or ""
                        raw_merged = _join_intermediate(content)
                        merged = _merge_intermediate(content)
                        # 拦截模式：检查伪造编号
                        if _intercept_final:
                            has_fake = _contains_fake_draw_reply(raw_merged)
                            if _enable_intercept and has_fake and _fake_retry_count == 0:
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
                            # 通过检查或已重试过：只发送清理后的缓冲内容，避免占位符泄漏
                            if on_text and _draw_buf:
                                safe_text = sanitize_draw_reply_text("".join(_draw_buf), allow_task_ids=False)
                                if safe_text:
                                    await on_text(safe_text)
                        elif control_stream_buf is not None:
                            await _flush_control_stream_buffer()
                        return merged, True, tool_messages, final_reasoning_content
                    if is_last_round and not _allow_terminal_tools:
                        final_reasoning_content = reasoning_content or ""
                        if control_stream_buf is not None:
                            await _flush_control_stream_buffer()
                        if content or intermediate_texts:
                            return _merge_intermediate(content), True, tool_messages, final_reasoning_content
                        return "", False, tool_messages, final_reasoning_content
                    for i, tc in enumerate(tool_calls):
                        if not tc.get("id"):
                            tc["id"] = f"call_{i}"
                    current_tool_count = len(tool_calls)
                    current_search_count = sum(1 for tc in tool_calls if _get(_get(tc, "function", {}), "name", "") in SEARCH_TOOL_NAMES)
                    # 终端工具轮跳过总工具次数限制
                    if not _allow_terminal_tools and total_tool_calls + current_tool_count > max_total_tool_calls:
                        logger.warning(f"单轮总工具调用次数超过限制: {total_tool_calls + current_tool_count} > {max_total_tool_calls}")
                        messages.append({
                            "role": "system",
                            "content": _TOTAL_TOOL_LIMIT_TEXT
                        })
                        internal_control_injected = True
                        round_idx = max_rounds
                        _allow_terminal_tools = True
                        continue
                    assistant_msg: Dict[str, Any] = {"role": "assistant", "content": content or "", "tool_calls": tool_calls}
                    if reasoning_content:
                        assistant_msg["reasoning_content"] = reasoning_content
                    messages.append(assistant_msg)
                    tool_messages.append(assistant_msg)
                else:
                    content, tool_calls, message_dict = await self._complete_once(messages, type, current_tools, request_state)
                    if not tool_calls:
                        # 检测伪造任务编号并重试（拦截不发送）
                        # 仅在工具仍可用时拦截（非最后一轮），最后一轮模型无法调工具则跳过
                        if (_enable_intercept and not has_anima_call and not is_last_round):
                            raw_merged = _join_intermediate(content)
                            has_fake = _contains_fake_draw_reply(raw_merged)
                            if has_fake and _fake_retry_count == 0:
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
                        safe_content = sanitize_draw_reply_text(content, allow_task_ids=has_anima_call)
                        if on_text and safe_content:
                            await on_text(safe_content)
                        return _merge_intermediate(content), True, tool_messages, final_reasoning_content
                    if is_last_round and not _allow_terminal_tools:
                        safe_content = sanitize_draw_reply_text(content, allow_task_ids=has_anima_call)
                        if on_text and safe_content:
                            await on_text(safe_content)
                        if content or intermediate_texts:
                            return _merge_intermediate(content), True, tool_messages, final_reasoning_content
                        return "", False, tool_messages, final_reasoning_content
                    tool_calls_from_dict = message_dict.get("tool_calls") or []
                    for i, tc in enumerate(tool_calls_from_dict):
                        if isinstance(tc, dict) and not tc.get("id"):
                            tc["id"] = f"call_{i}"
                    current_tool_count = len(tool_calls)
                    current_search_count = sum(1 for tc in tool_calls if _get(_get(tc, "function", {}), "name", "") in SEARCH_TOOL_NAMES)
                    # 终端工具轮跳过总工具次数限制
                    if not _allow_terminal_tools and total_tool_calls + current_tool_count > max_total_tool_calls:
                        logger.warning(f"单轮总工具调用次数超过限制: {total_tool_calls + current_tool_count} > {max_total_tool_calls}")
                        messages.append({
                            "role": "system",
                            "content": _TOTAL_TOOL_LIMIT_TEXT
                        })
                        internal_control_injected = True
                        round_idx = max_rounds
                        _allow_terminal_tools = True
                        continue
                    messages.append(message_dict)
                    tool_messages.append(message_dict)

                # 收集中间轮次的文本
                safe_intermediate = sanitize_internal_control_text(content or "")
                if safe_intermediate and safe_intermediate.strip():
                    intermediate_texts.append(safe_intermediate.strip())

                # 检测是否调用了画图工具（含终端工具轮）
                if not is_last_round or _allow_terminal_tools:
                    anima_in_tool_calls = any(
                        _get(_get(tc, "function", {}), "name", "") == "generate_anima_image"
                        for tc in tool_calls
                    )
                    if anima_in_tool_calls:
                        has_anima_call = True

                # 检查工具调用限制
                # 终端工具轮：过滤掉非终端工具调用，只执行允许的终端工具
                if is_last_round and _allow_terminal_tools:
                    _filtered = [tc for tc in tool_calls if _get(_get(tc, "function", {}), "name", "") in TERMINAL_TOOLS]
                    if len(_filtered) < len(tool_calls):
                        logger.info(f"终端工具轮：过滤掉 {len(tool_calls) - len(_filtered)} 个非终端工具调用")
                    tool_calls = _filtered
                current_tool_count = len(tool_calls)
                current_search_count = sum(1 for tc in tool_calls if _get(_get(tc, "function", {}), "name", "") in SEARCH_TOOL_NAMES)
                
                # 检查总工具调用次数限制（终端工具轮跳过限制）
                if not _allow_terminal_tools and total_tool_calls + current_tool_count > max_total_tool_calls:
                    logger.warning(f"单轮总工具调用次数超过限制: {total_tool_calls + current_tool_count} > {max_total_tool_calls}")
                    if tool_messages and messages and messages[-1] is tool_messages[-1]:
                        messages.pop()
                        tool_messages.pop()
                    messages.append({
                        "role": "system",
                        "content": _TOTAL_TOOL_LIMIT_TEXT
                    })
                    internal_control_injected = True
                    round_idx = max_rounds
                    _allow_terminal_tools = True
                    continue
                
                # 检查联网搜索工具调用次数限制
                if search_tool_calls + current_search_count > MAX_SEARCH_TOOL_CALLS:
                    logger.warning(f"单轮联网搜索工具调用次数超过限制: {search_tool_calls + current_search_count} > {MAX_SEARCH_TOOL_CALLS}")
                    # 添加临时提示词，提醒大模型不要继续调用搜索工具
                    messages.append({
                        "role": "system",
                        "content": _SEARCH_TOOL_LIMIT_TEXT
                    })
                    internal_control_injected = True
                
                # 更新计数器
                total_tool_calls += current_tool_count
                search_tool_calls += current_search_count
                
                # 通知外部：工具调用阶段开始（受保护，不打断）
                self._set_tool_calling(request_chat_key, True)
                self._current_chat_key = request_chat_key
                self._current_trigger_userid = request_trigger_userid
                try:
                    await self._execute_tool_calls(messages, tool_calls, plugin_config)
                    # 收集tool消息
                    for msg in messages:
                        if msg.get("role") == "tool" and msg not in tool_messages:
                            tool_messages.append(msg)
                finally:
                    self._set_tool_calling(request_chat_key, False)
                    self._notify_tool_done(request_chat_key)

                # 工具上下文超预算时，允许终端工具（画图/记忆）作为最后一轮，然后停止
                if plugin_config and round_idx < max_rounds:
                    _tool_budget = getattr(plugin_config, 'TOOL_CONTEXT_TOKEN_BUDGET', 16384)
                    _tool_ctx_msgs = [m for m in messages if m.get("role") == "tool" or (m.get("role") == "assistant" and m.get("tool_calls"))]
                    if _tool_ctx_msgs and self.cal_token_count(_tool_ctx_msgs) > _tool_budget:
                        logger.warning(f"工具上下文超预算（{self.cal_token_count(_tool_ctx_msgs)} > {_tool_budget}），允许终端工具后停止")
                        round_idx = max_rounds
                        _allow_terminal_tools = True

                # 中间轮结束后输出分隔符，与最终轮分段
                if on_text and not is_last_round:
                    await on_text("\n\n")

                # 判断是否计入轮数：只有包含非 memory 工具时才增加轮数计数
                tool_names = {_get(_get(tc, "function", {}), "name", "") for tc in tool_calls}
                if tool_names - {"remember"}:
                    round_idx += 1
            except Exception as e:
                self._set_tool_calling(request_chat_key, False)
                self._notify_tool_done(request_chat_key)
                logger.warning(f"LLM 请求失败: {e!r}")
                self._rotate_key()
                return f"请求大模型时发生错误: {e!r}", False, tool_messages, ""
        return "", False, tool_messages, ""

    async def get_response(
        self,
        prompt,
        type: str = "chat",
        custom: Optional[Dict[str, Any]] = None,
        request_profile: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, bool]:
        custom = custom or {}
        chunks: List[str] = []

        async def collect(chunk: str):
            chunks.append(chunk)

        result = await self.stream_response(
            prompt,
            type=type,
            custom=custom,
            plugin_config=None,
            request_profile=request_profile,
            on_text=collect,
        )
        return result[0], result[1]

    def consume_tool_outputs(self, chat_key: Optional[str] = None) -> List[Dict[str, Any]]:
        if chat_key:
            if not hasattr(self, "_last_tool_outputs_by_chat"):
                self._last_tool_outputs_by_chat = {}
            return self._last_tool_outputs_by_chat.pop(chat_key, [])
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
            existing["trigger_userid"] = input_data.get("trigger_userid", existing.get("trigger_userid"))
            existing["sender"] = input_data.get("sender", existing.get("sender"))
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
