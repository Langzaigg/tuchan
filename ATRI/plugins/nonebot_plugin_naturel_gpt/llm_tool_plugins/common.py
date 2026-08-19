import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import httpx


# 已知的短链域名集合。命中这些 host 时才会发起一次跟随重定向的请求来还原真实链接；
# 其他普通网页不额外发请求，避免每次抓取都多一跳。
SHORT_URL_HOSTS = {
    "xhslink.com", "xhslink.cn",        # 小红书
    "t.cn",                            # 微博
    "url.cn", "url.ms",                # 腾讯系
    "dwz.cn", "dwz1.cn",               # 百度短网址
    "b23.tv",                          # B站
    "v.douyin.com",                    # 抖音
    "v.kuaishou.com",                  # 快手
    "bit.ly", "bitly.com", "tinyurl.com",
    "is.gd", "t.ly", "cutt.ly", "shrtco.com",
}


def clean_text(text: str, limit: int, offset: int = 0) -> Tuple[str, bool, int]:
    """清理HTML文本并返回指定片段。
    
    Args:
        text: 原始HTML文本
        limit: 最大字符数
        offset: 起始偏移量
    
    Returns:
        (清理后的文本片段, 是否还有更多内容, 总长度)
    """
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.I | re.S)
    # 保留 <a> 标签的 href 属性
    text = re.sub(r'<a\s+[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>', r'\2 [\1]', text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    
    total_len = len(text)
    # 应用偏移量
    if offset > 0:
        text = text[offset:]
    
    # 截断到限制长度
    if len(text) <= limit:
        return text, False, total_len
    
    return text[:limit], True, total_len


def dict_without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def validate_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))


def is_short_url(url: str) -> bool:
    """判断 URL 是否命中已知短链域名集合。"""
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    return host in SHORT_URL_HOSTS


# 移动端 UA：社交平台分享链接多来自移动端，移动 UA 成功率明显高于桌面 UA
_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
)


async def resolve_short_url(url: str, config, timeout: Optional[int] = None) -> Tuple[str, bool]:
    """还原短链接。

    仅当 url 命中已知短链域名集合时才发起一次跟随重定向的请求，用 stream 模式只读
    响应头、不下载 body，取最终 URL。返回 (最终URL, 是否还原到不同站点)。
    非短链或任何异常都原样返回 (url, False)，绝不阻塞主抓取流程。
    """
    if not is_short_url(url):
        return url, False

    proxy = getattr(config, "TOOL_PROXY", "") or None
    tmo = timeout or getattr(config, "WEB_FETCH_TIMEOUT", 20)
    headers = {
        "User-Agent": _MOBILE_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9",
    }

    try:
        async with httpx.AsyncClient(
            proxy=proxy, timeout=tmo, follow_redirects=True, headers=headers
        ) as client:
            # stream：只读响应头拿最终 URL，不消耗 body，省流量
            async with client.stream("GET", url) as resp:
                final = str(resp.url)
        try:
            orig_host = (urlsplit(url).hostname or "").lower()
            final_host = (urlsplit(final).hostname or "").lower()
        except Exception:
            orig_host = final_host = ""
        redirected = bool(final) and final_host != orig_host
        return final or url, redirected
    except Exception:
        return url, False
