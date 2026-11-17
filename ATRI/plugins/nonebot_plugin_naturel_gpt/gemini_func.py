"""Gemini 原生协议（`/v1beta/models/{model}:generateContent`）适配层。

为什么需要这一层：部分聚合网关（实测 TeamoRouter）在 OpenAI 兼容端点
（`/v1/chat/completions`）上会把 Gemini 的 thought summary **当成普通 content 增量**
下发——既没有 `<think>` 标签，也没有 `reasoning_content` 字段，上层无法区分，思考就跟着
正文发进群。同一个模型走 Gemini 原生端点则完全正常：思考是带 `thought: true` 标记的
**独立 part**，正文 part 干净，响应里另有 `thoughtSignature`。所以让 Gemini 系 profile 走
原生协议是唯一不依赖文本启发式的解法——思考仍然保留（转成 `reasoning_content`），
只是绝不会混进正文。

插件内部（matcher / 工具循环 / 思考分流 / usage 统计）统一使用 OpenAI 兼容的消息与
chunk 结构，本模块只在**传输层**做双向翻译（与 `anthropic_func.py` 同构）：

- `build_request(kwargs)`：OpenAI 风格请求参数 → `(url, headers, body)`
- `stream_to_openai_chunks(response, sig_sink)`：Gemini SSE → OpenAI 风格 chunk
- `message_to_completion(data)`：Gemini candidates → OpenAI `chat.completion`

关于 `thoughtSignature`：Gemini 3/4 要求多轮把签名回传才能保持上下文。插件内部消息
结构里没有对应字段，这里用内部键 `_gemini_thought_signature` 挂在 assistant 消息上，
`split_messages` 再把它塞回该轮的最后一个 part。实测不回传不会 400，但会丢上下文质量。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com"
THOUGHT_SIGNATURE_KEY = "_gemini_thought_signature"

# Gemini 的 OpenAPI schema 子集不接受这些 JSON Schema 关键字
_SCHEMA_DROP_KEYS = {
    "$schema", "$id", "$defs", "$ref", "definitions",
    "additionalProperties", "patternProperties", "unevaluatedProperties",
    "propertyNames", "strict", "examples", "default",
}


def _flatten_text(content: Any) -> str:
    """OpenAI 风格 content（字符串 / 文本块列表）→ 纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _image_part(url: str) -> Optional[Dict[str, Any]]:
    """OpenAI image_url → Gemini inlineData。

    Gemini 原生 API 不接受任意 http 图片地址（`fileData` 只认官方 Files API），
    所以只处理 data URI；http 地址属于上层没走 base64 的兜底，这里直接丢弃，
    避免整条请求被 400。
    """
    url = (url or "").strip()
    if not url.startswith("data:"):
        return None
    try:
        header, data = url.split(",", 1)
    except ValueError:
        return None
    meta = header[5:]
    mime = (meta.split(";")[0] or "image/png").lower()
    if not mime.startswith("image/"):
        mime = "image/png"
    if not data:
        return None
    return {"inlineData": {"mimeType": mime, "data": data}}


def _content_to_parts(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"text": content}] if content else []
    parts: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                if item:
                    parts.append({"text": item})
            elif not isinstance(item, dict):
                continue
            elif item.get("type") == "text":
                text = str(item.get("text") or "")
                if text:
                    parts.append({"text": text})
            elif item.get("type") == "image_url":
                raw = item.get("image_url") or {}
                url = raw.get("url") if isinstance(raw, dict) else raw
                part = _image_part(str(url or ""))
                if part:
                    parts.append(part)
    return parts


def _append_content(out: List[Dict[str, Any]], role: str, parts: List[Dict[str, Any]]) -> None:
    """追加一轮内容，连续同角色合并 parts（Gemini 允许连续同角色，但合并更稳）。"""
    if not parts:
        return
    if out and out[-1]["role"] == role:
        out[-1]["parts"].extend(parts)
    else:
        out.append({"role": role, "parts": list(parts)})


def _clean_schema(node: Any) -> Any:
    """递归清洗 JSON Schema：删掉 Gemini 不认的关键字、把联合类型收敛成单类型。"""
    if isinstance(node, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in node.items():
            if key in _SCHEMA_DROP_KEYS:
                continue
            if key == "type" and isinstance(value, list):
                types = [str(v) for v in value if isinstance(v, str) and v != "null"]
                cleaned["type"] = types[0] if types else "string"
                continue
            cleaned[key] = _clean_schema(value)
        return cleaned
    if isinstance(node, list):
        return [_clean_schema(item) for item in node]
    return node


def _convert_tools(tools: Any) -> Optional[List[Dict[str, Any]]]:
    declarations: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        decl: Dict[str, Any] = {"name": str(fn["name"])}
        if fn.get("description"):
            decl["description"] = str(fn["description"])
        params = fn.get("parameters")
        decl["parameters"] = (
            _clean_schema(params) if isinstance(params, dict) else {"type": "object", "properties": {}}
        )
        declarations.append(decl)
    return [{"functionDeclarations": declarations}] if declarations else None


def _convert_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
    """OpenAI tool_choice → Gemini toolConfig（None 表示交给模型自动决定）。"""
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        low = tool_choice.lower()
        mode = {"auto": "AUTO", "none": "NONE", "any": "ANY", "required": "ANY"}.get(low)
        return {"functionCallingConfig": {"mode": mode}} if mode else None
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if fn.get("name"):
            return {
                "functionCallingConfig": {
                    "mode": "ANY",
                    "allowedFunctionNames": [str(fn["name"])],
                }
            }
    return None


def split_messages(
    messages: List[Dict[str, Any]],
) -> Tuple[List[str], List[Dict[str, Any]]]:
    """OpenAI messages → (system 文本列表, Gemini contents)。"""
    system_texts: List[str] = []
    contents: List[Dict[str, Any]] = []

    # tool 消息只带 tool_call_id，Gemini 的 functionResponse 需要函数名
    name_by_call_id: Dict[str, str] = {}
    for msg in messages or []:
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            for call in msg.get("tool_calls") or []:
                if isinstance(call, dict) and call.get("id"):
                    fn = call.get("function") or {}
                    name_by_call_id[str(call["id"])] = str(fn.get("name") or "")

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if role == "system":
            text = _flatten_text(content)
            if text:
                system_texts.append(text)
            continue

        if role == "tool":
            name = name_by_call_id.get(str(msg.get("tool_call_id") or ""), "") or "tool"
            result = _flatten_text(content)
            _append_content(
                contents,
                "user",
                [{"functionResponse": {"name": name, "response": {"result": result}}}],
            )
            continue

        if role == "assistant":
            parts: List[Dict[str, Any]] = []
            text = _flatten_text(content)
            if text:
                parts.append({"text": text})
            for call in msg.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                raw_args = fn.get("arguments") or "{}"
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    args = {}
                if not isinstance(args, dict):
                    args = {}
                parts.append({"functionCall": {"name": str(fn.get("name") or ""), "args": args}})
            # 回复轮的 thoughtSignature：Gemini 约定附在该轮最后一个 part 上
            signature = msg.get(THOUGHT_SIGNATURE_KEY)
            if parts and signature:
                parts[-1]["thoughtSignature"] = str(signature)
            _append_content(contents, "model", parts)
            continue

        _append_content(contents, "user", _content_to_parts(content))

    if contents and contents[0]["role"] == "model":
        contents.insert(0, {"role": "user", "parts": [{"text": "继续"}]})
    for item in contents:
        if not item["parts"]:
            item["parts"] = [{"text": " "}]
    return system_texts, contents


def _build_url(base_url: str, model: str, stream: bool) -> str:
    base = (base_url or DEFAULT_BASE_URL).rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    if not base.endswith("/v1beta"):
        base += "/v1beta"
    name = (model or "").strip()
    if name.startswith("models/"):
        name = name[len("models/"):]
    action = "streamGenerateContent?alt=sse" if stream else "generateContent"
    return f"{base}/models/{name}:{action}"


def _thinking_config(reasoning_effort: Any) -> Dict[str, Any]:
    """`includeThoughts: true`：思考以独立 `thought: true` part 下发，上层转成
    `reasoning_content`（`LLM_SHOW_REASONING` 决定要不要展示、`keep_reasoning` 决定
    要不要带进下一轮），正文只取非 thought part，两者永不混在一起。"""
    config: Dict[str, Any] = {"includeThoughts": True}
    level = {"low": "low", "medium": "medium", "high": "high"}.get(
        str(reasoning_effort or "").strip().lower()
    )
    if level:
        config["thinkingLevel"] = level
    return config


def build_request(kwargs: Dict[str, Any]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """OpenAI 风格请求参数 → (url, headers, body)。"""
    api_key = str(kwargs.get("api_key") or "")
    base_url = str(kwargs.get("base_url") or DEFAULT_BASE_URL)
    model = str(kwargs.get("model") or "")
    stream = bool(kwargs.get("stream"))
    system_texts, contents = split_messages(kwargs.get("messages") or [])

    body: Dict[str, Any] = {"contents": contents}

    generation: Dict[str, Any] = {}
    if kwargs.get("temperature") is not None:
        generation["temperature"] = kwargs["temperature"]
    if kwargs.get("top_p") is not None:
        generation["topP"] = kwargs["top_p"]
    if kwargs.get("max_tokens"):
        # 注意：Gemini 的 maxOutputTokens 把 thinking token 也算进去，思考量可达正文数倍，
        # 按配置值 ×8 发送，避免思考吃光额度导致 content 为空。
        generation["maxOutputTokens"] = int(kwargs["max_tokens"]) * 8
    generation["thinkingConfig"] = _thinking_config(kwargs.get("reasoning_effort"))
    body["generationConfig"] = generation

    if system_texts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_texts)}]}

    tools = _convert_tools(kwargs.get("tools"))
    if tools:
        body["tools"] = tools
        tool_config = _convert_tool_choice(kwargs.get("tool_choice"))
        if tool_config:
            body["toolConfig"] = tool_config

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    return _build_url(base_url, model, stream), headers, body


def _usage_to_openai(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not usage:
        return None
    prompt = int(usage.get("promptTokenCount") or 0)
    candidates = int(usage.get("candidatesTokenCount") or 0)
    thoughts = int(usage.get("thoughtsTokenCount") or 0)
    # Gemini 的计费口径：思考 token 也算输出
    completion = candidates + thoughts
    cached = int(usage.get("cachedContentTokenCount") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def _finish_reason(reason: Optional[str]) -> str:
    return {
        "STOP": "stop",
        "MAX_TOKENS": "length",
        "SAFETY": "content_filter",
        "RECITATION": "content_filter",
        "PROHIBITED_CONTENT": "content_filter",
        "BLOCKLIST": "content_filter",
        "SPII": "content_filter",
    }.get(str(reason or ""), "stop")


def _extract_signature(parts: List[Dict[str, Any]]) -> str:
    """签名约定：无 functionCall 时在最后一个 part，有并发调用时在第一个调用 part。"""
    for part in parts:
        if part.get("functionCall") and part.get("thoughtSignature"):
            return str(part["thoughtSignature"])
    for part in reversed(parts):
        if part.get("thoughtSignature"):
            return str(part["thoughtSignature"])
    return ""


def _collect_response_parts(
    parts: List[Dict[str, Any]],
) -> Tuple[str, List[str], List[Dict[str, Any]]]:
    """→ (正文, 思考文本, tool_calls)"""
    text_parts: List[str] = []
    thought_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for idx, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if part.get("thought"):
            if part.get("text"):
                thought_parts.append(str(part["text"]))
            continue
        if part.get("text"):
            text_parts.append(str(part["text"]))
        call = part.get("functionCall")
        if isinstance(call, dict) and call.get("name"):
            tool_calls.append({
                "id": f"call_{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {
                    "name": str(call["name"]),
                    "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                },
            })
    return "".join(text_parts), "".join(thought_parts), tool_calls


async def stream_to_openai_chunks(response, sig_sink: Optional[Dict[str, Any]] = None):
    """Gemini `:streamGenerateContent?alt=sse` → OpenAI 风格 chunk（async generator）。"""
    tool_index = 0
    async for raw in response.aiter_lines():
        line = (raw or "").strip()
        if not line or line.startswith("event:"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        if data.get("error"):
            err = data["error"]
            message = err.get("message") if isinstance(err, dict) else str(err)
            raise RuntimeError(f"Gemini stream error: {message}")

        for candidate in data.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            content = candidate.get("content") or {}
            parts = content.get("parts") or []
            signature = _extract_signature([p for p in parts if isinstance(p, dict)])
            if signature and sig_sink is not None:
                sig_sink[THOUGHT_SIGNATURE_KEY] = signature
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if part.get("thought"):
                    thought = str(part.get("text") or "")
                    if thought:
                        yield {"choices": [{"delta": {"reasoning_content": thought}}]}
                    continue
                text = str(part.get("text") or "")
                if text:
                    yield {"choices": [{"delta": {"content": text}}]}
                call = part.get("functionCall")
                if isinstance(call, dict) and call.get("name"):
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": tool_index,
                        "id": f"call_{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {
                            "name": str(call["name"]),
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        },
                    }]}}]}
                    tool_index += 1
            finish = candidate.get("finishReason")
            if finish:
                yield {"choices": [{"delta": {}, "finish_reason": _finish_reason(finish)}]}

        usage = data.get("usageMetadata")
        if usage:
            converted = _usage_to_openai(usage)
            if converted:
                yield {"usage": converted}


def message_to_completion(data: Dict[str, Any]) -> Dict[str, Any]:
    """Gemini candidates → OpenAI chat.completion。"""
    candidates = data.get("candidates") or []
    if not candidates:
        feedback = data.get("promptFeedback") or {}
        reason = str(feedback.get("blockReason") or "")
        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "" if reason else str(data.get("error") or ""),
        }
        return {
            "id": str(data.get("responseId") or ""),
            "object": "chat.completion",
            "model": str(data.get("modelVersion") or ""),
            "choices": [{
                "index": 0,
                "message": message,
                "finish_reason": "content_filter" if reason else "stop",
            }],
            "usage": _usage_to_openai(data.get("usageMetadata")) or {},
        }

    candidate = candidates[0] if isinstance(candidates[0], dict) else {}
    content = candidate.get("content") or {}
    parts = [p for p in (content.get("parts") or []) if isinstance(p, dict)]
    text, thought, tool_calls = _collect_response_parts(parts)
    signature = _extract_signature(parts)

    message = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if thought:
        message["reasoning_content"] = thought
    if signature:
        message[THOUGHT_SIGNATURE_KEY] = signature

    finish = _finish_reason(candidate.get("finishReason"))
    if tool_calls:
        finish = "tool_calls"

    return {
        "id": str(data.get("responseId") or ""),
        "object": "chat.completion",
        "model": str(data.get("modelVersion") or ""),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": _usage_to_openai(data.get("usageMetadata")) or {},
    }
