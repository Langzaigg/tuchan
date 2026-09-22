"""图片缓存模块 - 将远程图片下载为 base64 data URI，避免 LLM API 无法访问图片 URL

直传优先：公开可访问的 http(s) 图片 URL（非 QQ 私有域、非内网地址）直接原样传给 API，
不下载不缓存，省去每轮请求重传 base64 的开销；provider 拉取失败时由 matcher 标记
`mark_passthrough_failed()` 回退为 base64 下载。QQ 私有域（rkey 时效/Referer 限制）、
内网地址与 file:/// 始终走 base64 下载（API 侧无法访问）。"""

import asyncio
import base64
import io
import ipaddress
import time
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import httpx

from .logger import logger
from .config import config

# 单张图片大小上限（10MB）：缩放后仍超过则跳过（原为下载后直接判超限，现改为先缩放再判）
_MAX_SINGLE_IMAGE_BYTES = 10 * 1024 * 1024
# 缓存总大小上限（200MB）：图片就地保留在上下文中直到过期（统一 1 小时有效期），
# 缓存需覆盖一小时内的图片，被 LRU 挤出后重下载若遇 QQ rkey 过期会拿不到（该图退化为 [图片已过期]）
_MAX_CACHE_TOTAL_BYTES = 200 * 1024 * 1024
# 下载超时（秒）
_DOWNLOAD_TIMEOUT = 15.0

# 超过该体积的图片先缩放再送模型（8MB）：原图 base64 塞进请求体太大会让网关侧
# （Cloudflare Worker）资源超限直接 503，token 也白烧。阈值以下原样送，不动画质。
_IMAGE_DOWNSCALE_THRESHOLD_BYTES = 8 * 1024 * 1024
# 缩放目标：最长边像素 + JPEG 质量（仅超阈值图片会重编码）
_IMAGE_DOWNSCALE_MAX_SIDE = 1536
_IMAGE_DOWNSCALE_JPEG_QUALITY = 85

# 上游（如 DeepSeek）只认这几种图片格式：
# "You have uploaded an unsupported image. ... valid and has one of the following formats: webp, png, jpeg, and gif."
# 其余格式（bmp/tiff/avif/heic/ico…）以及 Content-Type 与实际字节不一致时，统一重编码为 JPEG。
_ACCEPTED_IMAGE_FORMATS = {"JPEG", "PNG", "GIF", "WEBP"}
_PIL_FORMAT_TO_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}

# QQ 系图片域名：带 rkey 时效或要求 Referer，API 侧无法直接拉取，必须 base64
_QQ_IMAGE_DOMAIN_KEYWORDS = ("qpic.cn", "qlogo.cn", "gtimg.cn", "qq.com")

# url -> (data_uri, size_bytes, access_time)
_cache: Dict[str, Tuple[str, int, float]] = {}
_cache_total_bytes: int = 0
# 记录已知下载失败的 URL，避免重复尝试
_known_bad_urls: Set[str] = set()
# 记录 provider 拉取失败的直传 URL，命中后回退为 base64 下载
_passthrough_bad: Set[str] = set()


def _is_data_uri(url: str) -> bool:
    return url.startswith("data:image/")


def _is_direct_passable(url: str) -> bool:
    """判断 URL 是否可直接传给 API（公开可达的 http(s) 图片地址）。

    QQ 私有域与内网/环回地址不可达 API 侧，必须走 base64 下载。"""
    if not url.startswith(("http://", "https://")):
        return False
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if any(kw in host for kw in _QQ_IMAGE_DOMAIN_KEYWORDS):
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # 普通域名视为公开可达
    return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast)


def mark_passthrough_failed(url: str) -> None:
    """标记直传 URL 被 provider 拉取失败，后续 resolve 回退为 base64 下载"""
    if url:
        _passthrough_bad.add(url)


def _evict_lru(needed: int = 0) -> None:
    """LRU 淘汰，直到缓存有足够空间容纳 needed 字节"""
    global _cache_total_bytes
    while _cache and (_cache_total_bytes + needed > _MAX_CACHE_TOTAL_BYTES):
        lru_url = min(_cache, key=lambda k: _cache[k][2])
        _, size, _ = _cache.pop(lru_url)
        _cache_total_bytes -= size


def _mime_from_content_type(ct: str) -> str:
    """按响应头的 content-type 推断 data URI 的 mime（非图片一律按 jpeg）"""
    if not ct.startswith("image/"):
        return "image/jpeg"
    if "png" in ct:
        return "image/png"
    if "webp" in ct:
        return "image/webp"
    if "gif" in ct:
        return "image/gif"
    return "image/jpeg"


def _downscale_image(content: bytes) -> Optional[Tuple[bytes, str]]:
    """把超大图片压成适合送模型的 JPEG，返回 (bytes, mime)；不适用/失败返回 None。

    只做体积优化，不追求无损：长边缩到 `_IMAGE_DOWNSCALE_MAX_SIDE`，再按
    `_IMAGE_DOWNSCALE_JPEG_QUALITY` 重编码（PNG 截图转 JPEG 通常能小一个数量级）。
    原图带透明通道时铺白底，避免 JPEG 把透明区变成黑块。
    任何异常（格式不支持、PIL 缺失）都返回 None，由调用方按原图继续——绝不因为缩放失败丢图。
    """
    try:
        from PIL import Image
    except Exception as e:
        logger.warning(f"[图片缓存] 无法导入 PIL，跳过缩放，按原图发送: {e!r}")
        return None
    try:
        with Image.open(io.BytesIO(content)) as im:
            im.load()
            width, height = im.size
            scale = _IMAGE_DOWNSCALE_MAX_SIDE / float(max(width, height))
            if scale < 1.0:
                im = im.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.LANCZOS,
                )
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                canvas = Image.new("RGB", im.size, (255, 255, 255))
                canvas.paste(im, mask=im.split()[-1])
                im = canvas
            else:
                im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=_IMAGE_DOWNSCALE_JPEG_QUALITY, optimize=True)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        logger.warning(f"[图片缓存] 图片缩放失败，按原图发送: {e!r}")
        return None


# 第三方上传接口对单个 multipart 字段的体积上限（实测 AnimeTrace / FastAPI 为 1024KB，
# 报错 "Part exceeded maximum size of 1024KB."）。base64 长度按此预算卡，留出余量。
_UPLOAD_PART_LIMIT_BYTES = 1024 * 1024
_UPLOAD_BASE64_BUDGET_BYTES = 900 * 1024
# 压缩逐级降档：(长边上限, JPEG 质量)，先降质量保尺寸，压不下去再缩图
_UPLOAD_SHRINK_STEPS = (
    (1536, 85), (1536, 75), (1280, 75), (1280, 65),
    (1024, 70), (1024, 60), (896, 60), (768, 55), (640, 50), (512, 45),
)


def shrink_data_uri(data_uri: str, budget_bytes: int = _UPLOAD_BASE64_BUDGET_BYTES) -> Optional[str]:
    """把 data URI 压到 base64 长度 ≤ budget_bytes，返回新的 data URI；已达标则原样返回。

    用于把图片提交给有单字段体积上限的第三方接口（AnimeTrace 的 multipart 字段限 1024KB）。
    base64 会让体积涨约 1/3，所以一张 800KB 的图提交时必然超限。
    逐档降长边 + 降 JPEG 质量直到达标；透明通道铺白底（JPEG 不支持透明）。
    压不下去 / PIL 不可用 / 不是 data URI 时返回 None，由调用方自行决定是否原样提交。
    """
    if not data_uri or not data_uri.startswith("data:image/"):
        return None
    _, _, b64 = data_uri.partition(",")
    if not b64 or len(b64) <= budget_bytes:
        return data_uri
    try:
        from PIL import Image
    except Exception as e:
        logger.warning(f"[图片缓存] 无法导入 PIL，跳过上传前压缩: {e!r}")
        return None
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        logger.warning(f"[图片缓存] 上传前压缩：base64 解码失败: {e!r}")
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            base = im.convert("RGBA") if im.mode in ("RGBA", "LA", "P") else im.convert("RGB")
            for max_side, quality in _UPLOAD_SHRINK_STEPS:
                width, height = base.size
                scale = max_side / float(max(width, height))
                if scale < 1.0:
                    work = base.resize(
                        (max(1, int(width * scale)), max(1, int(height * scale))),
                        Image.LANCZOS,
                    )
                else:
                    work = base
                if work.mode == "RGBA":
                    canvas = Image.new("RGB", work.size, (255, 255, 255))
                    canvas.paste(work, mask=work.split()[-1])
                    work = canvas
                buf = io.BytesIO()
                work.save(buf, format="JPEG", quality=quality, optimize=True)
                out_b64 = base64.b64encode(buf.getvalue()).decode("ascii")
                if len(out_b64) <= budget_bytes:
                    logger.info(
                        f"[图片缓存] 上传前压缩 {len(b64) / 1024:.0f}KB → {len(out_b64) / 1024:.0f}KB base64"
                        f"（长边 ≤{max_side} / q{quality}）"
                    )
                    return f"data:image/jpeg;base64,{out_b64}"
        logger.warning(f"[图片缓存] 上传前压缩到极限仍超预算（{len(b64) / 1024:.0f}KB base64）")
        return None
    except Exception as e:
        logger.warning(f"[图片缓存] 上传前压缩失败: {e!r}")
        return None


def _sniff_image_format(content: bytes) -> Optional[str]:
    """用 PIL 嗅探图片真实格式（不信任 Content-Type）；识别不出返回 None。"""
    try:
        from PIL import Image
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(content)) as im:
            return im.format
    except Exception:
        return None


def _normalize_image_for_api(content: bytes) -> Optional[Tuple[bytes, str]]:
    """把图片整理成「体积合适 + 上游认识的格式」，返回 (bytes, mime)；识别不了返回 None。

    - 真实格式属于 `_ACCEPTED_IMAGE_FORMATS` 且未超阈值 → 原样返回，但 mime 按**真实格式**修正。
      QQ 图片下载的 Content-Type 常是 `application/octet-stream`，旧实现一律标成 `image/jpeg`，
      字节与声明的类型对不上就会被上游整条请求拒掉（实测 DeepSeek：
      "You have uploaded an unsupported image … webp, png, jpeg, and gif"）。
    - 格式不被接受（bmp/tiff/avif/heic/ico…）或体积超阈值 → 重编码为 JPEG（透明铺白底，超尺寸先缩放）。
    - 连 PIL 都打不开（损坏/非图片）→ 返回 None，调用方跳过这张图：
      宁可少一张图，也不让整个请求 400 挂掉。
    """
    fmt = _sniff_image_format(content)
    if fmt is None:
        return None
    if fmt in _ACCEPTED_IMAGE_FORMATS and len(content) <= _IMAGE_DOWNSCALE_THRESHOLD_BYTES:
        return content, _PIL_FORMAT_TO_MIME[fmt]
    scaled = _downscale_image(content)
    if scaled:
        return scaled
    if fmt in _ACCEPTED_IMAGE_FORMATS:  # 重编码失败但格式本身可用 → 退回原图 + 真实 mime
        return content, _PIL_FORMAT_TO_MIME[fmt]
    return None


async def _download_as_data_uri(url: str) -> Optional[str]:
    """下载远程图片并转为 data URI；超大图/非主流格式先规范化再转"""
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # QQ 图片域名需要 Referer
        if "qpic.cn" in url or "qq.com" in url:
            headers["Referer"] = "https://im.qq.com/"
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True, headers=headers) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            raw = resp.content
            declared_mime = _mime_from_content_type(resp.headers.get("content-type", ""))
            normalized = _normalize_image_for_api(raw)
            if normalized is None:
                logger.warning(f"[图片缓存] 图片无法识别或格式不受支持，跳过（声明类型 {declared_mime}）: {url[:80]}")
                return None
            content, mime = normalized
            if content is not raw:
                logger.info(
                    f"[图片缓存] 图片已规范化 {len(raw) / 1024:.0f}KB → {len(content) / 1024:.0f}KB（{mime}）: {url[:80]}"
                )
            if len(content) > _MAX_SINGLE_IMAGE_BYTES:
                logger.warning(f"[图片缓存] 图片过大 ({len(content)} bytes)，跳过: {url[:80]}")
                return None
            b64 = base64.b64encode(content).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except Exception as e:
        # QQ 多媒体域名（multimedia.nt.qq.com.cn 等）的 URL 带有时效性 rkey，
        # 过期后 400 是预期行为，降级为 DEBUG 避免刷屏
        is_qq_media = "multimedia.nt.qq.com.cn" in url or "multimedia.qq.com" in url
        if is_qq_media:
            _known_bad_urls.add(url)
            logger.debug(f"[图片缓存] QQ多媒体URL下载失败(预期): {e} | {url[:80]}")
        else:
            logger.warning(f"[图片缓存] 下载失败: {e} | {url[:80]}")
        return None


async def resolve_url(url: str, force_base64: bool = False) -> str:
    """将图片 URL 解析为可提交 API 的形式。

    已是 data URI 或缓存命中时直接返回；公开 http(s) URL 默认直传（不下载），
    除非 force_base64 或该 URL 已被标记直传失败；下载失败返回空字符串。"""
    global _cache_total_bytes
    if not url or _is_data_uri(url):
        return url

    # 直传优先：公开 URL 原样传给 API，省下载与 base64 重传开销
    if not force_base64 and url not in _passthrough_bad and _is_direct_passable(url):
        return url

    cached = _cache.get(url)
    if cached:
        _cache[url] = (cached[0], cached[1], time.time())
        return cached[0]

    # 已知下载失败的 URL 直接跳过
    if url in _known_bad_urls:
        return ""

    data_uri = await _download_as_data_uri(url)
    if not data_uri:
        return ""  # 下载失败，返回空字符串供调用方过滤

    size = len(data_uri.encode("utf-8"))
    _evict_lru(size)
    _cache[url] = (data_uri, size, time.time())
    _cache_total_bytes += size

    if config.DEBUG_LEVEL > 0:
        logger.debug(f"[图片缓存] 已缓存 {size} bytes, 总计 {_cache_total_bytes} bytes: {url[:80]}")
    return data_uri


async def resolve_urls(urls: List[str], force_base64: bool = False) -> List[str]:
    """批量解析图片 URL，返回可提交 API 的列表（data URI 或直传 URL）。过滤失败项和重复项。"""
    if not urls:
        return []
    # 去重，保持顺序
    seen: Set[str] = set()
    unique_urls: List[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            unique_urls.append(u)
    tasks = [resolve_url(u, force_base64=force_base64) for u in unique_urls]
    results = await asyncio.gather(*tasks)
    return [r for r in results if r]


async def resolve_urls_keep_order(urls: List[str], force_base64: bool = False) -> List[str]:
    """按输入顺序逐项解析，失败项为空字符串（不过滤、不去重），供调用方按位置对齐；同一 URL 只解析一次。"""
    if not urls:
        return []
    unique_urls = list(dict.fromkeys(u for u in urls if u))
    results = await asyncio.gather(*(resolve_url(u, force_base64=force_base64) for u in unique_urls))
    mapping = dict(zip(unique_urls, results))
    return [(mapping.get(u) or "") if u else "" for u in urls]


def collect_active_urls(messages: List) -> Set[str]:
    """从 prompt_messages 中收集所有仍在上下文中的图片 URL"""
    from .persistent_data_manager import ChatMessageData
    active: Set[str] = set()
    for item in messages:
        if isinstance(item, ChatMessageData) and item.images:
            for url in item.images:
                if url and not _is_data_uri(url):
                    active.add(url)
    return active


def purge_stale(active_urls: Set[str]) -> None:
    """清除不在活跃集合中的缓存条目和已知坏 URL 记录"""
    global _cache_total_bytes
    stale = [u for u in _cache if u not in active_urls]
    for u in stale:
        _, size, _ = _cache.pop(u)
        _cache_total_bytes -= size
    stale_bad = [u for u in _known_bad_urls if u not in active_urls]
    for u in stale_bad:
        _known_bad_urls.discard(u)
    stale_pt = [u for u in _passthrough_bad if u not in active_urls]
    for u in stale_pt:
        _passthrough_bad.discard(u)
    if stale:
        logger.info(f"[图片缓存] 清除 {len(stale)} 条过期缓存, 剩余 {_cache_total_bytes} bytes")
    if stale_bad:
        logger.debug(f"[图片缓存] 清除 {len(stale_bad)} 条过期坏URL记录")
    if stale_pt:
        logger.debug(f"[图片缓存] 清除 {len(stale_pt)} 条过期直传失败记录")
