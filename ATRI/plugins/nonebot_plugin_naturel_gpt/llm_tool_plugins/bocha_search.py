from typing import Any, Dict, List, Tuple

import httpx

from .common import clean_text


def should_load(config) -> bool:
    """有非空 BOCHA_API_KEY 时才加载。"""
    return bool(getattr(config, "BOCHA_API_KEY", None))


schema = {
    "type": "function",
    "function": {
        "name": "bocha_search",
        "description": "网页搜索工具。当你对用户的问题不确定、不了解、或涉及实时信息（新闻、天气、股价等）时，应主动使用此工具搜索以给出准确回答。不要猜测不确定的事实，优先搜索验证。",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "description": "Number of results to return (1-15)"},
            },
            "required": ["query"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    if not config.BOCHA_API_KEY:
        return "博查搜索未配置 BOCHA_API_KEY。", []

    query = str(args.get("query") or "").strip()
    count = int(args.get("count") or config.BOCHA_SEARCH_COUNT)
    count = max(1, min(count, config.BOCHA_SEARCH_COUNT))
    payload = {"query": query, "count": count}
    headers = {"Authorization": f"Bearer {config.BOCHA_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=config.WEB_FETCH_TIMEOUT) as client:
        resp = await client.post(config.BOCHA_API_BASE, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    # 提取摘要格式返回
    web_pages = []
    try:
        web_pages = data.get("data", {}).get("webPages", {}).get("value", [])
    except (AttributeError, TypeError):
        pass

    if not web_pages:
        return f"搜索「{query}」未找到相关结果。", []

    # 格式化返回摘要
    results = []
    for i, page in enumerate(web_pages[:count], 1):
        title = page.get("name", "未知标题")
        url = page.get("url", "")
        snippet = page.get("snippet", "无摘要")
        results.append(f"{i}. {title}\n   {snippet}\n   {url}")

    summary = f"搜索「{query}」找到 {len(results)} 条结果：\n\n" + "\n\n".join(results)
    return summary, []
