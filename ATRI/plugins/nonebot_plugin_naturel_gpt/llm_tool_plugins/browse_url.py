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
            },
            "required": ["url"],
        },
    },
}


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    url = str(args.get("url") or "").strip()
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
            await page.goto(url, wait_until="networkidle", timeout=config.PLAYWRIGHT_TIMEOUT * 1000)
            text = await page.locator("body").inner_text(timeout=config.PLAYWRIGHT_TIMEOUT * 1000)
        finally:
            await browser.close()
    return clean_text(text, config.WEB_FETCH_MAX_CHARS), []
