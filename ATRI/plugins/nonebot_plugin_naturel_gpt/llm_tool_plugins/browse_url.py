import asyncio
import json
import random
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import httpx

from .common import is_short_url, resolve_short_url, validate_http_url

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
        "description": "Open a web page and return readable text with links. Resolves short links to their real URLs first, then extracts content via structured parsing for known social platforms or a real headless browser for JS-rendered pages (lightweight generic fallback when the browser is unavailable).",
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


# ---------- 短链还原 / 社交平台 SSR 解析 / 通用清洗 ----------

def _is_xhs_url(url: str) -> bool:
    """是否为小红书页面（xiaohongshu.com 域名）。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return "xiaohongshu.com" in host


def _extract_xhs_state_json(html: str) -> Optional[dict]:
    """从小红书页面 HTML 提取 window.__SETUP_SERVER_STATE__ 的 JSON 对象。

    小红书把笔记数据嵌在该 script 里（SSR）。注意它可能输出裸 undefined（非法 JSON），
    先把「值位置」的 undefined 规范为 null，再用 raw_decode 精确解析到第一个完整对象。
    """
    marker = "window.__SETUP_SERVER_STATE__"
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i)
    if j < 0:
        return None
    segment = html[j:]
    # 仅替换 JSON 值位置的 undefined（前导 :/[, 后随 ,}]），避免动到字符串内部
    segment = re.sub(r'(:|,|\[)\s*undefined(?=\s*[,}\]])', r'\1 null', segment)
    try:
        obj, _end = json.JSONDecoder().raw_decode(segment)
        return obj if isinstance(obj, dict) else None
    except Exception:
        k = segment.rfind("}")
        if k > 0:
            try:
                obj = json.loads(segment[: k + 1])
                return obj if isinstance(obj, dict) else None
            except Exception:
                return None
        return None


def _format_xhs_markdown(state: dict, max_comments: int = 6) -> str:
    """把小红书 SSR state 格式化为简洁 markdown（标题/作者/互动/描述/图片/热门评论）。"""
    launcher = state.get("LAUNCHER_SSR_STORE_PAGE_DATA") or {}
    note = launcher.get("noteData") or {}

    title = note.get("title") or ""
    desc = (note.get("desc") or "").strip()
    user = note.get("user") or {}
    nickname = user.get("nickName") or ""
    ntype = note.get("type") or ""
    interact = note.get("interactInfo") or {}

    parts: List[str] = []
    parts.append(f"# {title}" if title else "# (无标题)")

    meta: List[str] = []
    if nickname:
        meta.append(f"作者: {nickname}")
    if ntype:
        meta.append("视频笔记" if ntype == "video" else "图文笔记")
    stat: List[str] = []
    if interact.get("likedCount"):
        stat.append(f"赞{interact.get('likedCount')}")
    if interact.get("collectedCount"):
        stat.append(f"藏{interact.get('collectedCount')}")
    if interact.get("commentCount"):
        stat.append(f"评{interact.get('commentCount')}")
    if interact.get("shareCount"):
        stat.append(f"转{interact.get('shareCount')}")
    if stat:
        meta.append(" ".join(stat))
    if meta:
        parts.append("\n".join(f"- {m}" for m in meta))
    if desc:
        parts.append(f"\n{desc}")

    # 图片：优先 imageList[].url，否则取 infoList 第一条
    images: List[str] = []
    for img in (note.get("imageList") or []):
        u = img.get("url") or ""
        if not u:
            for info in (img.get("infoList") or []):
                if info.get("url"):
                    u = info.get("url")
                    break
        if u:
            images.append(u)
    if images:
        parts.append("\n## 图片\n" + "\n".join(f"![]({u})" for u in images[:9]))
        if len(images) > 9:
            parts.append(f"_...还有 {len(images) - 9} 张_")

    comment_data = launcher.get("commentData") or {}
    comments = comment_data.get("comments") or []
    if comments:
        def _like_num(c: dict) -> int:
            try:
                return int(c.get("likeViewCount") or 0)
            except Exception:
                return 0

        top = sorted(comments, key=_like_num, reverse=True)[:max_comments]
        cparts = ["\n## 热门评论"]
        for c in top:
            cu = c.get("user") or {}
            cn = cu.get("nickname") or ""
            loc = c.get("ipLocation") or ""
            content = (c.get("content") or "").strip()
            likes = c.get("likeViewCount") or "0"
            head = f"- **{cn}**"
            if loc:
                head += f"({loc})"
            head += f": {content}"
            if likes and str(likes) != "0":
                head += f" ({likes}赞)"
            cparts.append(head)
            for sc_ in (c.get("subComments") or [])[:2]:
                scn = (sc_.get("user") or {}).get("nickname") or ""
                scontent = (sc_.get("content") or "").strip()
                cparts.append(f"  - {scn}: {scontent}")
        parts.append("\n".join(cparts))

    return "\n\n".join(p for p in parts if p).strip()


async def _fetch_xhs_note(url: str, config) -> Optional[str]:
    """纯 HTTP + 移动 UA 抓小红书笔记页，解析 SSR JSON 返回 markdown。失败返回 None。"""
    proxy = getattr(config, "TOOL_PROXY", "") or None
    timeout = getattr(config, "WEB_FETCH_TIMEOUT", 20)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.text:
            return None
        html = resp.text
    except Exception:
        return None
    state = _extract_xhs_state_json(html)
    if not state:
        return None
    return _format_xhs_markdown(state) or None


async def _fetch_with_trafilatura(url: str, config) -> Optional[str]:
    """通用网页正文提取（trafilatura，软依赖）。未安装或失败返回 None。"""
    try:
        import trafilatura  # noqa: F401  软依赖
    except Exception:
        return None
    proxy = getattr(config, "TOOL_PROXY", "") or None
    timeout = getattr(config, "WEB_FETCH_TIMEOUT", 20)
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.text:
            return None
        downloaded = resp.text
    except Exception:
        return None
    try:
        text = trafilatura.extract(
            downloaded,
            output_format="markdown",
            include_comments=False,
            include_tables=True,
            include_links=True,
            favor_recall=True,
        )
    except Exception:
        return None
    return text or None


def _truncate_for_output(text: str, max_chars: int, offset: int) -> Tuple[str, bool, int]:
    """对纯文本按 max_chars/offset 截断，返回 (片段, 是否还有更多, 总长度)。"""
    total = len(text)
    if offset > 0:
        text = text[offset:]
    if len(text) <= max_chars:
        return text, False, total
    return text[:max_chars], True, total


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

    max_chars = config.WEB_FETCH_MAX_CHARS

    # ---- 1. 短链还原（仅命中已知短链域名才发请求，stream 不下载 body） ----
    resolved_url = url
    short_banner = ""
    if is_short_url(url):
        try:
            final, redirected = await resolve_short_url(url, config)
            if redirected and final:
                resolved_url = final
                short_banner = f"[短链已还原]\n原始: {url}\n真实链接: {resolved_url}\n\n"
        except Exception:
            pass

    # ---- 2. 小红书 SSR 解析分支（纯 HTTP + 移动 UA，无需登录态/浏览器） ----
    if _is_xhs_url(resolved_url):
        try:
            md = await _fetch_xhs_note(resolved_url, config)
            if md:
                text, has_more, total_len = _truncate_for_output(md, max_chars, offset)
                if has_more:
                    nxt = offset + len(text)
                    return f"{short_banner}{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={nxt} 继续读取]", []
                return f"{short_banner}{text}", []
        except Exception:
            pass  # 落到通用兜底

    # ---- 3. playwright 真实浏览器渲染（核心：保正文+导航链接+JS 渲染） ----
    async_playwright = None
    pw_import_err: Optional[Exception] = None
    try:
        from playwright.async_api import async_playwright
    except Exception as e:
        pw_import_err = e

    timeout_ms = config.PLAYWRIGHT_TIMEOUT * 1000
    proxy = getattr(config, "TOOL_PROXY", "") or None
    max_retries = 3
    last_error: Optional[Exception] = None

    if async_playwright is not None:
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

                    await page.goto(resolved_url, wait_until="networkidle", timeout=timeout_ms)

                    await _try_close_popups(page)

                    await asyncio.sleep(random.uniform(0.5, 1.5))

                    data = await _extract_content(page)

                    await browser.close()

                    if not data.get('mainText') and not data.get('sidebarText'):
                        if attempt < max_retries - 1:
                            continue
                        break  # 内容空，跳出进 trafilatura 兜底

                    text, has_more, total_len = _format_output(data, max_chars, offset)
                    if has_more:
                        next_offset = offset + len(text)
                        return f"{short_banner}{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={next_offset} 继续读取]", []
                    return f"{short_banner}{text}", []

            except Exception as e:
                last_error = e
                if attempt < max_retries - 1:
                    await asyncio.sleep(random.uniform(1, 3))
                    continue

    # ---- 4. trafilatura 通用清洗兜底（playwright 不可用 / 失败 / 内容空时） ----
    try:
        md = await _fetch_with_trafilatura(resolved_url, config)
        if md:
            text, has_more, total_len = _truncate_for_output(md, max_chars, offset)
            if has_more:
                nxt = offset + len(text)
                return f"{short_banner}{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={nxt} 继续读取]", []
            return f"{short_banner}{text}", []
    except Exception:
        pass

    if pw_import_err is not None:
        return f"{short_banner}Playwright 不可用且 trafilatura 也未提取到内容: {pw_import_err!r}", []
    return f"{short_banner}浏览器抓取失败（已重试{max_retries}次）且 trafilatura 未提取到内容: {last_error}", []
