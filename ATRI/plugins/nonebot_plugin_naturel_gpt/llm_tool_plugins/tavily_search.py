from typing import Any, Dict, List, Tuple

import httpx

from ..logger import logger

_tavily_disabled = False
_active_api_key: str = ""


def should_load(config) -> bool:
    return bool(_active_api_key) and not _tavily_disabled


def init(config) -> None:
    """启动时检查所有 key 的额度，选用剩余最多的那个。"""
    global _active_api_key
    keys = getattr(config, "TAVILY_API_KEY", []) or []
    if not keys:
        return

    if len(keys) == 1:
        _active_api_key = keys[0]
        logger.info("[tavily_search] 仅一个 key，直接使用")
        return

    best_key = ""
    best_remaining = -1
    timeout = getattr(config, "WEB_FETCH_TIMEOUT", 10)

    for key in keys:
        try:
            resp = httpx.get(
                "https://api.tavily.com/usage",
                headers={"Authorization": f"Bearer {key}"},
                timeout=timeout,
            )
            if resp.status_code != 200:
                logger.warning(f"[tavily_search] key ...{key[-6:]} usage 查询失败: HTTP {resp.status_code}")
                continue
            data = resp.json()
            limit = data.get("key", {}).get("limit")
            usage = data.get("key", {}).get("usage", 0)
            if limit is None:
                remaining = float("inf")
            else:
                remaining = limit - usage
            logger.info(f"[tavily_search] key ...{key[-6:]} usage={usage} limit={limit} remaining={remaining}")
            if remaining > best_remaining:
                best_remaining = remaining
                best_key = key
        except Exception as e:
            logger.warning(f"[tavily_search] key ...{key[-6:]} usage 查询异常: {e!r}")

    if best_key:
        _active_api_key = best_key
        logger.info(f"[tavily_search] 已选用额度最多的 key ...{best_key[-6:]}")
    else:
        _active_api_key = keys[0]
        logger.warning("[tavily_search] 所有 key usage 查询失败，回退使用第一个 key")


def _build_schema():
    return {
        "type": "function",
        "function": {
            "name": "tavily_search",
            "description": (
                "网页搜索工具。当你对用户的问题不确定、不了解、或涉及实时信息（新闻、天气、股价等）时，应主动使用此工具搜索以给出准确回答。"
                "不要猜测不确定的事实，优先搜索验证。人物、角色、作品资料搜索必须先用短查询定位可靠页面，只保留核心名称和少量来源/类型限定；"
                "先搜索页面，再用 fetch_url 抓取页面文本核对细节。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "简短搜索词。人物、角色或作品查询应先定位可靠页面，避免把多个待核对的外观或属性细节直接拼进 query。",
                    },
                },
                "required": ["query"],
            },
        },
    }


schema = _build_schema()


_MAX_CONTENT_PER_RESULT = 300


def _format_results(data: dict, max_chars: int = 6000) -> str:
    query = data.get("query", "")
    answer = data.get("answer", "")
    results = data.get("results", []) or []

    parts = [f"搜索「{query}」"]
    if answer:
        parts.append(f"\n[AI 摘要] {answer}")

    compact = "".join(parts)
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        url = r.get("url", "")
        content = r.get("content", "")
        if content and len(content) > _MAX_CONTENT_PER_RESULT:
            content = content[:_MAX_CONTENT_PER_RESULT] + "…"
        full_entry = f"\n{i}. {title}\n   {url}"
        if content:
            full_entry += f"\n   {content}"
        if len(compact) + len(full_entry) <= max_chars:
            compact += full_entry
        else:
            short_entry = f"\n{i}. {title} {url}"
            if len(compact) + len(short_entry) <= max_chars:
                compact += short_entry
            else:
                break
    return compact


async def _fallback_to_bocha(args: Dict[str, Any], config) -> str:
    global _tavily_disabled
    _tavily_disabled = True

    from . import TOOL_REGISTRY
    if "bocha_search" not in TOOL_REGISTRY:
        try:
            from . import bocha_search
            if getattr(config, "BOCHA_API_KEY", None):
                TOOL_REGISTRY["bocha_search"] = (bocha_search.schema, bocha_search.run)
                logger.info("[tavily_search] 已动态注册 bocha_search 作为 fallback")
        except Exception as e:
            logger.error(f"[tavily_search] 动态注册 bocha_search 失败: {e}")

    if "bocha_search" in TOOL_REGISTRY:
        logger.warning("[tavily_search] Tavily 调用失败，切换到 bocha_search")
        _, bocha_run = TOOL_REGISTRY["bocha_search"]
        result, _ = await bocha_run(args, config)
        return result

    logger.error("[tavily_search] Tavily 调用失败，且 bocha_search 不可用")
    return "搜索服务暂时不可用，请稍后再试。"


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    if not _active_api_key:
        return "Tavily 搜索未配置 TAVILY_API_KEY。", []

    query = str(args.get("query") or "").strip()
    if not query:
        return "搜索词不能为空。", []

    payload = {
        "query": query,
        "max_results": 20,
        "include_answer": "advanced",
        "search_depth": "basic",
    }
    headers = {
        "Authorization": f"Bearer {_active_api_key}",
        "Content-Type": "application/json",
    }

    try:
        timeout = getattr(config, "WEB_FETCH_TIMEOUT", 20)
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post("https://api.tavily.com/search", json=payload, headers=headers)

        if resp.status_code in (401, 429, 432, 433):
            logger.warning(f"[tavily_search] Tavily API 返回 {resp.status_code}，切换到 bocha_search")
            return await _fallback_to_bocha(args, config), []

        resp.raise_for_status()
        data = resp.json()

    except Exception as e:
        logger.warning(f"[tavily_search] Tavily 调用异常: {e!r}，切换到 bocha_search")
        return await _fallback_to_bocha(args, config), []

    results = data.get("results", []) or []
    if not results:
        return f"搜索「{query}」未找到相关结果。", []

    return _format_results(data, getattr(config, "WEB_FETCH_MAX_CHARS", 6000)), []
