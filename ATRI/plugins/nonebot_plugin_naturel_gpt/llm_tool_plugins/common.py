import re
from typing import Any, Dict


def clean_text(text: str, limit: int) -> str:
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def dict_without_none(data: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def validate_http_url(url: str) -> bool:
    return url.startswith(("http://", "https://"))
