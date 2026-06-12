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
                "offset": {
                    "type": "integer",
                    "description": "Starting position for reading content. Use 0 for first chunk, then increment by returned offset amount to read more.",
                    "default": 0,
                },
            },
            "required": ["url"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    url = str(args.get("url") or "").strip()
    offset = int(args.get("offset") or 0)
    if not validate_http_url(url):
        return "URL 必须以 http:// 或 https:// 开头。", []
    
    proxy = getattr(config, "TOOL_PROXY", "") or None
    timeout = config.WEB_FETCH_TIMEOUT
    
    # 先尝试使用代理
    if proxy:
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                text, has_more, total_len = clean_text(resp.text, config.WEB_FETCH_MAX_CHARS, offset)
                if has_more:
                    next_offset = offset + len(text)
                    return f"{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={next_offset} 继续读取]", []
                return text, []
        except Exception:
            pass  # 代理失败，回退到直连
    
    # 直连
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            text, has_more, total_len = clean_text(resp.text, config.WEB_FETCH_MAX_CHARS, offset)
            if has_more:
                next_offset = offset + len(text)
                return f"{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={next_offset} 继续读取]", []
            return text, []
    except httpx.ConnectError as e:
        return f"无法连接到目标网站（DNS解析失败或网络不通）: {url}", []
    except httpx.TimeoutException:
        return f"连接超时（超过{timeout}秒），网站可能无法访问: {url}", []
    except httpx.HTTPStatusError as e:
        return f"HTTP 错误 {e.response.status_code}: {e.response.reason_phrase} - {url}", []
    except httpx.HTTPError as e:
        return f"HTTP 请求失败: {e} - {url}", []
    except Exception as e:
        return f"抓取失败: {e} - {url}", []
