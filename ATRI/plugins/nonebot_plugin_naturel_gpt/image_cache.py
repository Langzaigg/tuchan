"""图片缓存模块 - 将远程图片下载为 base64 data URI，避免 LLM API 无法访问图片 URL"""

import asyncio
import base64
import time
from typing import Dict, List, Optional, Set, Tuple

import httpx

from .logger import logger
from .config import config

# 单张图片大小上限（10MB）
_MAX_SINGLE_IMAGE_BYTES = 10 * 1024 * 1024
# 缓存总大小上限（50MB）
_MAX_CACHE_TOTAL_BYTES = 50 * 1024 * 1024
# 下载超时（秒）
_DOWNLOAD_TIMEOUT = 15.0

# url -> (data_uri, size_bytes, access_time)
_cache: Dict[str, Tuple[str, int, float]] = {}
_cache_total_bytes: int = 0


def _is_data_uri(url: str) -> bool:
    return url.startswith("data:image/")


def _evict_lru(needed: int = 0) -> None:
    """LRU 淘汰，直到缓存有足够空间容纳 needed 字节"""
    global _cache_total_bytes
    while _cache and (_cache_total_bytes + needed > _MAX_CACHE_TOTAL_BYTES):
        lru_url = min(_cache, key=lambda k: _cache[k][2])
        _, size, _ = _cache.pop(lru_url)
        _cache_total_bytes -= size


async def _download_as_data_uri(url: str) -> Optional[str]:
    """下载远程图片并转为 data URI"""
    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            content = resp.content
            if len(content) > _MAX_SINGLE_IMAGE_BYTES:
                logger.warning(f"[图片缓存] 图片过大 ({len(content)} bytes)，跳过: {url[:80]}")
                return None
            ct = resp.headers.get("content-type", "")
            if "png" in ct:
                mime = "image/png"
            elif "webp" in ct:
                mime = "image/webp"
            elif "gif" in ct:
                mime = "image/gif"
            else:
                mime = "image/jpeg"
            b64 = base64.b64encode(content).decode("ascii")
            return f"data:{mime};base64,{b64}"
    except Exception as e:
        logger.warning(f"[图片缓存] 下载失败: {e} | {url[:80]}")
        return None


async def resolve_url(url: str) -> str:
    """将图片 URL 解析为 data URI。已是 data URI 或缓存命中时直接返回。"""
    global _cache_total_bytes
    if not url or _is_data_uri(url):
        return url

    cached = _cache.get(url)
    if cached:
        _cache[url] = (cached[0], cached[1], time.time())
        return cached[0]

    data_uri = await _download_as_data_uri(url)
    if not data_uri:
        return url  # 下载失败，回退到原始 URL

    size = len(data_uri.encode("utf-8"))
    _evict_lru(size)
    _cache[url] = (data_uri, size, time.time())
    _cache_total_bytes += size

    if config.DEBUG_LEVEL > 0:
        logger.debug(f"[图片缓存] 已缓存 {size} bytes, 总计 {_cache_total_bytes} bytes: {url[:80]}")
    return data_uri


async def resolve_urls(urls: List[str]) -> List[str]:
    """批量解析图片 URL，返回 data URI 列表"""
    if not urls:
        return []
    tasks = [resolve_url(u) for u in urls]
    return await asyncio.gather(*tasks)


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
    """清除不在活跃集合中的缓存条目"""
    global _cache_total_bytes
    stale = [u for u in _cache if u not in active_urls]
    for u in stale:
        _, size, _ = _cache.pop(u)
        _cache_total_bytes -= size
    if stale:
        logger.info(f"[图片缓存] 清除 {len(stale)} 条过期缓存, 剩余 {_cache_total_bytes} bytes")
