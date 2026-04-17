from typing import Any, Dict, List, Tuple

import httpx

from .common import clean_text

schema = {
    "type": "function",
    "function": {
        "name": "bocha_search",
        "description": "Search the web ONLY when the user explicitly asks about recent news, real-time events, or facts you are clearly unsure about.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer"},
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
    payload = {"query": query, "count": max(1, min(count, config.BOCHA_SEARCH_COUNT))}
    headers = {"Authorization": f"Bearer {config.BOCHA_API_KEY}", "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=config.WEB_FETCH_TIMEOUT) as client:
        resp = await client.post(config.BOCHA_API_BASE, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    return clean_text(str(data), config.WEB_FETCH_MAX_CHARS), []
