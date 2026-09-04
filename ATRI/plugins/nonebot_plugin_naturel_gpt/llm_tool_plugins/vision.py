"""视觉理解工具 - 把纯文本主模型无法直接看到的图片委托给视觉模型理解。

仅在「主模型 multimodal=false 且配置了 model_vision」的会话中由 get_tool_schemas 门控暴露。
工具拿到 [图片N] 占位符对应的真实 URL，复用 image_cache 下载为 data URI，
再用 _request_openai_compatible 调视觉模型，返回纯文本描述给主模型。
等价于参考项目 Qwen-MM-Plugins 的 encode_image_source + call_openai_chat，无新增依赖。
"""

from typing import Any, Dict, List, Tuple

from ..logger import logger

schema = {
    "type": "function",
    "function": {
        "name": "vision",
        "description": (
            "视觉理解工具。当对话上下文中出现 [图片N] 占位符、且需要识别/描述/理解图片内容时调用。"
            "[图片N] 的编号在整个对话上下文中全局唯一（1..N），image_index 对应 [图片N] 的 N。"
            "可访问当前上下文里的任何图片，包括历史消息和非触发上下文里的图。"
            "返回的是对图片的纯文字描述，你应基于描述回答用户，不要再重复调用本工具确认同一张图。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_index": {
                    "type": "integer",
                    "description": "图片序号，对应 [图片N] 的 N（全局唯一，从 1 开始）",
                },
                "prompt": {
                    "type": "string",
                    "description": "你想问这张图的问题，如：图里有什么、图中的文字是什么、整体风格如何",
                },
            },
            "required": ["image_index", "prompt"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    idx = int(args.get("image_index") or 1)
    prompt = str(args.get("prompt") or "").strip() or "详细描述这张图片的内容"

    from ..openai_func import TextGenerator
    tg = TextGenerator.instance

    # 1) 取当前触发消息的图片 URL 列表（ContextVar 快照，由 matcher 在 stream_response 前写入）
    urls = list(tg._current_trigger_images or [])
    if not urls:
        return "当前消息没有可用图片（可能图片已过期或未开启多模态提取）。", []
    if idx < 1 or idx > len(urls):
        return f"图片序号超出范围：当前消息共 {len(urls)} 张图，请检查 [图片N] 的 N。", []
    url = urls[idx - 1]

    # 2) 视觉模型配置快照（由 stream_response 按 request_profile 写入）
    vis = dict(tg._current_vision_config or {})
    if not vis or not vis.get("model"):
        return "视觉模型未配置（model_vision 为空），无法理解图片。", []

    # 3) URL → data URI（复用 image_cache，含 LRU 缓存 / QQ UA+Referer / 坏 URL 记忆）
    # 强制 base64：视觉模型调用链没有 400 回退重试，直传失败无法兜底
    from .. import image_cache
    try:
        resolved = await image_cache.resolve_urls([url], force_base64=True)
    except Exception as e:
        logger.warning(f"[vision] 图片下载异常: {e!r} | {str(url)[:80]}")
        return "图片下载失败，请让用户重新发送。", []
    if not resolved:
        return "图片无法下载（可能已过期），请让用户重新发送。", []
    data_uri = resolved[0]

    # 4) 组装 OpenAI 多模态请求（image_url + text）
    messages = [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": data_uri}},
            {"type": "text", "text": prompt},
        ],
    }]

    # 5) 调视觉模型（复用插件 httpx 直连，非流式）
    request_kwargs: Dict[str, Any] = {
        "model": vis["model"],
        "messages": messages,
        "stream": False,
        "base_url": vis.get("base_url") or "https://api.openai.com/v1",
        "api_key": vis.get("api_key") or "",
        "max_tokens": vis.get("max_tokens", 1024),
        "timeout": vis.get("timeout", 60),
    }
    # 代理仅在 use_socket_proxy 时生效（与主请求 _request_state 逻辑一致）
    if vis.get("proxy") and vis.get("use_socket_proxy"):
        request_kwargs["proxy"] = vis["proxy"]

    try:
        resp = await tg._request_openai_compatible(request_kwargs)
    except Exception as e:
        logger.warning(f"[vision] 视觉模型调用失败: {e!r}")
        return f"视觉模型调用失败：{e!r}。请稍后再试或直接回答用户。", []

    # 6) 提取文本回复
    try:
        choices = resp.get("choices") or []
        message = (choices[0] if choices else {}).get("message") or {}
        content = str(message.get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[vision] 解析视觉模型响应失败: {e!r}")
        return "视觉模型返回格式异常，无法解析图片描述。", []

    if not content:
        return "视觉模型未返回有效描述。", []

    # 7) 统计 token 消耗（按视觉模型名分桶，走非流式 usage）
    try:
        from ..stats import stats
        stats.record_model_usage(vis["model"], resp.get("usage"))
    except Exception:
        pass

    logger.info(f"[vision] image_index={idx} prompt={prompt!r} → {content[:120]}{'...' if len(content) > 120 else ''}")
    return content, []
