import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections import OrderedDict, deque
from contextvars import ContextVar
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from tiktoken import Encoding, encoding_for_model

from .llm_tools import execute_tool, get_tool_schemas
from .logger import logger
from .singleton import Singleton

# 工具调用限制常量（从配置读取，这些是默认值）
MAX_TOTAL_TOOL_CALLS = 15  # 单轮总工具调用次数限制，实际值从 LLM_MAX_TOTAL_TOOL_CALLS 配置读取
MAX_SEARCH_TOOL_CALLS = 3  # 单轮搜索工具调用次数限制
TERMINAL_TOOLS = {"generate_anima_image", "remember"}  # 终端工具：超限后仍允许最后一次调用
SEARCH_TOOL_NAMES = {"bocha_search", "tavily_search"}  # 搜索工具名称
# 仅在画图场景下暴露的工具：画图工具本体 + 为它确定作画标签的 danbooru_search
_DRAW_ONLY_TOOLS = {"generate_anima_image", "danbooru_search"}

_CURRENT_CHAT_KEY: ContextVar[str] = ContextVar("naturel_gpt_current_chat_key", default="")
_CURRENT_TRIGGER_USERID: ContextVar[str] = ContextVar("naturel_gpt_current_trigger_userid", default="")
# 视觉工具专用快照：本次请求可见图片表 {显示编号: 原始URL}，由 chat_prompt 的图片策略生成，
# 覆盖历史 / context_only / 触发消息（供 vision、anime_trace 把 [图片N] 映射回真实 URL）
# 用 default=None + getter 兜底，避免 list/dict 可变默认值跨上下文共享
_VISIBLE_IMAGES: ContextVar[Optional[Dict[int, str]]] = ContextVar("naturel_gpt_visible_images", default=None)
# 视觉工具专用快照：视觉模型配置 {model, base_url, api_key, max_tokens, proxy, use_socket_proxy, timeout}
_CURRENT_VISION_CONFIG: ContextVar[Optional[Dict[str, Any]]] = ContextVar("naturel_gpt_current_vision_config", default=None)

# 进程级兜底会话 ID：chat_key 不可用时（如启动期健康检查）保持本进程内稳定
_OPENCODE_FALLBACK_SESSION = uuid.uuid4().hex

# ---- 多 API Key 轮换策略 ----
# 铁律：恒定从**第一顺位 key** 开始用。只有当失败明确归因于该 key 自身（鉴权失败 / 额度耗尽 /
# 限流）时才顺延到下一个 key，并把失败的 key 打进冷却；冷却到期自动回到第一顺位。
# 参数错误（400）、超时、上下文超限、图片被拒等与 key 无关，换 key 也救不回来——以前对任何
# 异常都推进索引且成功不回退，导致一次偶发失败就把该 profile 的流量永久钉在第二个 key 上。
KEY_FAILURE_COOLDOWN_SECONDS = 300  # 单个 key 失败后的冷却时长（秒）
_KEY_ERROR_STATUS_CODES = ("http 401", "http 402", "http 403", "http 429")
_KEY_ERROR_MARKERS = (
    "invalid api key", "invalid_api_key", "incorrect api key", "api key is invalid",
    "unauthorized", "authentication error", "authentication_error",
    "no permission", "permission denied", "insufficient_quota", "insufficient balance",
    "exceeded your current quota", "you exceeded your quota", "quota exceeded",
    "rate limit", "rate_limit", "too many requests", "billing", "out of credit",
    "账号余额", "余额不足", "欠费", "配额", "限流",
)


def _is_key_level_error(err_text: Optional[str]) -> bool:
    """错误是否归因于当前 API key（鉴权 / 额度 / 限流）——只有这类错误才值得换 key。"""
    if not err_text:
        return False
    text = str(err_text).lower()
    if any(code in text for code in _KEY_ERROR_STATUS_CODES):
        return True
    return any(marker in text for marker in _KEY_ERROR_MARKERS)


def _build_provider_headers(base_url: str) -> Dict[str, str]:
    """Console Go（opencode.ai）要求客户端每个对话带稳定 x-opencode-session 头，
    并用自定义 User-Agent 标识客户端（https://opencode.ai/docs/go/）。
    会话 ID 由 chat_key 确定性派生（md5），同一群聊跨重启稳定，利于服务端路由与提示缓存。
    其他 provider 返回空 dict，不加任何额外头。
    """
    headers: Dict[str, str] = {}
    try:
        host = urlsplit(base_url).netloc.lower()
    except Exception:
        return headers
    if "opencode.ai" not in host:
        return headers
    headers["User-Agent"] = "ATRI-NaturelGPT/1.0 (QQ chatbot)"
    chat_key = _CURRENT_CHAT_KEY.get()
    if chat_key:
        session_id = hashlib.md5(chat_key.encode("utf-8")).hexdigest()[:24]
    else:
        session_id = _OPENCODE_FALLBACK_SESSION
    headers["x-opencode-session"] = f"atri-{session_id}"
    return headers


_TOTAL_TOOL_LIMIT_TEXT = f"工具调用次数已达上限（{MAX_TOTAL_TOOL_CALLS}次）。停止继续调用工具，基于已有工具结果直接回答当前用户。"
_SEARCH_TOOL_LIMIT_TEXT = f"搜索工具调用次数已达上限（{MAX_SEARCH_TOOL_CALLS}次），请基于已有搜索结果回复，不要再调用搜索工具。"
_TOOL_LOOP_TIMEOUT_TEXT = "工具调用总耗时超限。停止继续调用工具，基于已有工具结果直接回答当前用户。"
# force 模式强制画图提示：主流 provider 的思考模式均不兼容 tool_choice 强制指定（400），
# force 模式改为在消息尾部追加该提示（尾部追加不破坏历史前缀缓存；仅注入一次，
# 模型仍不调用则由 matcher 的伪造编号拦截/漫画强制画图兜底）
_FORCE_DRAW_HINT_TEXT = (
    "当前用户明确要求作画。你必须通过 tool_calls 调用 generate_anima_image 工具完成作画，"
    "禁止只在文字里说「画了」「在画了」「等图吧」而不实际调用工具；任务编号只能由工具返回，禁止编造。"
)
# 轮数上限的最后一轮：除 tool_choice="none" 外再明说一句。DeepSeek 系模型在 tool_choice="none"
# 下仍会"想"调工具，服务端不再解析，原生 DSML 标记就原样落进 content（群里出现一串
# <｜DSML｜calls>…）；明确告知"不能再调了、直接说结果"能显著降低这种泄漏。
_ROUND_LIMIT_TEXT = (
    "工具调用轮数已达上限，本轮不能再调用任何工具。请基于已有工具结果直接用文字回复当前用户；"
    "若还有没查到的内容，直接说明查到哪一步、缺什么即可，不要再输出任何工具调用标记。"
)
# 正文里泄漏工具调用标记后的重试提示（本轮禁止调工具 / 允许调工具 两种情况）
_MARKUP_LEAK_NO_TOOLS_TEXT = (
    "你刚才把工具调用标记直接写进了回复正文，这是无效的，也不会被执行。"
    "本轮不能调用工具，请只用自然语言回复用户，说明已查到的结果和还缺的信息。"
)
_MARKUP_LEAK_WITH_TOOLS_TEXT = (
    "你刚才把工具调用写进了回复正文而不是通过 tool_calls 接口，这样不会被执行。"
    "需要调用工具就使用 tool_calls；否则请只用自然语言回复。"
)
_INTERNAL_CONTROL_PATTERNS = (
    re.compile(r"单轮?工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有结果回复[。.]?"),
    re.compile(r"工具调用次数已达上限（?\d+次）?[。，,]?\s*停止继续调用工具，基于已有工具结果直接回答当前用户[。.]?"),
    re.compile(r"博查搜索工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有搜索结果回复，不要再调用搜索工具[。.]?"),
    re.compile(r"搜索工具调用次数已达上限（?\d+次）?[。，,]?\s*请基于已有搜索结果回复，不要再调用搜索工具[。.]?"),
    # assistant 空 content 的 API 占位符（见 _completion_kwargs）：模型看到自己"说过"它就会照抄，
    # 一旦混进正文会随中间轮文本合并进回复与历史，之后越滚越多
    re.compile(r"\[无内容\]"),
)
_MODEL_REQUEST_ERROR_PREFIX = "请求大模型时发生错误:"

# 工具调用标记泄漏检测：模型把工具调用写进了 content 而非 tool_calls。
# 1) Anthropic/通用 XML：<function_calls>…</function_calls>、<tool_call>…</tool_call>
# 2) DeepSeek 原生 DSML：<｜DSML｜calls> <｜DSML｜invoke name="x"> <｜DSML｜parameter …>…</｜DSML｜calls>
#    竖线为全角 U+FF5C，实际泄漏样例里会重复并带空格（<｜｜DSML｜｜ calls>），正则一律宽松匹配
# 3) DeepSeek 旧版：<｜tool▁calls▁begin｜>…<｜tool▁calls▁end｜>（▁ 为 U+2581）
_TOOL_CALL_XML_RE = re.compile(
    r'<(?:function_calls|tool_call)[\s>].*?</(?:function_calls|tool_call)>',
    re.DOTALL,
)
_DSML_TAG_RE = re.compile(r'</?\s*[｜|]+\s*DSML\s*[｜|]+[^>]*>')
_DSML_BLOCK_RE = re.compile(
    r'<\s*[｜|]+\s*DSML\s*[｜|]+\s*(calls|invoke)\b[^>]*>.*?'
    r'(?:</\s*[｜|]+\s*DSML\s*[｜|]+\s*\1\s*>|\Z)',
    re.DOTALL,
)
_DS_LEGACY_TAG_RE = re.compile(r'<[｜|]\s*tool[▁_ ][^｜|>]*[｜|]>')
_DS_LEGACY_BLOCK_RE = re.compile(
    r'<[｜|]\s*tool[▁_ ]calls[▁_ ]begin\s*[｜|]>.*?(?:<[｜|]\s*tool[▁_ ]calls[▁_ ]end\s*[｜|]>|\Z)',
    re.DOTALL,
)


def contains_tool_call_xml(content: str) -> bool:
    """检测文本中是否包含工具调用标记（XML / DeepSeek DSML / DeepSeek 旧版 tool 标记）。"""
    if not content:
        return False
    return bool(
        _TOOL_CALL_XML_RE.search(content)
        or _DSML_TAG_RE.search(content)
        or _DS_LEGACY_TAG_RE.search(content)
    )


def strip_tool_call_xml(content: str) -> str:
    """移除文本中的工具调用标记（整块、未闭合的截断块、零散标签）及其残留。"""
    if not content:
        return content
    content = _TOOL_CALL_XML_RE.sub('', content)
    # DSML / 旧版标记：先整块删到底（未闭合的块删到文本末尾），再清零散标签
    content = _DSML_BLOCK_RE.sub('', content)
    content = _DSML_TAG_RE.sub('', content)
    content = _DS_LEGACY_BLOCK_RE.sub('', content)
    content = _DS_LEGACY_TAG_RE.sub('', content)
    content = re.sub(r'</?(?:invoke|parameter)[^>]*>', '', content)
    return _normalize_draw_cleanup(content)


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


_IMG_MARKER_RE = re.compile(r"\[图片(\d+)\]")


def _next_image_index(messages: List[Dict[str, Any]]) -> int:
    """扫描 messages 文本中已有的 [图片N] 标记，返回下一张可用编号（最大值+1）。"""
    max_n = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, str):
            texts: Tuple[str, ...] = (content,)
        elif isinstance(content, list):
            texts = tuple(
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
        else:
            continue
        for t in texts:
            for match in _IMG_MARKER_RE.finditer(t):
                max_n = max(max_n, int(match.group(1)))
    return max_n + 1


# 尾部触发标记的文本头（chat_prompt 在触发消息前注入的 ephemeral system，不落库）
TRIGGER_MARKER_PREFIX = "[当前触发]"


def _drop_stale_trigger_marker(messages: List[Dict[str, Any]]) -> None:
    """循环邮箱插入新消息前，去掉原触发消息前的 [当前触发] 标记行：它指向的消息已经答过，
    留着会把"只回应那条、其余只作背景"的指令错套到新插入的消息上。同一条 system 里合并的
    [记忆提醒] 等其他行保留；只剩标记行时整条移除。"""
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") != "system":
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.startswith(TRIGGER_MARKER_PREFIX):
            continue
        rest = "\n".join(line for line in content.split("\n") if not line.startswith(TRIGGER_MARKER_PREFIX)).strip()
        if rest:
            messages[i] = {**m, "content": rest}
        else:
            del messages[i]


def _user_text_already_in_messages(messages: List[Dict[str, Any]], text: str) -> bool:
    """检查与该文本完全一致的 user 消息（或合并批次消息中的某一行）是否已在 messages 中。
    用于循环邮箱 entry 去重：entry 的消息在任务进入循环前已落库时，
    可能已被本轮 prompt 快照纳入，重复插入会让模型看到两条相同消息。
    邮箱批次合并为一条多行 user 消息后，按行比对。"""
    target = text.strip()
    if not target:
        return False
    for m in messages:
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, str):
            texts = [content]
        elif isinstance(content, list):
            texts = [
                str(item.get("text") or "")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
        else:
            continue
        for t in texts:
            if t.strip() == target or any(line.strip() == target for line in t.split("\n")):
                return True
    return False


def sanitize_internal_control_text(content: str) -> str:
    """清理仅供模型内部遵循的控制提示，防止其进入群消息和历史。"""
    if not content:
        return content
    content = content.replace(_TOTAL_TOOL_LIMIT_TEXT, "")
    content = content.replace(_SEARCH_TOOL_LIMIT_TEXT, "")
    content = content.replace(_TOOL_LOOP_TIMEOUT_TEXT, "")
    content = content.replace(_ROUND_LIMIT_TEXT, "")
    content = content.replace(_MARKUP_LEAK_NO_TOOLS_TEXT, "")
    content = content.replace(_MARKUP_LEAK_WITH_TOOLS_TEXT, "")
    for pattern in _INTERNAL_CONTROL_PATTERNS:
        content = pattern.sub("", content)
    # 过滤 LLM 输出的工具调用标记（XML / DSML / 旧版 tool 标记）
    content = strip_tool_call_xml(content)
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


def sanitize_draw_reply_text(content: str, allow_task_ids: bool = True) -> str:
    """清理不应直接出现在聊天中的画图占位符与内部控制文本。
    allow_task_ids 保留用于调用方兼容：伪造编号检测链已删除（force 模式改由 tool_choice 约束），
    输出侧只保留占位符回显与内部控制提示的兜底清洗。"""
    content = _clean_placeholder_echo(content)
    content = sanitize_internal_control_text(content)
    return content.strip()

enc_cache: Dict[str, Encoding] = {}
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# 单张图片的 token 估算（OpenAI vision 高分辨率约 765~1105，取保守值让预算裁剪留有余量）
IMAGE_TOKEN_ESTIMATE = 1000

# 文本 token 计数 LRU：token 裁剪的 while 循环会反复编码同一批消息文本，缓存消除 O(n²) 重编码
_TEXT_TOKEN_CACHE_MAX = 2048
_text_token_cache: "OrderedDict[Tuple[str, str], int]" = OrderedDict()

ChunkCallback = Callable[[str], Awaitable[None]]


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _format_stream_error(err: Any) -> str:
    """把 SSE body 内的 error 对象格式化为异常文本。

    部分网关（如 opencode zen）在 HTTP 200 的流里以 `data: {"error":{...}}` 下发上游错误，
    例如 `Streaming response failed: [400] At most 4 image(s) may be provided in one prompt.`。
    状态码优先取 error 自带字段，取不到则从 message 里的 [NNN] 提取，统一拼成 `HTTP <code>`，
    使上层按错误文本分流的判定（is_ctx_error / _is_image_limit_error 等）能够命中。
    """
    if isinstance(err, dict):
        message = str(err.get("message") or err.get("msg") or err)
        code = err.get("code") or err.get("status") or err.get("status_code")
    else:
        message = str(err)
        code = None
    if code is None:
        matched = re.search(r"\[(\d{3})\]", message)
        if matched:
            code = matched.group(1)
    return f"流式响应错误 HTTP {code}: {message}" if code else f"流式响应错误: {message}"


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
        # key 冷却表：scope（profile 名 / "__singleton__"）→ {key 索引: 冷却截止 monotonic 时间}。
        # 不用「粘性索引」——索引一旦前移成功也不回退，会把流量永久钉在备用 key 上。
        self._key_cooldowns: Dict[str, Dict[int, float]] = {}
        self.config = config
        self.proxy = proxy
        self.base_url = base_url
        self.extra_prompt = extra_prompt or ""
        self.last_tool_outputs: List[Dict[str, Any]] = []
        self._last_tool_outputs_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        self._current_chat_key: str = ""  # 当前会话的chat_key，供工具使用
        self._current_trigger_userid: str = ""  # 当前触发用户的userid，供工具使用
        # 循环邮箱：chat_key → 待插入的新触发消息队列（插入式打断，由运行中的 stream_response 循环在轮边界批量消费）
        self._loop_mailbox: Dict[str, Deque[Dict[str, Any]]] = {}
        # chat_key → 最近一次 stream_response 工具循环的最终消息列表引用（循环内原地 append/pop，
        # matcher 在请求结束后可读到的完整状态：含邮箱插入的新触发消息、assistant tool_calls、tool 响应）
        self._last_loop_messages_by_chat: Dict[str, List[Dict[str, Any]]] = {}
        self._last_stream_usage: Optional[Dict[str, Any]] = None  # 最近一次流式请求的 usage 信息

    _SINGLETON_KEY_SCOPE = "__singleton__"  # 无 profile 快照的兼容路径使用的 key 冷却作用域

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

    @property
    def _key_cooldowns(self) -> Dict[str, Dict[int, float]]:
        """key 冷却表：scope（profile 稳定标识 / "__singleton__"）→ {key 索引: 冷却截止 monotonic 时间}。
        懒初始化，避免 init() 之外的代码路径（测试、启动期健康检查）拿到空属性。"""
        store = getattr(self, "_key_cooldown_store", None)
        if store is None:
            store = {}
            self._key_cooldown_store = store
        return store

    @_key_cooldowns.setter
    def _key_cooldowns(self, value: Dict[str, Dict[int, float]]) -> None:
        self._key_cooldown_store = value or {}

    @property
    def _visible_images(self) -> Dict[int, str]:
        """本次请求可见图片表 {显示编号: 原始URL}（供 vision / anime_trace 读取，ContextVar 天然并发安全）。"""
        return dict(_VISIBLE_IMAGES.get() or {})

    @_visible_images.setter
    def _visible_images(self, value: Dict[int, str]) -> None:
        table: Dict[int, str] = {}
        for k, v in (value or {}).items():
            try:
                table[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
        _VISIBLE_IMAGES.set(table)

    @property
    def _current_vision_config(self) -> Dict[str, Any]:
        """视觉模型配置快照 {model, base_url, api_key, max_tokens, ...}，由 stream_response 按 profile 写入。"""
        return dict(_CURRENT_VISION_CONFIG.get() or {})

    @_current_vision_config.setter
    def _current_vision_config(self, value: Dict[str, Any]) -> None:
        _CURRENT_VISION_CONFIG.set(dict(value or {}))

    # ======== 循环邮箱（插入式打断）========
    def push_loop_input(self, chat_key: str, entry: Dict[str, Any]) -> None:
        """推入一条循环邮箱输入。entry 字段：text（按 prompt 用户消息格式预格式化，
        [HH:MM] sender: 正文，含 [回复xxx]/[图片N] 标记）、raw_text、sender、userid、
        image_urls、recorded_msg（matcher 已写入 prompt_messages 的 ChatMessageData）。"""
        if not hasattr(self, "_loop_mailbox"):
            self._loop_mailbox = {}
        self._loop_mailbox.setdefault(chat_key, deque()).append(entry)

    def drain_loop_inputs(self, chat_key: str) -> List[Dict[str, Any]]:
        """一次性取空指定会话的循环邮箱，按顺序返回全部待处理 entry。"""
        if not hasattr(self, "_loop_mailbox"):
            return []
        box = self._loop_mailbox.pop(chat_key, None)
        return list(box) if box else []

    def has_loop_inputs(self, chat_key: str) -> bool:
        """检查指定会话的循环邮箱是否有待处理输入"""
        return bool(getattr(self, "_loop_mailbox", None) and self._loop_mailbox.get(chat_key))

    async def _build_loop_batch_message(
        self,
        entries: List[Dict[str, Any]],
        img_index: int,
        multimodal_enabled: bool = True,
    ) -> Tuple[Dict[str, Any], int, Dict[int, str]]:
        """把一个打断点取空的全部邮箱 entries 合并为一条 user 消息：各行 [HH:MM] sender: 正文
        按到达顺序换行拼接；[图片N] 从 img_index 起对整批连续续编，image 部件按行顺序追加
        （含图片时经 image_cache 转 data URI 组装 multipart content）。
        返回 (message, 下一张图片编号, {编号: 原始URL})，编号表供并入 _visible_images。"""
        lines: List[str] = []
        all_images: List[str] = []
        numbered: Dict[int, str] = {}
        for entry in entries:
            text = str(entry.get("text") or "").strip()
            images = [str(u) for u in (entry.get("image_urls") or []) if u]
            if images:
                base = img_index
                found = len(_IMG_MARKER_RE.findall(text))
                text = _IMG_MARKER_RE.sub(lambda m: f"[图片{int(m.group(1)) + base - 1}]", text)
                # 文本中的占位符比图片少（异常数据/纯图片消息）：追加缺失的标记
                for i in range(found, len(images)):
                    text = f"{text} [图片{base + i}]".strip()
                for i, url in enumerate(images):
                    numbered[base + i] = url
                img_index += len(images)
                all_images.extend(images)
            if text:
                lines.append(text)
        text = "\n".join(lines) or "[图片]"
        message: Dict[str, Any] = {"role": "user", "content": text}
        if all_images and multimodal_enabled:
            from . import image_cache
            resolved = await image_cache.resolve_urls(all_images)
            if resolved:
                message["content"] = [{"type": "text", "text": text}] + [
                    {"type": "image_url", "image_url": {"url": url}} for url in resolved
                ]
        return message, img_index, numbered

    def switch_profile(self, profile_name: str, profile: Dict[str, Any]) -> str:
        """切换 OpenAI 配置 profile，返回切换结果描述"""
        self.api_keys = profile.get("api_keys", [""]) or [""]
        # 显式切换 profile（如 rg model）时清掉该 profile 的 key 冷却，
        # 让新配置乖乖从第一顺位 key 开始试
        self._reset_key_cooldowns(str(profile_name or self._profile_key_id(profile)))
        self._reset_key_cooldowns(self._profile_key_id(profile))
        self.base_url = profile.get("base_url", "")
        self.use_socket_proxy = profile.get("use_socket_proxy", False)
        self.proxy = profile.get("proxy") or None
        self.multimodal = profile.get("multimodal", True)
        self.extra_prompt = profile.get("extra_prompt", "") or ""
        self.config = {
            "model": profile.get("model", ""),
            "model_mini": profile.get("model_mini", ""),
            "max_tokens": profile.get("max_tokens", 4096),
            "temperature": profile.get("temperature"),
            "top_p": profile.get("top_p"),
            "frequency_penalty": profile.get("frequency_penalty"),
            "presence_penalty": profile.get("presence_penalty"),
            "max_summary_tokens": profile.get("max_summary_tokens", 800),
            "timeout": profile.get("timeout", 60),
            "enable_stream": self.config.get("enable_stream", True),
            "reasoning_effort": profile.get("reasoning_effort"),
        }
        proxy_info = f"socks:{self.proxy}" if self.use_socket_proxy and self.proxy else ("直连" if not self.proxy else self.proxy)
        return f"模型: {self.config['model']} | mini: {self.config['model_mini']} | base_url: {self.base_url or '默认'} | 代理: {proxy_info}"

    @staticmethod
    def _profile_key_id(profile: Dict[str, Any]) -> str:
        """profile 快照的稳定标识，用于 per-profile 的 key 冷却表"""
        name = profile.get("name")
        if name:
            return str(name)
        return f"{profile.get('base_url', '')}|{profile.get('model', '')}"

    def _prune_key_cooldowns(self, scope: str) -> Dict[int, float]:
        """清理 scope 下已到期的冷却记录，返回仍在冷却中的 {key 索引: 截止时间}"""
        cooldowns = self._key_cooldowns.get(scope)
        if not cooldowns:
            return {}
        now = time.monotonic()
        for index in [i for i, until in cooldowns.items() if until <= now]:
            cooldowns.pop(index, None)
        return cooldowns

    def _key_plan(self, scope: str, api_keys: List[str], tried: Optional[Set[int]] = None) -> Tuple[str, int]:
        """选定本次请求使用的 key，返回 (key, 索引)。

        选择顺序**恒定从第一顺位开始**：跳过仍在冷却中的 key 和本次请求内已经试过的 key。
        全部不可用时退回第一顺位（宁可复用也不要无 key 可用）。"""
        if not api_keys:
            return "", 0
        count = len(api_keys)
        if count == 1:
            return api_keys[0], 0
        cooldowns = self._prune_key_cooldowns(scope)
        tried = tried or set()
        for index in range(count):
            if index in cooldowns or index in tried:
                continue
            if index != 0:
                logger.info(
                    f"API key 顺延使用第 {index + 1}/{count} 个 | scope: {scope} | "
                    f"前序 key 冷却中或本次请求内已失败"
                )
            return api_keys[index], index
        return api_keys[0], 0

    def _mark_key_failed(self, scope: str, index: int) -> None:
        """把失败的 key 打进冷却：后续请求自动顺延，冷却到期自动回到第一顺位。"""
        self._key_cooldowns.setdefault(scope, {})[int(index)] = time.monotonic() + KEY_FAILURE_COOLDOWN_SECONDS

    def _reset_key_cooldowns(self, scope: str) -> None:
        """清空 scope 的冷却记录（profile 被显式切换/重载时调用，重新从第一顺位开始）"""
        self._key_cooldowns.pop(scope, None)

    def _current_key(self) -> str:
        """无 profile 快照的兼容路径取 key：同 _key_plan，恒定优先第一顺位"""
        key, _ = self._key_plan(self._SINGLETON_KEY_SCOPE, list(self.api_keys or [""]))
        return key

    def _request_state(self, profile: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if profile:
            api_keys = profile.get("api_keys", [""]) or [""]
            request_config = {
                "model": profile.get("model", ""),
                "model_mini": profile.get("model_mini", ""),
                "max_tokens": profile.get("max_tokens", 4096),
                "temperature": profile.get("temperature"),
                "top_p": profile.get("top_p"),
                "frequency_penalty": profile.get("frequency_penalty"),
                "presence_penalty": profile.get("presence_penalty"),
                "max_summary_tokens": profile.get("max_summary_tokens", 800),
                "timeout": profile.get("timeout", 60),
                "enable_stream": profile.get("enable_stream", self.config.get("enable_stream", True)),
                "reasoning_effort": profile.get("reasoning_effort"),
            }
            api_key, api_key_index = self._key_plan(self._profile_key_id(profile), api_keys)
            return {
                "api_key": api_key,
                "api_key_index": api_key_index,
                "key_scope": self._profile_key_id(profile),
                "config": request_config,
                "base_url": profile.get("base_url", ""),
                "proxy": profile.get("proxy") or None,
                "use_socket_proxy": profile.get("use_socket_proxy", False),
                "multimodal": profile.get("multimodal", True),
                "keep_reasoning": bool(profile.get("keep_reasoning", False)),
            }
        api_key, api_key_index = self._key_plan(self._SINGLETON_KEY_SCOPE, list(self.api_keys or [""]))
        return {
            "api_key": api_key,
            "api_key_index": api_key_index,
            "key_scope": self._SINGLETON_KEY_SCOPE,
            "config": dict(self.config),
            "base_url": self.base_url,
            "proxy": self.proxy,
            "use_socket_proxy": getattr(self, "use_socket_proxy", False),
            "multimodal": getattr(self, "multimodal", True),
            "keep_reasoning": bool(self.config.get("keep_reasoning", False)),
        }

    def _build_vision_config(self, request_profile: Optional[Dict[str, Any]], request_state: Dict[str, Any]) -> Dict[str, Any]:
        """根据本轮 profile 构造视觉模型配置快照。
        仅当主模型 multimodal=false 且配置了 model_vision 时返回非空配置，供 vision 工具读取。
        缺省复用本 profile 的 base_url / api_keys；可选覆盖 model_vision_base_url / model_vision_api_keys / model_vision_max_tokens。
        """
        if not request_profile:
            return {}
        # 原生多模态 profile 不走视觉工具
        if request_profile.get("multimodal", request_state.get("multimodal", True)):
            return {}
        vision_model = request_profile.get("model_vision")
        if not vision_model:
            return {}
        api_keys = request_profile.get("model_vision_api_keys") or request_profile.get("api_keys") or [""]
        # 视觉模型可用独立的 key 列表（model_vision_api_keys），故用独立冷却作用域：
        # 主模型 key 的冷却状态不应该影响视觉模型选 key
        vision_scope = f"{self._profile_key_id(request_profile)}#vision"
        vision_key, _ = self._key_plan(vision_scope, list(api_keys))
        return {
            "model": vision_model,
            "base_url": request_profile.get("model_vision_base_url") or request_profile.get("base_url", "") or "",
            "api_key": vision_key,
            "max_tokens": request_profile.get("model_vision_max_tokens", 1024),
            "timeout": request_profile.get("timeout", 60),
            "proxy": request_profile.get("proxy"),
            "use_socket_proxy": request_profile.get("use_socket_proxy", False),
        }

    def _completion_kwargs(
        self,
        messages: List[Dict[str, Any]],
        type: str,
        stream: bool,
        tools: Optional[List[Dict[str, Any]]] = None,
        request_state: Optional[Dict[str, Any]] = None,
        tool_choice: Optional[Any] = None,
    ) -> Dict[str, Any]:
        state = request_state or self._request_state()
        request_config = state.get("config") or self.config
        # 为 API 请求准备消息副本：确保 assistant tool_calls 消息 content 非空
        # 避免 provider（如 Moonshot）因空 content 拒绝多轮工具调用。
        # reasoning_content 不在此剥离：跨轮历史在 stream_response 循环起始处按
        # profile keep_reasoning 统一处理，循环内 assistant 的思考链原样保留。
        api_messages: List[Dict[str, Any]] = []
        for msg in messages:
            if msg.get("role") == "assistant":
                c = msg.get("content")
                # assistant 消息 content 不能为空（Moonshot 等 provider 会 400）
                if c is None or (isinstance(c, str) and not c.strip()):
                    msg = dict(msg)
                    msg["content"] = "[无内容]"
            api_messages.append(msg)
        # 当前 profile 不支持多模态时，剥离 image_url 内容
        if not state.get("multimodal", True):
            for msg in api_messages:
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
            "messages": api_messages,
            "timeout": request_config.get("timeout", 30),
            "stream": stream,
            "api_key": state.get("api_key", ""),
        }
        if type not in {"summarize", "impression"}:
            kwargs["max_tokens"] = request_config.get("max_tokens", 1024)
        for optional_key in ("temperature", "top_p", "frequency_penalty", "presence_penalty"):
            value = request_config.get(optional_key)
            if value is not None:
                kwargs[optional_key] = value
        # reasoning_effort（如 "low"/"medium"/"high"）：仅主对话请求透传，摘要/印象走 model_mini 不传
        if type not in {"summarize", "impression"}:
            reasoning_effort = request_config.get("reasoning_effort")
            if reasoning_effort is not None:
                kwargs["reasoning_effort"] = reasoning_effort
        if state.get("base_url"):
            kwargs["base_url"] = state["base_url"]
        # 代理：use_socket_proxy=True 时将 proxy 作为 socks 代理地址
        effective_proxy = state.get("proxy") if (state.get("proxy") and state.get("use_socket_proxy")) else None
        if effective_proxy:
            kwargs["proxy"] = effective_proxy
        if tools:
            kwargs["tools"] = tools
            # tool_choice：None 默认 "auto"；"" 完全省略该字段（provider 不支持 tool_choice 的降级重试）；
            # 其余值（"none" / 指定函数的 dict）原样透传
            if tool_choice is None:
                kwargs["tool_choice"] = "auto"
            elif tool_choice != "":
                kwargs["tool_choice"] = tool_choice
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
            # Moonshot 等 provider 要求 assistant 消息 content 非空，填充占位符
            for msg in prompt:
                if isinstance(msg, dict) and msg.get("role") == "assistant":
                    c = msg.get("content")
                    if c is None or (isinstance(c, str) and not c.strip()):
                        msg["content"] = "[无内容]"
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
        headers.update(_build_provider_headers(base_url))

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
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty", "reasoning_effort"):
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
        headers.update(_build_provider_headers(base_url))

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }

        if "temperature" in kwargs:
            body["temperature"] = kwargs["temperature"]
        if "max_tokens" in kwargs:
            body["max_tokens"] = kwargs["max_tokens"]
        for optional_key in ("top_p", "frequency_penalty", "presence_penalty", "reasoning_effort"):
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
        on_tool_call: Optional[Callable] = None,
        tool_choice: Optional[Any] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str]:
        kwargs = self._completion_kwargs(messages, type, True, tools, request_state, tool_choice=tool_choice)
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_call_chunks: Dict[int, Dict[str, Any]] = {}
        last_usage: Optional[Dict[str, Any]] = None
        tool_call_notified = False  # 本轮是否已通知过工具调用（每个流式轮只通知一次）

        async for chunk in self._stream_iter_openai(kwargs):
            # provider 以 HTTP 200 在流内下发上游错误（该 chunk 既无 choices 也无 usage）：
            # 若继续静默跳过，本轮会以空内容“成功”返回，上层拿不到任何错误、也不会重试，
            # 表现为机器人一声不吭。抬成异常交由 stream_response 的 except 统一处理。
            stream_error = _get(chunk, "error")
            if stream_error:
                raise RuntimeError(_format_stream_error(stream_error))
            choices = _get(chunk, "choices", [])
            if not choices:
                # 空 choices 可能是携带 usage 的最终 chunk
                usage = _get(chunk, "usage")
                if usage:
                    last_usage = usage
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
                if on_tool_call and not tool_call_notified:
                    tool_call_notified = True
                    await on_tool_call(tool_call)
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

            # 某些 provider 在最后一个 choice chunk 中携带 usage
            usage = _get(chunk, "usage")
            if usage:
                last_usage = usage

        self._last_stream_usage = last_usage
        # 统计本次请求的 token 消耗（按实际模型名分桶）
        try:
            from .stats import stats
            stats.record_model_usage(kwargs.get("model"), last_usage)
        except Exception:
            pass
        return (
            "".join(content_parts),
            [v for _, v in sorted(tool_call_chunks.items()) if v["function"]["name"] or v.get("id")],
            "".join(reasoning_parts),
        )

    async def _complete_once(
        self,
        messages: List[Dict[str, Any]],
        type: str,
        tools: Optional[List[Dict[str, Any]]],
        request_state: Optional[Dict[str, Any]] = None,
        on_tool_call: Optional[Callable] = None,
        tool_choice: Optional[Any] = None,
    ) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
        kwargs = self._completion_kwargs(messages, type, False, tools, request_state, tool_choice=tool_choice)
        response = await self._acompletion(**kwargs)
        message = _get(_get(response, "choices", [])[0], "message", {})
        message_dict = _message_to_dict(message)
        content = str(message_dict.get("content") or "")
        tool_calls = message_dict.get("tool_calls") or []
        if tool_calls and on_tool_call:
            await on_tool_call(tool_calls)
        # 统计非流式请求的 token 消耗
        usage = _get(response, "usage")
        self._last_stream_usage = usage
        try:
            from .stats import stats
            stats.record_model_usage(kwargs.get("model"), usage)
        except Exception:
            pass
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
            # 统计工具调用次数（无论成功失败）
            try:
                from .stats import stats
                stats.inc_tool_call(name)
            except Exception:
                pass
            try:
                tool_content, attachments = await execute_tool(name, args, plugin_config)
                logger.info(f"[工具返回] {name} → {tool_content[:200]}{'...' if len(tool_content) > 200 else ''}")
            except Exception as e:
                logger.error(f"[工具调用失败] {name}(tool_call_id={tool_call_id}): {e}")
                tool_content = f"工具调用失败: {e}"
                attachments = []
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
        on_tool_call: Optional[Callable] = None,
        on_reply_complete: Optional[Callable[[str, List[Dict[str, Any]], Optional[List[Dict[str, Any]]]], Awaitable[None]]] = None,
    ) -> Tuple[str, bool, List[Dict[str, Any]], str]:
        custom = custom or {}
        # 逐 dict 浅拷贝即可：循环内对消息的所有改动（reasoning pop、图片剥离、空 content 填充）
        # 都是 dict 级替换而非原地改嵌套列表，调用方的 prompt 对象保持请求前快照不被污染。
        messages = [dict(m) for m in self._normalize_prompt(prompt, custom)]
        request_chat_key = self._current_chat_key
        # 驻留循环消息列表引用：循环内只做原地 append/pop，请求结束后 matcher 读到的即最终完整状态
        #（含循环邮箱插入的新触发消息与工具调用段），写入 latest.json 的 loop_messages 便于排查。
        if request_chat_key:
            self._last_loop_messages_by_chat[request_chat_key] = messages
        request_trigger_userid = self._current_trigger_userid
        request_state = self._request_state(request_profile)
        # 本次请求内已试过的 key 索引：key 级失败时顺延到下一个 key 重试，避免一个坏 key
        # 直接把整条回复判死（顺延顺序仍是第一顺位优先）
        _tried_key_indices: Set[int] = {int(request_state.get("api_key_index", 0))}
        # 跨轮持久化历史中的 reasoning 默认剥离（profile keep_reasoning=true 才保留，
        # 兼容不接受该字段的 provider）；本循环内 append 的 assistant 消息在此之后产生，
        # 思考链在同一 user turn 的工具循环内天然保留，保证多步工具任务连贯。
        if not request_state.get("keep_reasoning", False):
            for _m in messages:
                _m.pop("reasoning_content", None)
        # 视觉工具快照：本次请求可见图片表 + 视觉模型配置，按本轮 profile 钉死，避免多群并发串数据。
        # visible_images 由 matcher 在调用 stream_response 前写入；这里重新置位以绑定到本次请求的上下文。
        self._visible_images = dict(self._visible_images or {})
        self._current_vision_config = self._build_vision_config(request_profile, request_state)
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
        _allow_terminal_tools = False  # 工具超限后进入终端阶段：允许终端工具（画图/记忆）单次调用后收尾
        _terminal_wrapup_pending = False  # 终端工具已执行一次，下一轮为无工具收尾轮（tool_choice="none"）
        _bad_json_retries = 0  # 工具参数 JSON 全丢的重试次数（≤1，第二次撤工具强制出文本）
        _tool_choice_dropped = False  # provider 不支持 tool_choice（400）后置 True，后续轮完全省略该字段
        _force_draw_hint_injected = False  # force 模式的尾部强制画图提示是否已注入（仅一次）
        _reasoning_stripped = False  # keep_reasoning=true 但 provider 400 拒绝 reasoning_content 后置 True，剥离重试一次
        internal_control_injected = False  # 是否向模型注入过内部控制提示
        _round_limit_injected = False  # 轮数上限提示是否已注入（同一最后一轮内的各种 continue 重试不重复追加）
        _markup_leak_retries = 0  # 正文泄漏工具调用标记（DSML/XML）后的重试次数（≤1）
        _drop_tools_this_round = False  # 泄漏重试轮：整段去掉 tools（一次性标志，读取后即清零）

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

        # 画图工具常驻注册（auto/on/force 与漫画模式），仅 off 模式过滤；
        # 调用时机改由工具 description 与 S1 画图行为规则约束，保持 tools 数组稳定以最大化缓存命中
        from .llm_tool_plugins import anima_generate as _ag
        _draw_mode = _ag.get_chat_mode(request_chat_key) if request_chat_key else "auto"
        _is_manga = _ag.get_manga_mode(request_chat_key) if request_chat_key else False
        if not _is_manga and _draw_mode == "off":
            # danbooru_search 只服务于画图时的作画标签确定，随画图工具一起进出，避免闲聊轮白占工具位
            tool_schemas = [s for s in tool_schemas if s.get("function", {}).get("name") not in _DRAW_ONLY_TOOLS]
        # force 模式 + 画图关键词：尾部注入强制画图提示（不用 tool_choice——主流 provider
        # 在思考模式下均不兼容强制指定，会 400；提示词强制 + matcher 伪造编号拦截兜底）
        _force_draw_request = (_draw_mode == "force" and _has_draw_request)

        _image_stripped = False  # 工具调用后续轮是否已剥离图片

        # 工具循环终止保护：轮数闸门（LLM_MAX_TOOL_ROUNDS）+ 时间闸门（LLM_TOOL_LOOP_MAX_SECONDS）
        _tool_loop_max_seconds = getattr(plugin_config, "LLM_TOOL_LOOP_MAX_SECONDS", 180) if plugin_config else 180
        loop_start = time.monotonic()

        round_idx = 0
        round_tool_choice: Any = None  # 本轮请求的 tool_choice（None=默认 auto，"none"=强制出文本，""=省略字段）

        _all_reply_texts: List[str] = []  # 本次循环已完成的全部回复文本（逐轮落库，返回值保留全部文本）
        _entries_for_next_reply: List[Dict[str, Any]] = []  # 上一回复边界插入的邮箱批次（下一个回复所应答的新消息）

        async def _emit_reply(merged_text: str) -> None:
            """一个回复完成（无 tool_calls 的最终文本轮）：累计文本并立即回调 matcher 逐轮落库。
            每个完成的回复恰好回调一次（含因插入而继续前的这一轮与整个循环的最终轮）。
            reply_entries 为本回复应答的邮箱插入批次（空 → None，表示应答原始触发消息）。"""
            nonlocal _entries_for_next_reply
            if not merged_text or not merged_text.strip():
                return
            _all_reply_texts.append(merged_text)
            if on_reply_complete:
                await on_reply_complete(merged_text, list(tool_messages), list(_entries_for_next_reply) or None)
            _entries_for_next_reply = []

        def _joined_reply_text(fallback: str) -> str:
            """返回全部已完成回复的合并文本（无已完成回复时回退本轮 merged）。"""
            return "\n\n".join(_all_reply_texts) if _all_reply_texts else (fallback or "")

        async def _insert_mailbox_entries(reply_completed: bool, round_content: str = "", round_reasoning: str = "") -> bool:
            """轮边界批量消费循环邮箱：一次性取空，把新触发消息合并为一条多行 user 消息插入 messages。
            返回 True 表示已插入、循环应继续（round_idx 与 loop_start 已重置）。
            终端阶段（_allow_terminal_tools/_terminal_wrapup_pending）不接收插入，
            残留 entry 由 matcher 任务收尾时作为新触发重新处理。
            reply_completed=True 表示上一轮产出了最终文本（无 tool_calls，回复已完成）；
            False 表示上一轮以 tool_calls 结束（被插入打断的触发尚未完成回复）。"""
            nonlocal round_idx, loop_start, intermediate_texts, tool_messages, _entries_for_next_reply, _round_limit_injected
            if not request_chat_key or not hasattr(self, "_loop_mailbox"):
                return False
            if _allow_terminal_tools or _terminal_wrapup_pending:
                if self._loop_mailbox.get(request_chat_key):
                    logger.info(
                        f"[循环邮箱] 终端阶段跳过插入 | 会话: {request_chat_key} | "
                        f"残留 {len(self._loop_mailbox[request_chat_key])} 条将由 matcher 重新处理"
                    )
                return False
            drained = self.drain_loop_inputs(request_chat_key)
            if not drained:
                return False
            # 去重：entry 的消息在任务进入循环前已落库、并被本轮 prompt 快照纳入时跳过插入
            entries: List[Dict[str, Any]] = []
            for entry in drained:
                entry_text = str(entry.get("text") or "").strip()
                if entry_text and _user_text_already_in_messages(messages, entry_text):
                    logger.info(
                        f"[循环邮箱] entry 已在当前 messages 中，跳过插入 | 会话: {request_chat_key} | "
                        f"sender={entry.get('sender')}"
                    )
                    continue
                entries.append(entry)
            if not entries:
                return False
            # 原触发消息的 [当前触发] 标记已经完成使命（那条已答/正在答），去掉以免错套到新消息上
            _drop_stale_trigger_marker(messages)
            if reply_completed:
                # 刚产出的本轮回复作为 assistant 消息入列（仅本轮 content；
                # 中间轮文本已在之前的 assistant(tool_calls) 消息里），模型完整看到自己说过的话
                assistant_reply: Dict[str, Any] = {"role": "assistant", "content": round_content or ""}
                if round_reasoning:
                    assistant_reply["reasoning_content"] = round_reasoning
                messages.append(assistant_reply)
            count = len(entries)
            # 未处理标记（ephemeral system：不落历史、不进 tool_messages、不进返回的 tool_messages 列表）。
            # 要求逐条分别回应、各自成段、点名对象——多条新消息合并为下一条 user 消息，
            # 避免"一并回应"把不同人的话题揉进一段。
            if reply_completed:
                notice = (
                    f"[新消息提醒] 你上一条回复之后新到 {count} 条消息，见下一条。"
                    "请逐条分别回应：每条各自成段、开头点名对象，不要把不同人的话题并进同一句；"
                    "与新消息无关的旧上下文不要牵扯。"
                )
            else:
                notice = (
                    "[新消息提醒] 你上一条消息的回复尚未完成，请先完成它并单独成段；"
                    f"之后对下一条中的 {count} 条新消息逐条分别回应，每条各自成段、开头点名对象。"
                )
            messages.append({"role": "system", "content": notice})
            multimodal_enabled = bool(getattr(plugin_config, "MULTIMODAL_ENABLE", True)) if plugin_config else True
            # 整批合并为一条 user 消息；图片编号从当前 messages 最大 [图片N] 续编，并入可见图片表
            img_index = _next_image_index(messages)
            batch_msg, img_index, numbered = await self._build_loop_batch_message(entries, img_index, multimodal_enabled)
            messages.append(batch_msg)
            if numbered:
                visible = dict(self._visible_images)
                visible.update(numbered)
                self._visible_images = visible
            # 触发者上下文更新为批次中最新的发言者（记忆工具 user 维度归属最新触发者）
            for entry in entries:
                if entry.get("userid"):
                    self._current_trigger_userid = str(entry["userid"])
            # 新输入到来：重置轮数与循环计时（终端标志此时必为 False），继续循环。
            # per-reply 累积（中间文本/工具消息）仅在刚完成的回复已经 on_reply_complete 落库
            # （reply_completed=True）时清零；工具轮被插入打断（False）时工具段与中间文本
            # 尚未落库，必须保留给最终完成回复的回调一并落库，否则历史丢失整个工具调用段。
            round_idx = 0
            loop_start = time.monotonic()
            _round_limit_injected = False  # 新输入重开轮数，再到最后一轮时要重新提示
            if reply_completed:
                intermediate_texts = []
                tool_messages = []
            # 记录本批次 entry，随下一个完成的回复回调给 matcher（该回复应答的就是这批新消息）
            _entries_for_next_reply = list(entries)
            logger.info(
                f"[循环邮箱] 轮边界批量插入 | 会话: {request_chat_key} | 批量: {count} | "
                f"senders: {', '.join(str(e.get('sender') or 'anonymous') for e in entries)} | "
                f"上一轮未完成回复: {not reply_completed}"
            )
            return True

        while True:
            try:
                # 时间闸门：工具循环总耗时超限 → 进终端轮优雅收尾（不强制丢弃已产出文本）
                # 协议约束：该检查点在轮边界，正常不会紧跟 assistant(tool_calls)；
                # 若为异常重试等罕见路径导致末尾是带 tool_calls 的 assistant，则跳过提示注入，
                # 仅置终端标志，避免 system 隔断 assistant(tool_calls) → tool 响应的连续配对
                if not _allow_terminal_tools and time.monotonic() - loop_start > _tool_loop_max_seconds:
                    logger.warning(f"工具循环总耗时超过 {_tool_loop_max_seconds}s，进入终端轮收尾")
                    _tail_msg = messages[-1] if messages else {}
                    if not (_tail_msg.get("role") == "assistant" and _tail_msg.get("tool_calls")):
                        messages.append({"role": "system", "content": _TOOL_LOOP_TIMEOUT_TEXT})
                        internal_control_injected = True
                    round_idx = max_rounds
                    _allow_terminal_tools = True

                # force 模式：不使用 tool_choice 强制指定（主流 provider 在思考模式下均不兼容，
                # 会 400），改为在消息尾部注入强制画图提示（仅一次；尾部追加不破坏历史前缀缓存；
                # 末尾是 assistant(tool_calls) 时跳过注入，避免 system 隔断 tool_calls → tool 响应的
                # 连续配对）。模型仍不调用时由 matcher 的伪造编号拦截/漫画强制画图兜底。
                if (
                    _force_draw_request
                    and not has_anima_call
                    and not _force_draw_hint_injected
                ):
                    _force_draw_hint_injected = True
                    _tail_msg = messages[-1] if messages else {}
                    if not (_tail_msg.get("role") == "assistant" and _tail_msg.get("tool_calls")):
                        messages.append({"role": "system", "content": _FORCE_DRAW_HINT_TEXT})
                        logger.info("force 模式：已在尾部注入强制画图提示（不使用 tool_choice）")

                # 最后一轮 tools 数组保持全量不变，改传 tool_choice="none" 强制模型直接回复（保住缓存前缀）；
                # 终端阶段（_allow_terminal_tools）同样保持全量 tools，非终端调用在执行侧过滤
                is_last_round = round_idx >= max_rounds
                _terminal_this_round = is_last_round and _allow_terminal_tools and not _terminal_wrapup_pending
                current_tools = tool_schemas
                if _tool_choice_dropped:
                    round_tool_choice = ""  # 降级：完全省略 tool_choice 字段
                elif _terminal_wrapup_pending or (is_last_round and not _allow_terminal_tools):
                    round_tool_choice = "none"
                else:
                    round_tool_choice = None
                # 本轮是否禁止调工具（泄漏处理据此分流）；泄漏重试轮整段去掉 tools——
                # 宁可这一轮缓存前缀失效，也不能让同一段标记再漏一次
                _no_tools_round = _drop_tools_this_round or round_tool_choice == "none"
                if _drop_tools_this_round:
                    _drop_tools_this_round = False
                    current_tools = None
                    round_tool_choice = ""

                # 轮数上限的最后一轮（终端工具轮不注入）：明确告诉模型不能再调工具、直接出文本——
                # 只靠 tool_choice="none" 不够，DeepSeek 系仍会把想调的工具以原生标记写进 content。
                # 顺带列出中间轮已说过的话避免重复。置 internal_control_injected 让本轮走缓冲输出，
                # 万一仍泄漏也能在发出前拦下。只注入一次，本轮内的各种 continue 重试不重复追加。
                if is_last_round and not _allow_terminal_tools and not _round_limit_injected:
                    _round_limit_injected = True
                    hint = _ROUND_LIMIT_TEXT
                    if intermediate_texts:
                        hint += "\n你在工具调用阶段已说过以下内容，最终回复中不要重复，只补充新信息：\n" + "\n".join(intermediate_texts)
                    messages.append({"role": "system", "content": hint})
                    internal_control_injected = True

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
                    safe_text = sanitize_internal_control_text("".join(control_stream_buf))
                    safe_text = sanitize_draw_reply_text(safe_text, allow_task_ids=has_anima_call)
                    if safe_text:
                        await on_text(safe_text)

                async def _handle_markup_leak(round_text: str) -> Optional[str]:
                    """文本收尾轮的正文里混入工具调用标记（DSML/XML）时的处理。
                    首次：丢弃本轮文本，注入提示后重试一轮（本轮本就禁止调工具 → 重试轮连 tools 都不带）；
                    再次：不再重试，剥掉标记后按文本收尾。返回 None 表示调用方应 continue 重试。"""
                    nonlocal _markup_leak_retries, _drop_tools_this_round, internal_control_injected
                    if not contains_tool_call_xml(round_text):
                        return round_text
                    if _markup_leak_retries < 1:
                        _markup_leak_retries += 1
                        if _no_tools_round:
                            _drop_tools_this_round = True
                            notice = _MARKUP_LEAK_NO_TOOLS_TEXT
                            mode = "本轮禁止调工具，重试轮去掉 tools"
                        else:
                            notice = _MARKUP_LEAK_WITH_TOOLS_TEXT
                            mode = "本轮允许调工具"
                        messages.append({"role": "system", "content": notice})
                        internal_control_injected = True
                        logger.warning(f"[工具标记泄漏] 正文混入工具调用标记（{mode}），丢弃本轮文本并重试: {round_text[:120]!r}")
                        return None
                    stripped = strip_tool_call_xml(round_text)
                    logger.warning(f"[工具标记泄漏] 重试后仍泄漏，已剥离标记按文本收尾: {round_text[:120]!r}")
                    return stripped

                if (request_state.get("config") or {}).get("enable_stream", True):
                    content, tool_calls, reasoning_content = await self._stream_once(
                        messages, type, current_tools, effective_on_text, round_on_reasoning, request_state, on_tool_call, tool_choice=round_tool_choice
                    )
                    # 过滤参数 JSON 不完整的 tool_calls（流式截断导致），让模型重试
                    if tool_calls:
                        _valid_tool_calls = []
                        for tc in tool_calls:
                            raw_args = _get(tc, "function", {}).get("arguments", "")
                            try:
                                json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else raw_args
                                _valid_tool_calls.append(tc)
                            except (json.JSONDecodeError, ValueError):
                                func_name = _get(tc, "function", {}).get("name", "?")
                                logger.warning(f"[工具调用] 丢弃参数不完整的 tool_call: {func_name}({raw_args!r})")
                        if len(_valid_tool_calls) < len(tool_calls):
                            if not _valid_tool_calls:
                                # 全部丢弃：首次注入提示让模型重新调用；
                                # 第二次撤工具（下一轮 tool_choice="none"）强制出文本，防无界循环
                                tool_calls = []
                                _bad_json_retries += 1
                                if _bad_json_retries > 1:
                                    round_idx = max_rounds
                                    _terminal_wrapup_pending = True
                                    messages.append({"role": "system", "content": "工具调用参数多次不完整（JSON 截断），停止调用工具，直接基于已有信息用文字回复。"})
                                    internal_control_injected = True
                                    continue
                                messages.append({"role": "system", "content": "你刚才的工具调用参数不完整（JSON 截断），请重新调用。"})
                                continue
                            tool_calls = _valid_tool_calls
                    if not tool_calls:
                        final_reasoning_content = reasoning_content or ""
                        # 工具调用后续轮返回空内容：可能是图片撑爆上下文导致，
                        # 剥离图片后重试（兜底，正常情况下 except 分支已处理异常场景）
                        if not content.strip() and not intermediate_texts and tool_messages and not _image_stripped:
                            _image_stripped = True
                            for m in messages:
                                if isinstance(m.get("content"), list):
                                    text_parts = [c.get("text", "") for c in m["content"] if isinstance(c, dict) and c.get("type") == "text"]
                                    m["content"] = "\n".join(text_parts) if text_parts else "[图片已省略]"
                            logger.warning("工具轮返回空内容，已剥离图片并重试")
                            continue
                        # 正文混入工具调用标记：首次丢弃重试，再次剥离后收尾
                        _clean_content = await _handle_markup_leak(content)
                        if _clean_content is None:
                            continue
                        content = _clean_content
                        if control_stream_buf is not None:
                            await _flush_control_stream_buffer()
                        merged_reply = _merge_intermediate(content)
                        # 逐轮落库：本回复完成，立即回调 matcher 落库（每个回复恰好一次）
                        await _emit_reply(merged_reply)
                        # 回复完成边界：邮箱有新触发消息则批量插入并继续循环（终端阶段内部跳过）
                        if await _insert_mailbox_entries(
                            reply_completed=bool(merged_reply and merged_reply.strip()),
                            round_content=content,
                            round_reasoning=reasoning_content or "",
                        ):
                            continue
                        return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                    if _terminal_wrapup_pending:
                        # 收尾轮模型仍返回 tool_calls（不遵守 tool_choice="none"）：不再执行工具，直接以已产出文本收尾
                        final_reasoning_content = reasoning_content or ""
                        if control_stream_buf is not None:
                            await _flush_control_stream_buffer()
                        merged_reply = _merge_intermediate(content)
                        await _emit_reply(merged_reply)
                        # 终端阶段不接收插入，仅记录邮箱残留日志（残留由 matcher 任务收尾时重新处理）
                        await _insert_mailbox_entries(reply_completed=True)
                        return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                    if is_last_round and not _allow_terminal_tools:
                        final_reasoning_content = reasoning_content or ""
                        if control_stream_buf is not None:
                            await _flush_control_stream_buffer()
                        if content or intermediate_texts:
                            merged_reply = _merge_intermediate(content)
                            await _emit_reply(merged_reply)
                            if await _insert_mailbox_entries(
                                reply_completed=bool(merged_reply and merged_reply.strip()),
                                round_content=content,
                                round_reasoning=reasoning_content or "",
                            ):
                                continue
                            return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                        if not _image_stripped:
                            _image_stripped = True
                            for m in messages:
                                if isinstance(m.get("content"), list):
                                    text_parts = [c.get("text", "") for c in m["content"] if isinstance(c, dict) and c.get("type") == "text"]
                                    m["content"] = "\n".join(text_parts) if text_parts else "[图片已省略]"
                            logger.warning("最后一轮返回空内容，已剥离图片并重试")
                            continue
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
                    content, tool_calls, message_dict = await self._complete_once(messages, type, current_tools, request_state, on_tool_call, tool_choice=round_tool_choice)
                    # 过滤参数 JSON 不完整的 tool_calls
                    if tool_calls:
                        _valid_tool_calls = []
                        for tc in tool_calls:
                            raw_args = _get(tc, "function", {}).get("arguments", "")
                            try:
                                json.loads(raw_args) if isinstance(raw_args, str) and raw_args.strip() else raw_args
                                _valid_tool_calls.append(tc)
                            except (json.JSONDecodeError, ValueError):
                                func_name = _get(tc, "function", {}).get("name", "?")
                                logger.warning(f"[工具调用] 丢弃参数不完整的 tool_call: {func_name}({raw_args!r})")
                        if len(_valid_tool_calls) < len(tool_calls):
                            if not _valid_tool_calls:
                                # 全部丢弃：首次注入提示让模型重新调用；
                                # 第二次撤工具（下一轮 tool_choice="none"）强制出文本，防无界循环
                                tool_calls = []
                                _bad_json_retries += 1
                                if _bad_json_retries > 1:
                                    round_idx = max_rounds
                                    _terminal_wrapup_pending = True
                                    messages.append({"role": "system", "content": "工具调用参数多次不完整（JSON 截断），停止调用工具，直接基于已有信息用文字回复。"})
                                    internal_control_injected = True
                                    continue
                                messages.append({"role": "system", "content": "你刚才的工具调用参数不完整（JSON 截断），请重新调用。"})
                                continue
                            tool_calls = _valid_tool_calls
                    if not tool_calls:
                        final_reasoning_content = message_dict.get("reasoning_content", "")
                        # 正文混入工具调用标记：首次丢弃重试，再次剥离后收尾
                        _clean_content = await _handle_markup_leak(content)
                        if _clean_content is None:
                            continue
                        content = _clean_content
                        safe_content = sanitize_draw_reply_text(content, allow_task_ids=has_anima_call)
                        if on_text and safe_content:
                            await on_text(safe_content)
                        merged_reply = _merge_intermediate(content)
                        # 逐轮落库：本回复完成，立即回调 matcher 落库（每个回复恰好一次）
                        await _emit_reply(merged_reply)
                        # 回复完成边界：邮箱有新触发消息则批量插入并继续循环（终端阶段内部跳过）
                        if await _insert_mailbox_entries(
                            reply_completed=bool(merged_reply and merged_reply.strip()),
                            round_content=content,
                            round_reasoning=final_reasoning_content or "",
                        ):
                            continue
                        return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                    if _terminal_wrapup_pending:
                        # 收尾轮模型仍返回 tool_calls（不遵守 tool_choice="none"）：不再执行工具，直接以已产出文本收尾
                        final_reasoning_content = message_dict.get("reasoning_content", "")
                        safe_content = sanitize_draw_reply_text(content, allow_task_ids=has_anima_call)
                        if on_text and safe_content:
                            await on_text(safe_content)
                        merged_reply = _merge_intermediate(content)
                        await _emit_reply(merged_reply)
                        # 终端阶段不接收插入，仅记录邮箱残留日志（残留由 matcher 任务收尾时重新处理）
                        await _insert_mailbox_entries(reply_completed=True)
                        return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                    if is_last_round and not _allow_terminal_tools:
                        safe_content = sanitize_draw_reply_text(content, allow_task_ids=has_anima_call)
                        if on_text and safe_content:
                            await on_text(safe_content)
                        if content or intermediate_texts:
                            merged_reply = _merge_intermediate(content)
                            await _emit_reply(merged_reply)
                            if await _insert_mailbox_entries(
                                reply_completed=bool(merged_reply and merged_reply.strip()),
                                round_content=content,
                                round_reasoning=message_dict.get("reasoning_content", "") or "",
                            ):
                                continue
                            return _joined_reply_text(merged_reply), True, tool_messages, final_reasoning_content
                        if not _image_stripped:
                            _image_stripped = True
                            for m in messages:
                                if isinstance(m.get("content"), list):
                                    text_parts = [c.get("text", "") for c in m["content"] if isinstance(c, dict) and c.get("type") == "text"]
                                    m["content"] = "\n".join(text_parts) if text_parts else "[图片已省略]"
                            logger.warning("最后一轮返回空内容，已剥离图片并重试")
                            continue
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
                    # 确保 message_dict 只包含过滤后的 tool_calls（与 _execute_tool_calls 使用的一致）
                    message_dict["tool_calls"] = tool_calls
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
                # 注意：超限提示不能在执行工具前插入 messages —— assistant(tool_calls) 消息之后
                # 必须紧跟对应的 tool 响应消息，中间插入 system 会破坏协议配对，导致上游 400
                # （"assistant message with 'tool_calls' must be followed by tool messages"）。
                _search_limit_hit = False
                if search_tool_calls + current_search_count > MAX_SEARCH_TOOL_CALLS:
                    logger.warning(f"单轮联网搜索工具调用次数超过限制: {search_tool_calls + current_search_count} > {MAX_SEARCH_TOOL_CALLS}")
                    _search_limit_hit = True
                    internal_control_injected = True
                
                # 更新计数器
                total_tool_calls += current_tool_count
                search_tool_calls += current_search_count

                self._current_chat_key = request_chat_key
                self._current_trigger_userid = request_trigger_userid
                await self._execute_tool_calls(messages, tool_calls, plugin_config)
                # 收集tool消息
                for msg in messages:
                    if msg.get("role") == "tool" and msg not in tool_messages:
                        tool_messages.append(msg)

                # 终端阶段只允许单次终端工具调用：本轮以终端模式进入且已执行完毕
                # （含非终端调用全部被过滤的情况），下一轮进入无工具收尾轮（tool_choice="none"）
                if _terminal_this_round:
                    _terminal_wrapup_pending = True

                # 搜索超限提示在工具响应全部追加完成后才插入，
                # 避免隔断 assistant(tool_calls) → tool 响应的连续配对。
                if _search_limit_hit:
                    messages.append({
                        "role": "system",
                        "content": _SEARCH_TOOL_LIMIT_TEXT
                    })

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

                # 工具轮边界：批量插入循环邮箱中的新触发消息（回复未完成版标记；终端阶段内部跳过）
                await _insert_mailbox_entries(reply_completed=False)
            except Exception as e:
                err_text = str(e).lower()
                # keep_reasoning=true 但 provider 不接受 reasoning_content（400）：
                # 剥离全部 reasoning 后重试一次
                if (
                    not _reasoning_stripped
                    and "reasoning" in err_text
                    and ("400" in err_text or "bad request" in err_text)
                ):
                    _reasoning_stripped = True
                    for _m in messages:
                        _m.pop("reasoning_content", None)
                    logger.warning(f"provider 拒绝 reasoning_content，已剥离思考字段并重试: {e!r}")
                    continue
                # provider 不支持 tool_choice（400 且错误文本提及该字段）：
                # 后续轮完全省略 tool_choice 重试一次，退化为纯提示词约束（仅此一次）
                if (
                    not _tool_choice_dropped
                    and round_tool_choice
                    and "tool_choice" in err_text
                    and ("400" in err_text or "bad request" in err_text)
                ):
                    _tool_choice_dropped = True
                    logger.warning(f"provider 拒绝 tool_choice 参数，后续请求省略该字段并重试: {e!r}")
                    continue
                # 工具调用后续轮：图片已无用，任何疑似上下文/token 错误都尝试剥离图片重试
                is_ctx_error = (
                    "context" in err_text
                    or "token" in err_text
                    or "length" in err_text
                    or "too large" in err_text
                    or "request too large" in err_text
                    or "http 400" in err_text
                    or "status code: 400" in err_text
                    or "bad request" in err_text
                )
                if is_ctx_error and round_idx > 0 and not _image_stripped:
                    _image_stripped = True
                    for m in messages:
                        if isinstance(m.get("content"), list):
                            text_parts = [c.get("text", "") for c in m["content"] if isinstance(c, dict) and c.get("type") == "text"]
                            m["content"] = "\n".join(text_parts) if text_parts else "[图片已省略]"
                    logger.warning(f"工具轮请求失败，已剥离图片并重试: {e!r}")
                    continue
                logger.warning(f"LLM 请求失败: {e!r}")
                # key 级失败（鉴权 401/403、额度 402、限流 429 …）：把失败的 key 打进冷却，
                # 并在本次请求内顺延到下一个 key 重试——第一顺位优先，全部试过才放弃。
                # 其他错误（400 参数、超时、上下文超限、图片被拒）与 key 无关，换 key 也救不回来，
                # 以前对任何异常都推进索引，才出现「一次偶发失败就把流量永久钉在第二个 key」。
                if _is_key_level_error(err_text):
                    key_scope = request_state.get("key_scope") or (
                        self._profile_key_id(request_profile) if request_profile else self._SINGLETON_KEY_SCOPE
                    )
                    key_list = list(
                        (request_profile.get("api_keys") if request_profile else self.api_keys) or [""]
                    )
                    if len(key_list) > 1:
                        failed_index = int(request_state.get("api_key_index", 0))
                        self._mark_key_failed(key_scope, failed_index)
                        _tried_key_indices.add(failed_index)
                        next_key, next_index = self._key_plan(key_scope, key_list, _tried_key_indices)
                        if next_index not in _tried_key_indices:
                            _tried_key_indices.add(next_index)
                            request_state = dict(request_state)
                            request_state["api_key"] = next_key
                            request_state["api_key_index"] = next_index
                            logger.warning(
                                f"当前 key 不可用（{err_text[:120]}），本次请求改用第 {next_index + 1}/"
                                f"{len(key_list)} 个 key 重试 | scope: {key_scope}"
                            )
                            continue
                        logger.warning(
                            f"{len(key_list)} 个 key 均不可用，放弃本次请求 | scope: {key_scope}"
                        )
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

    @staticmethod
    def generate_msg_template(sender: str, msg: str, time_str: str = "") -> str:
        return f"{time_str}{sender}: {msg}"

    @staticmethod
    def _cal_text_tokens(text: str, model: str = "gpt-3.5-turbo") -> int:
        """计算纯文本的token数（LRU 缓存：裁剪循环会反复编码同一文本）"""
        cache_key = (model, text)
        cached = _text_token_cache.get(cache_key)
        if cached is not None:
            _text_token_cache.move_to_end(cache_key)
            return cached
        try:
            if model in enc_cache:
                enc = enc_cache[model]
            else:
                enc = encoding_for_model(model)
                enc_cache[model] = enc
            tokens = len(enc.encode(text))
        except Exception:
            tokens = max(1, len(text) // 2)
        _text_token_cache[cache_key] = tokens
        if len(_text_token_cache) > _TEXT_TOKEN_CACHE_MAX:
            _text_token_cache.popitem(last=False)
        return tokens

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
                            total += IMAGE_TOKEN_ESTIMATE
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
