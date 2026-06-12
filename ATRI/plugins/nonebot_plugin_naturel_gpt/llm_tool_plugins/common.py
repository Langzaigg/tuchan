import re
from typing import Any, Dict, Tuple


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
