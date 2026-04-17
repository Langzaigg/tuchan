from typing import Any, Dict, List, Tuple

import httpx

from .common import clean_text, validate_http_url

schema = {
    "type": "function",
    "function": {
        "name": "fetch_url",
        "description": "Fetch a web page through a normal HTTP client and return readable text.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
            },
            "required": ["url"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    url = str(args.get("url") or "").strip()
    if not validate_http_url(url):
        return "URL 必须以 http:// 或 https:// 开头。", []
    async with httpx.AsyncClient(timeout=config.WEB_FETCH_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return clean_text(resp.text, config.WEB_FETCH_MAX_CHARS), []
