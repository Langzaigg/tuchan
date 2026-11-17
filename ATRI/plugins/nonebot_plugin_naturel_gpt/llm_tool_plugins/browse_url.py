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
        "description": "Open a web page and return readable text with links. Resolves short links to their real URLs first, then extracts content via structured parsing for known social platforms or a real headless browser for JS-rendered pages, with lightweight generic cleanup and server-side extraction as fallbacks when the browser is unavailable or blocked.",
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

# 小红书 PC 端链接必须用桌面 UA：移动 UA 访问 /explore/<id>?xsec_token=... 会被
# 风控 302 到 /404/sec_xxx（HTTP 200 的 404 页），拿不到任何笔记数据。
_XHS_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
# 移动端分享页（短链还原后）反过来只认移动 UA，作为桌面 UA 失败后的备选
_XHS_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)
_XHS_STATE_MARKERS = (
    "window.__SETUP_SERVER_STATE__",   # 移动端 SSR
    "window.__INITIAL_STATE__",        # PC 端 SSR
)
# 抓取结果附带/解读的配图上限（与多模态容量上限一致，避免刷屏与 token 爆炸）
_XHS_MAX_IMAGES = 4
# 视频字幕最多带多少字符（字幕是视频笔记里唯一有实质内容的部分，但过长会挤占正文预算）
_XHS_SUBTITLE_MAX_CHARS = 1500
# 字幕语言优先级：source 是原始音轨语言，其余是机翻
_XHS_SUBTITLE_LANGS = ("source", "zh-CN", "en-US")
_XHS_VISION_PROMPT = (
    "以下是从同一篇小红书笔记里抓取的配图（按顺序）。请用中文简洁总结图片的实际内容："
    "主体与场景、画面里的关键文字（如有）、整体风格。逐张一行，不要臆测看不清的细节，"
    "总长度不超过 300 字。"
)

def _is_xhs_url(url: str) -> bool:
    """是否为小红书页面（xiaohongshu.com 域名）。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return "xiaohongshu.com" in host


def _xhs_parse_js_object(html: str, marker: str) -> Optional[dict]:
    """抓 marker 后第一个 JS 对象字面量并解析成 dict。

    小红书把笔记数据嵌在 script 里（SSR），但值是 JS 字面量而非严格 JSON，
    可能输出裸 undefined（非法 JSON）：先把「值位置」的 undefined 规范为 null
    （前导 :/[, 后随 ,}]），避免动到字符串内部，再用 raw_decode 精确解析到
    第一个完整对象；失败时退化为「到最后一个 } 为止」整体 json.loads。
    """
    i = html.find(marker)
    if i < 0:
        return None
    j = html.find("{", i + len(marker))
    if j < 0:
        return None
    segment = html[j:]
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


def _extract_xhs_state_json(html: str) -> Optional[dict]:
    """依次尝试小红书的两种 SSR 全局变量，返回第一个解析成功的 state。

    两种页面结构差别很大，必须分开认：
      * 移动端 `window.__SETUP_SERVER_STATE__`：
        {LAUNCHER_SSR_STORE_PAGE_DATA: {noteData, commentData}}
      * PC 端 `window.__INITIAL_STATE__`：
        {note: {firstNoteId, noteDetailMap: {<noteId>: {note, comments:{list}}}}}
    """
    for marker in _XHS_STATE_MARKERS:
        state = _xhs_parse_js_object(html, marker)
        if state:
            return state
    return None


def _xhs_note_bundle(state: dict) -> Tuple[dict, List[dict]]:
    """从两种 SSR 结构里取出 (note, comments)；取不到 note 返回 ({}, [])。"""
    # 移动端：noteData / commentData 平铺在 LAUNCHER_SSR_STORE_PAGE_DATA 下
    launcher = state.get("LAUNCHER_SSR_STORE_PAGE_DATA") or {}
    note = launcher.get("noteData") or {}
    if isinstance(note, dict) and note:
        comments = (launcher.get("commentData") or {}).get("comments") or []
        return note, comments if isinstance(comments, list) else []

    # PC 端：note.noteDetailMap[<noteId>] = {note, comments:{list,...}}
    root = state.get("note") or {}
    detail_map = root.get("noteDetailMap") or {}
    if not isinstance(detail_map, dict) or not detail_map:
        return {}, []
    entry = detail_map.get(root.get("firstNoteId") or "") or next(iter(detail_map.values()), None)
    if not isinstance(entry, dict):
        return {}, []
    note = entry.get("note") or {}
    comments = (entry.get("comments") or {}).get("list") or []
    return (
        note if isinstance(note, dict) else {},
        comments if isinstance(comments, list) else [],
    )


def _xhs_image_url(img: dict) -> str:
    """取一张图可用的地址。

    PC 端 imageList[].url 常为空串，真实地址在 urlDefault 或 infoList（WB_DFT 最优，
    WB_PRV 是预览图）。移动端一般直接给 url。
    """
    if not isinstance(img, dict):
        return ""
    for key in ("url", "urlDefault", "urlPre"):
        u = img.get(key)
        if isinstance(u, str) and u.startswith("http"):
            return u
    infos = img.get("infoList") or []
    for want in ("WB_DFT", "WB_PRV"):
        for info in infos:
            if not isinstance(info, dict):
                continue
            u = info.get("url")
            if info.get("imageScene") == want and isinstance(u, str) and u.startswith("http"):
                return u
    for info in infos:
        u = (info or {}).get("url") if isinstance(info, dict) else ""
        if isinstance(u, str) and u.startswith("http"):
            return u
    return ""


def _xhs_note_images(note: dict, limit: int) -> List[str]:
    """取笔记前 limit 张配图（去重、按出现顺序）。"""
    urls: List[str] = []
    seen = set()
    for img in (note.get("imageList") or []):
        u = _xhs_image_url(img)
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
        if len(urls) >= limit:
            break
    return urls


def _xhs_video_duration(note: dict) -> str:
    """视频笔记时长（mm:ss）；图文笔记返回空串。"""
    video = note.get("video") or {}
    for raw in (
        (video.get("capa") or {}).get("duration"),
        ((video.get("media") or {}).get("video") or {}).get("duration"),
    ):
        try:
            sec = int(float(raw))
        except Exception:
            continue
        if sec > 0:
            return f"{sec // 60:02d}:{sec % 60:02d}"
    return ""


def _xhs_subtitle_url(note: dict) -> str:
    """取视频笔记的字幕 srt 地址；没有返回空串。

    字幕挂在 `video.mediaV2`（JSON **字符串**，不是对象）的 `video.subtitles` 下：
        {"source": [{"url": ..., "language": "zh-CN"}], "zh-CN": [...], "en-US": [...]}
    `source` 是原始音轨语言（本片即中文），优先级最高；zh-CN / en-US 多为机翻。
    """
    video = note.get("video") or {}
    raw = video.get("mediaV2")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        media = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(media, dict):
        return ""
    subs = (media.get("video") or {}).get("subtitles") or {}
    if not isinstance(subs, dict):
        return ""
    for lang in _XHS_SUBTITLE_LANGS:
        for item in (subs.get(lang) or []):
            u = item.get("url") if isinstance(item, dict) else ""
            if isinstance(u, str) and u.startswith("http"):
                return u
    for items in subs.values():
        for item in (items or []):
            u = item.get("url") if isinstance(item, dict) else ""
            if isinstance(u, str) and u.startswith("http"):
                return u
    return ""


def _srt_to_text(srt: str) -> str:
    """srt → 纯文本：去掉序号与时间轴行，相邻重复行只留一条。"""
    lines_out: List[str] = []
    normalized = (srt or "").replace("\r\n", "\n").replace("\r", "\n")
    for block in re.split(r"\n\s*\n", normalized):
        rows = [r.strip() for r in block.split("\n") if r.strip()]
        rows = [r for r in rows if not r.isdigit() and "-->" not in r]
        if not rows:
            continue
        line = " ".join(rows)
        if not lines_out or lines_out[-1] != line:
            lines_out.append(line)
    return "\n".join(lines_out)


async def _xhs_fetch_subtitles(note: dict, config) -> str:
    """拉视频笔记的字幕并转纯文本；图文笔记 / 无字幕 / 任何失败都返回空串。

    视频笔记的 desc 往往只是一堆话题标签，字幕才是真正的「内容」，所以值得多一次
    请求（超时压到 10 秒）；失败一律静默降级，绝不影响正文与配图抓取。
    """
    if (note.get("type") or "") != "video":
        return ""
    url = _xhs_subtitle_url(note)
    if not url:
        return ""
    proxy = getattr(config, "TOOL_PROXY", "") or None
    try:
        timeout = min(int(getattr(config, "WEB_FETCH_TIMEOUT", 20) or 20), 10)
    except Exception:
        timeout = 10
    try:
        async with httpx.AsyncClient(
            proxy=proxy, timeout=timeout, follow_redirects=True,
            headers={"User-Agent": _XHS_DESKTOP_UA},
        ) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.text:
            return ""
        text = _srt_to_text(resp.text)
    except Exception:
        return ""
    if not text:
        return ""
    if len(text) > _XHS_SUBTITLE_MAX_CHARS:
        text = text[:_XHS_SUBTITLE_MAX_CHARS] + "\n…（字幕过长已截断）"
    return text


def _format_xhs_markdown(note: dict, comments: List[dict], max_comments: int = 6) -> str:
    """把归一化后的小红书 note 格式化为简洁 markdown（标题/作者/互动/描述/标签/评论）。

    配图不在这里输出（由调用方作为附件 + 视觉描述处理），避免长 URL 刷屏。
    """
    title = note.get("title") or ""
    desc = (note.get("desc") or "").strip()
    user = note.get("user") or {}
    nickname = user.get("nickName") or user.get("nickname") or ""
    ntype = note.get("type") or ""
    interact = note.get("interactInfo") or {}

    parts: List[str] = []
    parts.append(f"# {title}" if title else "# (无标题)")

    meta: List[str] = []
    if nickname:
        meta.append(f"作者: {nickname}")
    if ntype:
        meta.append("视频笔记" if ntype == "video" else "图文笔记")
    duration = _xhs_video_duration(note) if ntype == "video" else ""
    if duration:
        meta.append(f"时长: {duration}")
    ip_location = note.get("ipLocation") or ""
    if ip_location:
        meta.append(f"IP 属地: {ip_location}")
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

    tags: List[str] = []
    for tag in (note.get("tagList") or []):
        name = (tag or {}).get("name")
        if name:
            tags.append(f"#{name}")
    if tags:
        parts.append("\n话题: " + " ".join(tags))

    if comments:
        def _like_num(c: dict) -> int:
            try:
                return int(c.get("likeViewCount") or c.get("likeCount") or 0)
            except Exception:
                return 0

        top = sorted(comments, key=_like_num, reverse=True)[:max_comments]
        cparts = ["\n## 热门评论"]
        for c in top:
            if not isinstance(c, dict):
                continue
            cu = c.get("user") or c.get("userInfo") or {}
            cn = cu.get("nickName") or cu.get("nickname") or ""
            loc = c.get("ipLocation") or ""
            content = (c.get("content") or "").strip()
            likes = c.get("likeViewCount") or c.get("likeCount") or "0"
            head = f"- **{cn}**" if cn else "-"
            if loc:
                head += f"({loc})"
            head += f": {content}"
            if likes and str(likes) != "0":
                head += f" ({likes}赞)"
            cparts.append(head)
            for sc_ in (c.get("subComments") or [])[:2]:
                if not isinstance(sc_, dict):
                    continue
                scu = sc_.get("user") or sc_.get("userInfo") or {}
                scn = scu.get("nickName") or scu.get("nickname") or ""
                scontent = (sc_.get("content") or "").strip()
                cparts.append(f"  - {scn}: {scontent}")
        parts.append("\n".join(cparts))

    return "\n\n".join(p for p in parts if p).strip()


async def _xhs_get_html(url: str, ua: str, config) -> Optional[str]:
    """取小红书页面 HTML；被风控重定向到 /404/sec_xxx 时返回 None。"""
    proxy = getattr(config, "TOOL_PROXY", "") or None
    timeout = getattr(config, "WEB_FETCH_TIMEOUT", 20)
    headers = {
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://www.xiaohongshu.com/",
    }
    try:
        async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
        if resp.status_code != 200 or not resp.text:
            return None
        # 风控页是 HTTP 200 的 /404/sec_xxx，只能靠最终 URL 判出来
        if "/404/sec_" in str(resp.url):
            return None
        return resp.text
    except Exception:
        return None


async def _fetch_xhs_note(url: str, config) -> Optional[Tuple[str, List[str]]]:
    """抓小红书笔记，返回 (markdown, 配图URL列表)；失败返回 None。

    UA 顺序很关键（实测 2026-11）：
      * PC 端分享链接 /explore/<id>?xsec_token=... 用移动 UA 必被 302 到 /404/sec_xxx，
        桌面 UA 才能拿到 __INITIAL_STATE__ 里的笔记数据；
      * 短链还原出来的移动分享页反过来只认移动 UA（数据在 __SETUP_SERVER_STATE__）。
    所以桌面优先、移动兜底，任一命中即返回。
    """
    for ua in (_XHS_DESKTOP_UA, _XHS_MOBILE_UA):
        html = await _xhs_get_html(url, ua, config)
        if not html:
            continue
        state = _extract_xhs_state_json(html)
        if not state:
            continue
        note, comments = _xhs_note_bundle(state)
        if not note:
            continue
        md = _format_xhs_markdown(note, comments)
        if not md:
            continue
        # 视频笔记补字幕：desc 常常只是一堆话题标签，字幕才是正文（失败静默跳过）
        subtitles = await _xhs_fetch_subtitles(note, config)
        if subtitles:
            md = f"{md}\n\n## 视频字幕\n{subtitles}"
        return md, _xhs_note_images(note, _XHS_MAX_IMAGES)
    return None


async def _describe_xhs_images(urls: List[str], config) -> str:
    """把笔记配图交给视觉模型转成文字描述；未配置视觉模型/全部失败返回空串。

    主模型是纯文本（profile 配 model_vision 且 multimodal=false）时，这是图片内容
    真正进入上下文的唯一路径。单张图下载失败只跳过该张（等价于「剥离」），全部失败
    返回空串，调用方退化为只给正文+评论。
    """
    if not urls:
        return ""
    from ..logger import logger
    try:
        from ..openai_func import TextGenerator
        from .. import image_cache
        tg = TextGenerator.instance
    except Exception:
        return ""
    if not tg:
        return ""
    vis = dict(getattr(tg, "_current_vision_config", {}) or {})
    if not vis or not vis.get("model"):
        return ""

    try:
        resolved = await image_cache.resolve_urls_keep_order(urls, force_base64=True)
    except Exception as e:
        logger.warning(f"[browse_url] 小红书配图下载失败: {e!r}")
        return ""
    data_uris = [u for u in (resolved or []) if u]
    if not data_uris:
        return ""

    content: List[Dict[str, Any]] = [
        {"type": "image_url", "image_url": {"url": u}} for u in data_uris
    ]
    content.append({"type": "text", "text": _XHS_VISION_PROMPT})
    request_kwargs: Dict[str, Any] = {
        "model": vis["model"],
        "messages": [{"role": "user", "content": content}],
        "stream": False,
        "base_url": vis.get("base_url") or "https://api.openai.com/v1",
        "api_key": vis.get("api_key") or "",
        "max_tokens": vis.get("max_tokens", 1024),
        "timeout": vis.get("timeout", 60),
    }
    if vis.get("proxy") and vis.get("use_socket_proxy"):
        request_kwargs["proxy"] = vis["proxy"]

    try:
        resp = await tg._request_openai_compatible(request_kwargs)
        choices = resp.get("choices") or []
        message = (choices[0] if choices else {}).get("message") or {}
        text = str(message.get("content") or "").strip()
    except Exception as e:
        logger.warning(f"[browse_url] 小红书配图视觉描述失败: {e!r}")
        return ""

    if text:
        try:
            from ..stats import stats
            stats.record_model_usage(vis["model"], resp.get("usage"))
        except Exception:
            pass
        logger.info(f"[browse_url] 小红书配图描述 {len(data_uris)} 张 → {text[:120]}")
    return text


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


async def _fetch_with_tavily_extract(url: str, config) -> Optional[str]:
    """Tavily Extract 服务端爬取（最终兜底）：客户端抓取全部失败时，
    用 Tavily 服务器端代抓页面（反爬/JS 渲染失败场景），返回 Markdown。无 key 或失败返回 None。"""
    from .tavily_search import _active_api_key
    if not _active_api_key:
        return None
    payload = {"urls": [url], "extract_depth": "advanced", "format": "markdown"}
    headers = {"Authorization": f"Bearer {_active_api_key}", "Content-Type": "application/json"}
    try:
        timeout = getattr(config, "WEB_FETCH_TIMEOUT", 60)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("https://api.tavily.com/extract", json=payload, headers=headers)
        if resp.status_code != 200:
            from ..logger import logger
            logger.warning(f"[browse_url] tavily extract 兜底返回 {resp.status_code}")
            return None
        data = resp.json()
    except Exception:
        return None
    results = data.get("results", []) or []
    if not results:
        return None
    content = (results[0].get("raw_content") or "").strip()
    return content or None


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

    # ---- 2. 小红书 SSR 解析分支（纯 HTTP + 双 UA，无需登录态/浏览器） ----
    if _is_xhs_url(resolved_url):
        try:
            fetched = await _fetch_xhs_note(resolved_url, config)
            if fetched:
                md, images = fetched
                # 配图：先尝试视觉模型转文字（真正进上下文），失败就只保留附件；
                # 图片整体失败时退化为「只给正文+评论」，绝不因配图拖垮正文抓取。
                desc = await _describe_xhs_images(images, config) if images else ""
                if desc:
                    md = f"{md}\n\n## 配图内容\n{desc}"
                elif images:
                    md = f"{md}\n\n## 配图\n共 {len(images)} 张，已随本条结果一并提供"
                # into_context：让多模态主模型直接看到图片（请求层按 profile 决定是否注入），
                # 同时照常作为图片消息发出，用户也能看到原图
                attachments = [{"type": "image", "url": u, "into_context": True} for u in images]
                text, has_more, total_len = _truncate_for_output(md, max_chars, offset)
                if has_more:
                    nxt = offset + len(text)
                    return f"{short_banner}{text}\n\n[内容已截断，总长度 {total_len} 字符。使用 offset={nxt} 继续读取]", attachments
                return f"{short_banner}{text}", attachments
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

    # ---- 5. tavily extract 服务端代抓（客户端策略全部失败后的最终兜底，反爬/JS 渲染场景） ----
    try:
        md = await _fetch_with_tavily_extract(resolved_url, config)
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
