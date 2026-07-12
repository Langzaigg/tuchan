import asyncio
import random
import re
from typing import Any, Dict, List, Tuple

from .common import clean_text, validate_http_url

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
]

POPUP_SELECTORS = [
    'button[aria-label*="accept" i]',
    'button[aria-label*="agree" i]',
    'button[aria-label*="consent" i]',
    'button[aria-label*="cookie" i]',
    'button[aria-label*="close" i]',
    'button[class*="cookie" i]',
    'button[class*="consent" i]',
    'button[id*="cookie" i]',
    'button[id*="consent" i]',
    '[class*="popup"] button',
    '[class*="modal"] button[class*="close"]',
    '[class*="overlay"] button[class*="close"]',
    'button:has-text("Accept")',
    'button:has-text("I agree")',
    'button:has-text("Got it")',
    'button:has-text("OK")',
    'button:has-text("Close")',
]

EXTRACT_JS = """
() => {
    function extractLinks(element) {
        const links = [];
        const anchors = element.querySelectorAll('a[href]');
        anchors.forEach(a => {
            const href = a.href;
            const text = a.textContent.trim();
            if (href && text && !href.startsWith('javascript:') && !href.startsWith('#')) {
                links.push({ text, href });
            }
        });
        return links;
    }

    function getVisibleText(element) {
        const walker = document.createTreeWalker(
            element,
            NodeFilter.SHOW_TEXT,
            {
                acceptNode: function(node) {
                    const style = window.getComputedStyle(node.parentElement);
                    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
                        return NodeFilter.FILTER_REJECT;
                    }
                    return NodeFilter.FILTER_ACCEPT;
                }
            }
        );
        const texts = [];
        while (walker.nextNode()) {
            const text = walker.currentNode.textContent.trim();
            if (text) texts.push(text);
        }
        return texts.join(' ');
    }

    const mainContent = document.querySelector('main, article, [role="main"], .content, .main-content, #content, #main, .md-content');
    const sidebar = document.querySelector(
        '.md-sidebar, .md-sidebar--primary, .md-nav--primary, ' +
        'nav.md-nav, aside, [role="navigation"], .sidebar, .menu, #sidebar, #menu, ' +
        '.toc, .table-of-contents, .md-nav__list'
    );

    const mainText = mainContent ? getVisibleText(mainContent) : getVisibleText(document.body);
    const sidebarText = sidebar ? getVisibleText(sidebar) : '';

    const mainLinks = mainContent ? extractLinks(mainContent) : extractLinks(document.body);
    const sidebarLinks = sidebar ? extractLinks(sidebar) : [];

    const title = document.title || '';
    const headings = Array.from(document.querySelectorAll('h1, h2, h3')).map(h => h.textContent.trim()).filter(t => t);

    return {
        title,
        headings,
        mainText,
        sidebarText,
        mainLinks,
        sidebarLinks,
        url: window.location.href
    };
}
"""

schema = {
    "type": "function",
    "function": {
        "name": "browse_url",
        "description": "Open a web page with a real browser, wait for JavaScript rendering, and return readable text with links. Use when fetch_url fails or the page requires JavaScript rendering. Extracts main content and sidebar navigation links.",
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


async def _try_close_popups(page) -> None:
    for selector in POPUP_SELECTORS:
        try:
            elements = await page.query_selector_all(selector)
            for el in elements[:3]:
                try:
                    await el.click(timeout=1000)
                    await asyncio.sleep(0.3)
                except Exception:
                    pass
        except Exception:
            pass


async def _apply_stealth(page) -> None:
    await page.evaluate("""
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN'] });
            const getParameter = WebGLRenderingContext.prototype.getParameter;
            WebGLRenderingContext.prototype.getParameter = function(parameter) {
                if (parameter === 37445) return 'Intel Inc.';
                if (parameter === 37446) return 'Intel Iris OpenGL Engine';
                return getParameter.call(this, parameter);
            };
        }
    """)


async def _extract_content(page) -> Dict[str, Any]:
    return await page.evaluate(EXTRACT_JS)


def _format_output(data: Dict[str, Any], max_chars: int, offset: int) -> Tuple[str, bool, int]:
    parts = []

    if data.get('title'):
        parts.append(f"# {data['title']}\n")

    if data.get('headings'):
        parts.append("## 页面结构\n" + "\n".join(f"- {h}" for h in data['headings'][:10]) + "\n")

    if data.get('sidebarLinks'):
        seen = set()
        unique_links = []
        for link in data['sidebarLinks']:
            if link['href'] not in seen:
                seen.add(link['href'])
                unique_links.append(link)
        if unique_links:
            parts.append("## 导航链接\n" + "\n".join(
                f"- [{l['text'][:50]}]({l['href']})" for l in unique_links[:30]
            ) + "\n")

    if data.get('mainText'):
        parts.append("## 正文内容\n" + data['mainText'])

    if data.get('mainLinks'):
        seen = set()
        unique_links = []
        for link in data['mainLinks']:
            if link['href'] not in seen:
                seen.add(link['href'])
                unique_links.append(link)
        if unique_links and len(unique_links) <= 50:
            parts.append("\n## 正文链接\n" + "\n".join(
                f"- [{l['text'][:80]}]({l['href']})" for l in unique_links
            ))

    full_text = "\n".join(parts)
    total_len = len(full_text)

    if offset > 0:
        full_text = full_text[offset:]

    if len(full_text) <= max_chars:
        return full_text, False, total_len

    return full_text[:max_chars], True, total_len


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    url = str(args.get("url") or "").strip()
    offset = int(args.get("offset") or 0)
    if not validate_http_url(url):
        return "URL 必须以 http:// 或 https:// 开头。", []

    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        return f"Playwright 不可用: {e!r}", []

    max_chars = config.WEB_FETCH_MAX_CHARS
    timeout_ms = config.PLAYWRIGHT_TIMEOUT * 1000
    proxy = getattr(config, "TOOL_PROXY", "") or None

    max_retries = 3
    last_error = None

    for attempt in range(max_retries):
        try:
            async with async_playwright() as p:
                launch_args = [
                    "--disable-blink-features=AutomationControlled",
                    "--disable-features=IsolateOrigins,site-per-process",
                    "--disable-web-security",
                ]

                browser = await p.chromium.launch(
                    headless=True,
                    args=launch_args,
                    proxy={"server": proxy} if proxy else None,
                )

                context = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1920, "height": 1080},
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )

                page = await context.new_page()
                await _apply_stealth(page)

                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)

                await _try_close_popups(page)

                await asyncio.sleep(random.uniform(0.5, 1.5))

                data = await _extract_content(page)

                await browser.close()

                if not data.get('mainText') and not data.get('sidebarText'):
                    if attempt < max_retries - 1:
                        continue
                    return "页面内容为空，可能被反爬机制阻止。", []

                text, has_more, total_len = _format_output(data, max_chars, offset)
                if has_more:
                    next_offset = offset + len(text)
                    return f"{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={next_offset} 继续读取]", []
                return text, []

        except Exception as e:
            last_error = e
            if attempt < max_retries - 1:
                await asyncio.sleep(random.uniform(1, 3))
                continue

    return f"浏览器抓取失败（已重试{max_retries}次）: {last_error}", []
