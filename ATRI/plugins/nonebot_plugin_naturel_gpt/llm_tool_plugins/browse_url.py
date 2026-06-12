from typing import Any, Dict, List, Tuple

from .common import clean_text, validate_http_url

schema = {
    "type": "function",
    "function": {
        "name": "browse_url",
        "description": "ONLY use when fetch_url fails or the page requires JavaScript rendering. Open a page with Playwright, wait for browser rendering, and return visible text.",
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
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return f"Playwright 不可用: {e!r}", []

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=config.PLAYWRIGHT_TIMEOUT * 1000)
            text = await page.locator("body").inner_text(timeout=config.PLAYWRIGHT_TIMEOUT * 1000)
        except Exception as e:
            # 超时或其他错误时，尝试获取已加载的内容
            try:
                text = await page.locator("body").inner_text(timeout=5000)
            except:
                raise e
        finally:
            await browser.close()
    
    text, has_more, total_len = clean_text(text, config.WEB_FETCH_MAX_CHARS, offset)
    if has_more:
        next_offset = offset + len(text)
        return f"{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={next_offset} 继续读取]", []
    return text, []
