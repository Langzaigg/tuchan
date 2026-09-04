"""博查搜索（内部 fallback，不注册独立工具 schema）。

仅由 tavily_search 在 Tavily 不可用/调用失败时直接调用 `run`，
对模型只暴露 tavily_search 一个搜索工具，保持工具集收敛。
"""

from typing import Any, Dict, List, Tuple

import httpx

from .common import clean_text

MIN_SEARCH_COUNT = 10
MAX_SEARCH_COUNT = 20


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
