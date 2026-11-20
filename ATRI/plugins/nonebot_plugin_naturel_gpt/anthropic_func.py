"""Anthropic `/v1/messages` 原生协议适配层。

插件内部（matcher / 工具循环 / 思考分流 / usage 统计）统一使用 OpenAI 兼容的消息与
chunk 结构，本模块只在**传输层**做双向翻译：

- `build_request(kwargs)`：OpenAI 风格请求参数 → `(url, headers, body)`
- `stream_to_openai_chunks(response)`：Anthropic SSE 事件流 → OpenAI 风格 chunk
- `message_to_completion(data)`：Anthropic message → OpenAI `chat.completion`

这样 `openai_func._stream_once` / `_complete_once` 的上层逻辑一行都不用改。

关于 thinking：Anthropic 的 extended thinking 要求工具轮把带 `signature` 的
thinking block 原样回传，而插件的内部消息结构（OpenAI 风格 `reasoning_content`）
拿不到 signature，强行启用会在工具轮被上游 400 拒绝。因此这里**不发送**
`thinking` 字段——`reasoning_effort` 档位在 Anthropic 协议下不生效（想强制不思考
用 `rg model <profile> default`，行为一致）。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

ANTHROPIC_VERSION = "2023-06-01"
# Anthropic 必填 max_tokens；profile 没配时用这个兜底。8192 对长回复偏小（会被 finish_reason=length 截断），
# 官方 2026 示例普遍写 16000，这里跟上。
DEFAULT_MAX_TOKENS = 16000
# max_tokens 是「正文 + 思考」的总上限（实测 max_tokens=300 + 困难任务 → thinking 300、正文为空、
# stop_reason=max_tokens），所以配了 max_tokens 时要按 ×8 留思考余量，再按模型上限收敛
# （实测 Sonnet 5.5 接受 128000）。
MAX_OUTPUT_TOKENS_MULTIPLIER = 8
MAX_OUTPUT_TOKENS_CAP = 128000
# 上一轮 thinking block（含 signature）在内部消息里的存放键，`split_messages` 会原样回传
THINKING_BLOCKS_KEY = "_anthropic_thinking_blocks"

# Anthropic 只接受这几种图片 media_type
_IMAGE_MEDIA_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
}

# 历史窗口可能从印象或人设开始，首个 user 之前也可能有轮次资料。
_TURN_SYSTEM_PREFIXES = (
    "[用户印象", "[impression]", "[人设资料]",
    "[调用结果]", "[搜索工具摘要]", "[作画记录]",
    "[当前触发]", "[记忆提醒]", "[新消息提醒]",
)
_MUTABLE_HEADER_PREFIXES = ("[压缩上下文摘要]", "[当前状态]")


def _flatten_text(content: Any) -> str:
    """把 OpenAI 风格的 content（字符串 / 文本块列表）压成纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def _image_block(url: str) -> Optional[Dict[str, Any]]:
    """OpenAI image_url → Anthropic image block（data URI 走 base64，其余走 url）。"""
    url = (url or "").strip()
    if not url:
        return None
    if url.startswith("data:"):
        try:
            header, data = url.split(",", 1)
        except ValueError:
            return None
        # data:image/png;base64,xxxx
        meta = header[5:]
        media_type = meta.split(";")[0].lower() or "image/png"
        if media_type not in _IMAGE_MEDIA_TYPES:
            media_type = "image/png"
        return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}
    return {"type": "image", "source": {"type": "url", "url": url}}


def _content_to_blocks(content: Any) -> List[Dict[str, Any]]:
    """OpenAI content → Anthropic content blocks。"""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: List[Dict[str, Any]] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                blocks.append({"type": "text", "text": item})
            elif not isinstance(item, dict):
                continue
            elif item.get("type") == "text":
                text = str(item.get("text") or "")
                if text:
                    blocks.append({"type": "text", "text": text})
            elif item.get("type") == "image_url":
                raw = item.get("image_url") or {}
                url = raw.get("url") if isinstance(raw, dict) else raw
                block = _image_block(str(url or ""))
                if block:
                    blocks.append(block)
    return blocks


def _append_blocks(out: List[Dict[str, Any]], role: str, blocks: List[Dict[str, Any]]) -> None:
    """按 Anthropic 的「user/assistant 交替」要求追加消息，连续同角色合并。

    合并是必须的：一轮里多个 tool 结果必须落在**同一条** user 消息的多个
    tool_result block 里，否则上游报 400。
    """
    if not blocks:
        return
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": list(blocks)})


def _convert_tool(tool: Any) -> Optional[Dict[str, Any]]:
    """OpenAI function tool → Anthropic tool。"""
    if not isinstance(tool, dict):
        return None
    fn = tool.get("function") if tool.get("type") == "function" else tool
    if not isinstance(fn, dict) or not fn.get("name"):
        return None
    return {
        "name": str(fn.get("name")),
        "description": str(fn.get("description") or ""),
        "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
    }


def _convert_tool_choice(tool_choice: Any) -> Optional[Dict[str, Any]]:
    if tool_choice is None:
        return None
    if isinstance(tool_choice, str):
        low = tool_choice.lower()
        if low == "auto":
            return {"type": "auto"}
        if low == "any":
            return {"type": "any"}
        if low == "none":
            # Anthropic 原生支持 none（旧实现是「干脆不传 tools」，模型会因此看不到工具描述）
            return {"type": "none"}
        return None
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        if fn.get("name"):
            return {"type": "tool", "name": str(fn["name"])}
    return None


def split_messages(messages: List[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
    """只将开头的系统指令提升到顶层，轮次资料留在对应的对话位置。"""
    system_texts: List[str] = []
    out: List[Dict[str, Any]] = []
    conversation_started = False
    pending_notes: List[Dict[str, Any]] = []

    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")

        if role == "system":
            text = _flatten_text(content)
            if not text:
                continue
            if not conversation_started and not text.lstrip().startswith(_TURN_SYSTEM_PREFIXES):
                system_texts.append(text)
            else:
                # CC 不接受 messages 内的 system 角色。用显式文本块保留原位置。
                conversation_started = True
                pending_notes.append({"type": "text", "text": f"[系统补充]\n{text}"})
            continue

        conversation_started = True
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": _flatten_text(content),
            }
            _append_blocks(out, "user", [block])
            continue

        # tool_result 必须排在合并后的 user 消息最前面，提示要等整批结果写完。
        if pending_notes:
            _append_blocks(out, "user", pending_notes)
            pending_notes.clear()

        if role == "assistant":
            blocks: List[Dict[str, Any]] = []
            # 上一轮的 thinking block（含 signature）必须原样回传，且要排在 content 最前面，
            # 否则开了 adaptive thinking 之后多轮/工具轮会被上游 400。
            saved_thinking = msg.get(THINKING_BLOCKS_KEY)
            if isinstance(saved_thinking, list):
                blocks.extend(block for block in saved_thinking if isinstance(block, dict))
            text = _flatten_text(content)
            if text:
                blocks.append({"type": "text", "text": text})
            for idx, call in enumerate(msg.get("tool_calls") or []):
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
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or f"toolu_{idx}"),
                    "name": str(fn.get("name") or ""),
                    "input": args,
                })
            # reasoning_content 不转成 thinking block：没有 signature，上游会拒
            _append_blocks(out, "assistant", blocks)
            continue

        blocks = _content_to_blocks(content)
        _append_blocks(out, "user", blocks)

    _append_blocks(out, "user", pending_notes)

    # 首条必须是 user：个别网关对 assistant 开头直接 400
    if out and out[0]["role"] == "assistant":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "继续"}]})
    # 空 content block 兜底
    for item in out:
        if not item["content"]:
            item["content"] = [{"type": "text", "text": " "}]
    return system_texts, out


def _build_url(base_url: str) -> str:
    base = (base_url or "https://api.anthropic.com").rstrip("/")
    if base.endswith("/v1"):
        return base + "/messages"
    return base + "/v1/messages"


# ---- prompt caching（11-17 实测 CC：**顶层 cache_control 无效，必须逐块打点**）----
CACHE_CONTROL = {"type": "ephemeral"}


def _system_blocks(system_texts: List[str]) -> List[Dict[str, Any]]:
    """缓存整个头部，并在摘要或状态变动时保留稳定的头部前缀。"""
    blocks = [{"type": "text", "text": text} for text in system_texts if text]
    if not blocks:
        return blocks

    blocks[-1]["cache_control"] = dict(CACHE_CONTROL)
    for idx, block in enumerate(blocks):
        if block["text"].lstrip().startswith(_MUTABLE_HEADER_PREFIXES):
            if idx:
                blocks[idx - 1]["cache_control"] = dict(CACHE_CONTROL)
            break
    return blocks


def _apply_cache_breakpoints(conv: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """给「上一轮历史」的末尾补第二个断点：群聊每轮只追加新消息，前缀天然稳定。

    断点会随轮次前移，但 Anthropic 是前缀匹配——旧断点包含在新前缀里，仍然命中。
    只有一条消息（单轮）时不打历史断点，system 那个已经够用。
    """
    if len(conv) >= 2:
        for block in reversed(conv[-2]["content"]):
            if block.get("type") in {"text", "image", "tool_use", "tool_result"}:
                block["cache_control"] = dict(CACHE_CONTROL)
                break
    return conv


def build_request(kwargs: Dict[str, Any]) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
    """OpenAI 风格请求参数 → (url, headers, body)。"""
    api_key = str(kwargs.get("api_key") or "")
    base_url = str(kwargs.get("base_url") or "https://api.anthropic.com")
    system_texts, conv = split_messages(kwargs.get("messages") or [])

    # Anthropic 的 max_tokens 把 thinking 也算进去（和 Gemini 的 maxOutputTokens 一样），
    # 按配置值 ×8 再收敛到模型上限，避免思考吃光额度导致正文为空。
    raw_max_tokens = kwargs.get("max_tokens")
    if raw_max_tokens:
        max_tokens = min(
            int(raw_max_tokens) * MAX_OUTPUT_TOKENS_MULTIPLIER, MAX_OUTPUT_TOKENS_CAP
        )
    else:
        max_tokens = DEFAULT_MAX_TOKENS

    body: Dict[str, Any] = {
        "model": kwargs.get("model"),
        "max_tokens": max_tokens,
        "messages": _apply_cache_breakpoints(conv),
    }
    if system_texts:
        # 逐块给断点；顶层 cache_control 在部分网关（实测 CC）会被直接忽略
        body["system"] = _system_blocks(system_texts)
    if kwargs.get("stream"):
        body["stream"] = True

    # 思考：新版 Claude（Fable 5 / Opus 4.8 / 4.7 / Sonnet 4.6+）只认 adaptive，
    # budget_tokens 已移除（传了直接 400）。profile 配 `anthropic_thinking: adaptive` 才发。
    thinking = kwargs.get("anthropic_thinking")
    if thinking:
        if isinstance(thinking, str) and thinking.strip().lower() in ("adaptive", "true", "on", "1"):
            body["thinking"] = {"type": "adaptive"}
        elif isinstance(thinking, dict):
            body["thinking"] = thinking

    for key in ("temperature", "top_p"):
        if kwargs.get(key) is not None:
            body[key] = kwargs[key]

    tools = [_convert_tool(t) for t in (kwargs.get("tools") or [])]
    tools = [t for t in tools if t]
    tool_choice = kwargs.get("tool_choice")
    if tools:
        body["tools"] = tools
        choice = _convert_tool_choice(tool_choice)
        if choice:
            body["tool_choice"] = choice

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    return _build_url(base_url), headers, body


def _usage_to_openai(usage: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Anthropic usage → OpenAI usage。

    Anthropic 的 `input_tokens` **只算未命中的新增输入**，缓存命中/写入分开报，
    所以 prompt_tokens 要把三部分加起来；否则开了缓存之后统计会凭空变小。
    """
    if not usage:
        return None
    fresh = int(usage.get("input_tokens") or 0)
    completion = int(usage.get("output_tokens") or 0)
    cached = int(usage.get("cache_read_input_tokens") or 0)
    created = int(usage.get("cache_creation_input_tokens") or 0)
    thinking = int((usage.get("output_tokens_details") or {}).get("thinking_tokens") or 0)
    prompt = fresh + cached + created
    converted: Dict[str, Any] = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {
            "cached_tokens": cached,
            "cache_creation_tokens": created,
        },
    }
    if thinking:
        # output_tokens 本身已包含 thinking，这里只是把明细标出来
        converted["completion_tokens_details"] = {"reasoning_tokens": thinking}
    return converted


def _finish_reason(stop_reason: Optional[str]) -> str:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(str(stop_reason or ""), "stop")


def _extract_error(data: Dict[str, Any]) -> Optional[str]:
    err = data.get("error")
    if isinstance(err, dict):
        return str(err.get("message") or err.get("type") or err)
    if isinstance(err, str):
        return err
    return None


async def stream_to_openai_chunks(response, sink: Optional[Dict[str, Any]] = None):
    """Anthropic SSE → OpenAI 风格 chunk（async generator）。

    产出结构与 `/chat/completions` 的 stream chunk 一致：
    `{"choices":[{"delta":{...}}]}` 与 `{"usage":{...}}`。

    `sink` 用来把本轮完整 thinking block（thinking + signature）带出异步生成器，
    供上层的 assistant 消息保存、下一轮原样回传——signature 丢了会被上游 400。
    """
    tool_index: Dict[int, int] = {}   # content_block index → tool_calls index
    next_tool_idx = 0
    usage_acc: Dict[str, Any] = {}
    thinking_state: Dict[int, Dict[str, str]] = {}
    thinking_blocks: List[Dict[str, Any]] = []

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

        etype = data.get("type")

        if etype == "error":
            raise RuntimeError(f"Anthropic stream error: {_extract_error(data) or data}")

        if etype == "message_start":
            msg = data.get("message") or {}
            usage_acc = dict(msg.get("usage") or {})
            u = _usage_to_openai(usage_acc)
            if u:
                yield {"usage": u}
            continue

        if etype == "content_block_start":
            block = data.get("content_block") or {}
            idx = int(data.get("index") or 0)
            btype = block.get("type")
            if btype == "thinking":
                thinking_state[idx] = {
                    "thinking": str(block.get("thinking") or ""),
                    "signature": str(block.get("signature") or ""),
                }
            elif btype == "tool_use":
                tool_index[idx] = next_tool_idx
                yield {"choices": [{"delta": {"tool_calls": [{
                    "index": next_tool_idx,
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name") or ""), "arguments": ""},
                }]}}]}
                next_tool_idx += 1
            continue

        if etype == "content_block_delta":
            delta = data.get("delta") or {}
            dtype = delta.get("type")
            idx = int(data.get("index") or 0)
            if dtype == "text_delta":
                text = delta.get("text") or ""
                if text:
                    yield {"choices": [{"delta": {"content": text}}]}
            elif dtype == "thinking_delta":
                think = delta.get("thinking") or ""
                if idx in thinking_state:
                    thinking_state[idx]["thinking"] += think
                if think:
                    yield {"choices": [{"delta": {"reasoning_content": think}}]}
            elif dtype == "signature_delta":
                sig = delta.get("signature") or ""
                if idx in thinking_state:
                    thinking_state[idx]["signature"] += sig
            elif dtype == "input_json_delta":
                ti = tool_index.get(idx)
                partial = delta.get("partial_json") or ""
                if ti is not None and partial:
                    yield {"choices": [{"delta": {"tool_calls": [{
                        "index": ti,
                        "function": {"arguments": partial},
                    }]}}]}
            continue

        if etype == "content_block_stop":
            idx = int(data.get("index") or 0)
            state = thinking_state.pop(idx, None)
            if state and state.get("thinking") and state.get("signature"):
                thinking_blocks.append({
                    "type": "thinking",
                    "thinking": state["thinking"],
                    "signature": state["signature"],
                })
            continue

        if etype == "message_delta":
            usage_acc.update(data.get("usage") or {})
            u = _usage_to_openai(usage_acc)
            if u:
                yield {"usage": u}
            stop_reason = (data.get("delta") or {}).get("stop_reason")
            if stop_reason:
                yield {"choices": [{"delta": {}, "finish_reason": _finish_reason(stop_reason)}]}
            continue

        if etype == "message_stop":
            break
        # ping / content_block_stop 等：忽略

    if thinking_blocks and sink is not None:
        sink[THINKING_BLOCKS_KEY] = thinking_blocks


def message_to_completion(data: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic message → OpenAI chat.completion。"""
    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    thinking_blocks: List[Dict[str, Any]] = []
    for idx, block in enumerate(data.get("content") or []):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text") or ""))
        elif btype == "thinking":
            reasoning_parts.append(str(block.get("thinking") or ""))
            # 原样留存（thinking + signature），下一轮必须带回去
            if block.get("signature"):
                thinking_blocks.append(block)
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or f"toolu_{idx}"),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if thinking_blocks:
        message[THINKING_BLOCKS_KEY] = thinking_blocks

    return {
        "id": str(data.get("id") or ""),
        "object": "chat.completion",
        "model": str(data.get("model") or ""),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _finish_reason(data.get("stop_reason")),
        }],
        "usage": _usage_to_openai(data.get("usage")) or {},
    }
