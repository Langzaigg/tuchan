from typing import Any, Dict, List, Tuple

import httpx

from .common import clean_text

MIN_SEARCH_COUNT = 10
MAX_SEARCH_COUNT = 20


def should_load(config) -> bool:
    """有 BOCHA_API_KEY 且 Tavily 不可用时才加载。"""
    if not getattr(config, "BOCHA_API_KEY", None):
        return False
    from . import tavily_search
    if getattr(config, "TAVILY_API_KEY", None) and not tavily_search._tavily_disabled:
        return False
    return True


schema = {
    "type": "function",
    "function": {
        "name": "bocha_search",
        "description": (
            "网页搜索工具。当你对用户的问题不确定、不了解、或涉及实时信息（新闻、天气、股价等）时，应主动使用此工具搜索以给出准确回答。"
            "不要猜测不确定的事实，优先搜索验证。查询词必须简短、宽泛、以定位权威/百科页面为目标。"
            "搜索人物、角色、作品条目时，查询词应只包含核心名称和少量来源/类型限定，用于定位可靠页面；不要把外观、属性或待核对结论拆成一串细节词堆进搜索词。"
            "先找到可靠页面，再用 fetch_url 抓取页面文本核对细节；只有首轮无结果时才换一个别名或语种重搜。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "简短搜索词。人物、角色或作品查询应先定位可靠页面，避免把多个待核对的外观或属性细节直接拼进 query。",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results to return. The tool always returns at least 10 and at most 20 results.",
                    "minimum": MIN_SEARCH_COUNT,
                    "maximum": MAX_SEARCH_COUNT,
                },
            },
            "required": ["query"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    if not config.BOCHA_API_KEY:
        return "博查搜索未配置 BOCHA_API_KEY。", []

    query = str(args.get("query") or "").strip()
    try:
        requested_count = int(args.get("count") or config.BOCHA_SEARCH_COUNT)
    except (TypeError, ValueError):
        requested_count = int(config.BOCHA_SEARCH_COUNT or MIN_SEARCH_COUNT)
    count = max(MIN_SEARCH_COUNT, min(requested_count, MAX_SEARCH_COUNT))
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
