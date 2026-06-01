from typing import Any, Dict, List, Tuple

import httpx

from .common import dict_without_none

schema = {
    "type": "function",
    "function": {
        "name": "pixiv_search",
        "description": "Search one Pixiv image through Lolicon API by concise tags. Use only when the user asks for an image.",
        "parameters": {
            "type": "object",
            "properties": {
                "tag": {
                    "type": "string",
                    "description": "Comma-separated concise tags. Use random for a random image.",
                },
            },
            "required": ["tag"],
        },
    },
}


async def _do_search(tags, r18, pic_proxy, exclude_ai, proxy, timeout) -> dict:
    async with httpx.AsyncClient(proxy=proxy, timeout=timeout) as client:
        resp = await client.post(
            "https://api.lolicon.app/setu/v2",
            json=dict_without_none(
                {
                    "tag": tags,
                    "num": 1,
                    "r18": int(r18),
                    "proxy": pic_proxy,
                    "excludeAI": bool(exclude_ai),
                },
            ),
        )
        resp.raise_for_status()
        return resp.json()


_PIXIV_MIRRORS = [
    "i.pixiv.cat",
    "i.acgmx.com",
    "i.loli.net",
    "i.pixiv.nl",
    "i.pixiv.re",
]


async def _build_display_url(original_url: str) -> str:
    """将 Pixiv 原图 URL 转为可用的缩略图 URL，尝试多个镜像域名。"""
    master_url = (
        original_url
        .replace("img-original", "img-master")
        .replace(".jpg", "_master1200.jpg")
        .replace(".png", "_master1200.jpg")
        .replace(".gif", "_master1200.jpg")
    )
    # 已经是镜像域名，直接返回
    for mirror in _PIXIV_MIRRORS:
        if mirror in master_url:
            return master_url
    # i.pximg.net → 尝试替换为镜像
    async with httpx.AsyncClient(timeout=8, follow_redirects=True) as client:
        for mirror in _PIXIV_MIRRORS:
            candidate = master_url.replace("i.pximg.net", mirror)
            try:
                r = await client.head(candidate, headers={"Referer": "https://www.pixiv.net/"})
                if r.status_code == 200:
                    return candidate
            except Exception:
                continue
    return master_url


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    tag_str = str(args.get("tag") or "").strip()
    tags = None if not tag_str or tag_str.lower() == "random" or "随机" in tag_str else [x.strip() for x in tag_str.split(",") if x.strip()]

    proxy = getattr(config, "TOOL_PROXY", "") or None
    if proxy and not str(proxy).startswith(("http", "socks")):
        proxy = f"http://{proxy}"

    r18 = int(getattr(config, "PIXIV_R18", 0))
    pic_proxy = getattr(config, "PIXIV_PIC_PROXY", "") or None
    exclude_ai = bool(getattr(config, "PIXIV_EXCLUDE_AI", True))

    # 第一次搜索
    data = await _do_search(tags, r18, pic_proxy, exclude_ai, proxy, config.WEB_FETCH_TIMEOUT)
    items = data.get("data") or []

    # 回退逻辑：多关键词搜不到时，只用第一个关键词重试
    if not items and tags and len(tags) > 1:
        fallback_tag = tags[:1]
        data = await _do_search(fallback_tag, r18, pic_proxy, exclude_ai, proxy, config.WEB_FETCH_TIMEOUT)
        items = data.get("data") or []

    if not items:
        return f"没有找到关于 {tag_str or 'random'} 的图片。", []

    pic = items[0]
    urls = pic.get("urls") or {}
    image_url = urls.get("original") or ""
    display_url = await _build_display_url(image_url)
    pid = pic.get("pid")
    author = pic.get("author")
    tags = ", ".join(pic.get("tags") or [])
    content = (
        f"已找到图片。标题: {pic.get('title')}; 作者: {author}; "
        f"标签: {tags}。图片会自动发送给用户。"
    )
    attachment = {
        "type": "image",
        "url": display_url,
        "pid": pid,
        "author": author,
        "gallery_url": f"https://www.pixiv.net/artworks/{pid}" if pid else None,
    }
    return content, [attachment]
