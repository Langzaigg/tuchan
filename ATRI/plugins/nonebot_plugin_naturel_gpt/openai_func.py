import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from tiktoken import Encoding, encoding_for_model

from .llm_tools import execute_tool, get_tool_schemas
from .logger import logger
from .singleton import Singleton

enc_cache: Dict[str, Encoding] = {}
os.environ["TOKENIZERS_PARALLELISM"] = "false"

ChunkCallback = Callable[[str], Awaitable[None]]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _message_to_dict(message: Any) -> Dict[str, Any]:
    if isinstance(message, dict):
        return dict(message)
    if hasattr(message, "model_dump"):
        return message.model_dump(exclude_none=True)
    if hasattr(message, "dict"):
        return message.dict(exclude_none=True)
    return {
        "role": _get(message, "role", "assistant"),
        "content": _get(message, "content", ""),
        "tool_calls": _get(message, "tool_calls", None),
    }


class TextGenerator(Singleton["TextGenerator"]):
    def init(self, api_keys: list, config: dict, proxy=None, base_url=""):
        self.api_keys = api_keys or [""]
        self.key_index = 0
        self.config = config
        self.proxy = proxy
        self.base_url = base_url
        self.last_tool_outputs: List[Dict[str, Any]] = []

    def _current_key(self) -> str:
        return self.api_keys[self.key_index % len(self.api_keys)]

    def _rotate_key(self) -> None:
        self.key_index = (self.key_index + 1) % len(self.api_keys)

    def _completion_kwargs(self, messages: List[Dict[str, Any]], type: str, stream: bool, tools: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        model_key = "model_mini" if type in {"summarize", "impression"} else "model"
        kwargs: Dict[str, Any] = {
            "model": self.config[model_key],
            "messages": messages,
            "temperature": self.config.get("temperature", 0.6),
            "max_tokens": self.config.get("max_summary_tokens" if type in {"summarize", "impression"} else "max_tokens", 1024),
            "timeout": self.config.get("timeout", 30),
            "stream": stream,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.proxy:
            kwargs["proxy"] = self.proxy
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
            response.raise_for_status()
            return response.json()

    async def _stream_iter_openai(self, kwargs: Dict[str, Any]):
        """流式调用 OpenAI-compatible API YIELD 每个 SSE chunk"""
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
        if tools:
            body["tools"] = tools
            if tool_choice:
                body["tool_choice"] = tool_choice

        client_kwargs: Dict[str, Any] = {
            "timeout": httpx.Timeout(timeout),
        }
        if proxy:
            client_kwargs["proxy"] = proxy

        async with httpx.AsyncClient(**client_kwargs) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
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
    ) -> Tuple[str, List[Dict[str, Any]]]:
        kwargs = self._completion_kwargs(messages, type, True, tools)
        content_parts: List[str] = []
        tool_call_chunks: Dict[int, Dict[str, Any]] = {}

        async for chunk in self._stream_iter_openai(kwargs):
            choices = _get(chunk, "choices", [])
            if not choices:
                continue
            delta = _get(choices[0], "delta", {})

            reasoning = _get(delta, "reasoning_content") or _get(delta, "reasoning") or ""
            if reasoning and on_reasoning:
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
                if _get(tool_call, "id"):
                    state["id"] = _get(tool_call, "id")
                function = _get(tool_call, "function", {}) or {}
                if _get(function, "name"):
                    state["function"]["name"] += str(_get(function, "name"))
                if _get(function, "arguments"):
                    state["function"]["arguments"] += str(_get(function, "arguments"))

        return "".join(content_parts), [v for _, v in sorted(tool_call_chunks.items()) if v["function"]["name"]]

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
            tool_call_id = _get(tool_call, "id", "") or f"call_{idx}"
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
    ) -> Tuple[str, bool]:
        messages = self._normalize_prompt(prompt, custom)
        self.last_tool_outputs = []
        tool_schemas = get_tool_schemas(plugin_config) if plugin_config and type == "chat" else []
        max_rounds = getattr(plugin_config, "LLM_MAX_TOOL_ROUNDS", 0) if plugin_config else 0

        for _ in range(max_rounds + 1):
            try:
                if self.config.get("enable_stream", True):
                    content, tool_calls = await self._stream_once(messages, type, tool_schemas, on_text, on_reasoning)
                    if not tool_calls:
                        return content.strip(), True
                    messages.append({"role": "assistant", "content": content or None, "tool_calls": tool_calls})
                else:
                    content, tool_calls, message_dict = await self._complete_once(messages, type, tool_schemas)
                    if not tool_calls:
                        if on_text and content:
                            await on_text(content)
                        return content.strip(), True
                    messages.append(message_dict)

                await self._execute_tool_calls(messages, tool_calls, plugin_config)
            except Exception as e:
                logger.warning(f"LLM 请求失败: {e!r}")
                self._rotate_key()
                if len(self.api_keys) <= 1:
                    return f"请求大模型时发生错误: {e!r}", False
        return "工具调用轮数过多，已停止本次回复。", False

    async def get_response(self, prompt, type: str = "chat", custom: dict = {}) -> Tuple[str, bool]:
        chunks: List[str] = []

        async def collect(chunk: str):
            chunks.append(chunk)

        return await self.stream_response(prompt, type=type, custom=custom, plugin_config=None, on_text=collect)

    def consume_tool_outputs(self) -> List[Dict[str, Any]]:
        outputs = self.last_tool_outputs
        self.last_tool_outputs = []
        return outputs

    @staticmethod
    def generate_msg_template(sender: str, msg: str, time_str: str = "") -> str:
        return f"{time_str}{sender}: {msg}"

    @staticmethod
    def cal_token_count(text: str, model: str = "gpt-3.5-turbo"):
        try:
            if model in enc_cache:
                enc = enc_cache[model]
            else:
                enc = encoding_for_model(model)
                enc_cache[model] = enc
            return len(enc.encode(text))
        except Exception:
            return max(1, len(text) // 2)
