"""Tavily Extract - 网页内容爬取工具。

当 fetch_url / browse_url 等浏览器工具无法访问目标页面时（如被反爬、JS 渲染失败等），
使用 Tavily Extract API 通过 Tavily 服务器端爬取页面内容，返回干净的 Markdown 或纯文本。
"""

from typing import Any, Dict, List, Tuple

import httpx

from ..logger import logger


def should_load(config) -> bool:
    """仅当 tavily_search 已配置可用 key 且未被禁用时才加载。"""
    from .tavily_search import _active_api_key, _tavily_disabled
    return bool(_active_api_key) and not _tavily_disabled


_MAX_CONTENT_PER_RESULT = 8000


def _build_schema():
    return {
        "type": "function",
        "function": {
            "name": "tavily_extract",
            "description": (
                "网页内容爬取工具（Tavily 后端）。当 fetch_url / browse_url 因反爬、JS 渲染失败等原因无法获取页面内容时，"
                "使用此工具通过 Tavily 服务器端爬取页面，返回干净的 Markdown 格式内容。"
                "支持同时提取多个 URL，并可根据查询意图对内容块进行重排序。"
                "注意：此工具消耗 Tavily API 额度，仅在浏览器端工具确实无法访问时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要爬取的一个或多个 URL（最多 20 个）。",
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "用户查询意图，用于对提取的内容块进行重排序。"
                            "如果不提供，返回原始页面内容不重排。"
                        ),
                    },
                    "extract_depth": {
                        "type": "string",
                        "enum": ["basic", "advanced"],
                        "description": (
                            "提取深度。advanced 会提取表格、嵌入内容等更多数据，成功率更高但延迟略长；"
                            "basic 更快但内容可能不完整。默认为 advanced。"
                        ),
                        "default": "advanced",
                    },
                    "format": {
                        "type": "string",
                        "enum": ["markdown", "text"],
                        "description": "返回内容的格式。markdown 会保留标题、链接等结构，text 为纯文本。默认 markdown。",
                        "default": "markdown",
                    },
                    "include_images": {
                        "type": "boolean",
                        "description": "是否在结果中包含页面图片 URL 列表。默认 false。",
                        "default": False,
                    },
                },
                "required": ["urls"],
            },
        },
    }


schema = _build_schema()


def _format_results(data: dict, max_chars: int = 12000) -> str:
    results = data.get("results", []) or []
    failed = data.get("failed_results", []) or []

    parts = []

    for r in results:
        url = r.get("url", "")
        raw_content = r.get("raw_content", "")
        images = r.get("images", []) or []

        if raw_content:
            content = raw_content if len(raw_content) <= _MAX_CONTENT_PER_RESULT else raw_content[:_MAX_CONTENT_PER_RESULT] + "\n\n[内容过长，已截断]"
        else:
            content = "(无内容)"

        parts.append(f"## {url}\n\n{content}")

        if images:
            parts.append(f"\n### 图片 ({len(images)} 张)\n" + "\n".join(f"- {img}" for img in images[:20]))
            if len(images) > 20:
                parts.append(f"- ... 还有 {len(images) - 20} 张图片未列出")

        parts.append("")

    if failed:
        parts.append("## 提取失败的 URL\n")
        for f in failed:
            parts.append(f"- {f.get('url', '?')}: {f.get('error', '未知错误')}")
        parts.append("")

    response_time = data.get("response_time")
    if response_time is not None:
        parts.append(f"---\n响应时间: {response_time:.2f}s")

    full = "\n".join(parts).strip()
    if len(full) > max_chars:
        full = full[:max_chars] + "\n\n[整体输出过长，已截断]"
    return full


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    from .tavily_search import _active_api_key

    if not _active_api_key:
        return "Tavily Extract 未配置 API key（TAVILY_API_KEY 为空）。", []

    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    if not urls:
        return "请提供至少一个要爬取的 URL。", []

    # 限制最多 20 个 URL
    if len(urls) > 20:
        urls = urls[:20]

    payload: Dict[str, Any] = {
        "urls": urls,
        "extract_depth": args.get("extract_depth", "advanced"),
        "format": args.get("format", "markdown"),
    }

    query = args.get("query") or ""
    if query:
        payload["query"] = query
        payload["chunks_per_source"] = min(int(args.get("chunks_per_source", 3) or 3), 5)

    if args.get("include_images"):
        payload["include_images"] = True

    headers = {
        "Authorization": f"Bearer {_active_api_key}",
        "Content-Type": "application/json",
    }

    try:
        timeout = getattr(config, "WEB_FETCH_TIMEOUT", 60)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("https://api.tavily.com/extract", json=payload, headers=headers)

        if resp.status_code != 200:
            detail = ""
            try:
                detail = resp.json().get("detail", {}).get("error", "")
            except Exception:
                pass
            logger.warning(f"[tavily_extract] API 返回 {resp.status_code}: {detail}")
            return f"Tavily Extract 请求失败 (HTTP {resp.status_code}): {detail or resp.text[:200]}", []

        data = resp.json()
    except httpx.TimeoutException:
        return "Tavily Extract 请求超时，页面可能过大或服务器响应慢。", []
    except Exception as e:
        logger.warning(f"[tavily_extract] 请求异常: {e!r}")
        return f"Tavily Extract 请求失败: {e!r}", []

    results = data.get("results", []) or []
    failed = data.get("failed_results", []) or []

    if not results and not failed:
        return "未提取到任何页面内容，所有 URL 均未能成功处理。", []

    return _format_results(data, getattr(config, "WEB_FETCH_MAX_CHARS", 12000)), []
