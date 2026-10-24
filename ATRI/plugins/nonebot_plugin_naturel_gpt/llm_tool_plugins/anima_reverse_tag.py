"""Anima 提示词反推工具 - 参考图作画前置步骤。

调用上游 AnimaTool 服务的 POST /anima/reverse_tag（与 COMFYUI_BASE_URL 同服务，WD14 tagger），
返回图片的分级/角色候选以及一份可直接作为 generate_anima_image 参数基础的反推结果
（响应里的 anima 字段：quality_meta_year_safe / count / character / series / tags）。

图片获取方式与 anime_trace 一致：从 tg._visible_images 按 [图片N] 编号取 URL，
经 image_cache 下载为 base64 提交（QQ 图床 URL 上游无法直接访问，故用 image_base64 而非 image_path）。

本工具不参与自动发现（见 llm_tool_plugins/__init__.py 的 _EXCLUDED_MODULES），
由 enable_anima_tool() 随画图工具一起注册、disable_anima_tool() 一起移除，
并列入 openai_func._DRAW_ONLY_TOOLS（draw_mode=off 的会话不暴露）。
"""

from typing import Any, Dict, List, Tuple

import httpx

from ..logger import logger

# 反推请求超时（实测 XPU 上单张 1~7 秒，留足首次加载模型的余量）
_REVERSE_TAG_TIMEOUT = 120

# 角色候选展示的最低置信度：WD14 分类器的 character 头置信度普遍很低（实测正常图 top1 仅 0.07），
# 低于该值的候选只会误导模型，不予展示
_CHARACTER_MIN_SCORE = 0.5


# ============ 工具 Schema ============

schema = {
    "type": "function",
    "function": {
        "name": "anima_reverse_tag",
        "description": (
            "图片提示词反推（tagger）。分析图片内容，返回图片分级、角色候选和一份可直接用于作画的"
            "反推标签（人数/角色/作品/通用 tags）。当用户发来参考图要求「照着这张画」「参考这张图作画」"
            "「画一个类似的」「按这张图的风格/角色画」时，必须先调用本工具反推标签，"
            "再综合反推结果、图片本身和用户的具体要求（增删标签、调整动作/场景/构图/画风）调用 generate_anima_image 作画。"
            "image_index 对应本次对话中出现的 [图片N] 的 N（[图片已过期] 不可用）。"
            "仅服务于参考图作画场景：用户只是问图里有什么时用 vision，问角色是谁时用 anime_trace。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_index": {
                    "type": "integer",
                    "description": "图片序号，对应 [图片N] 的 N（从 1 开始）",
                },
                "threshold": {
                    "type": "number",
                    "description": "标签置信度阈值（可选，0.2~0.7），缺省由上游决定。想拿更全的标签可调低，只要核心标签可调高。",
                },
            },
            "required": ["image_index"],
        },
    },
}


# ============ 结果格式化 ============

def _format_result(data: Dict[str, Any]) -> str:
    """把 /anima/reverse_tag 响应格式化为给主模型的文本结果。"""
    lines: List[str] = []

    rating = data.get("rating") or {}
    if rating:
        top_rating = max(rating.items(), key=lambda kv: kv[1])
        lines.append(f"[图片分级] {top_rating[0]} {top_rating[1]:.2f}")

    anima = data.get("anima") or {}
    if anima:
        lines.append("[反推作画参数]（可直接作为 generate_anima_image 的参数基础）")
        for key in ("quality_meta_year_safe", "count", "character", "series", "tags"):
            val = anima.get(key)
            if val:
                lines.append(f"{key}: {val}")

    characters = data.get("character_top5") or {}
    if characters:
        top3 = sorted(characters.items(), key=lambda kv: kv[1], reverse=True)[:3]
        if top3 and top3[0][1] >= _CHARACTER_MIN_SCORE:
            lines.append("[角色候选]（分类器结果，仅供参考；确认角色身份用 anime_trace）")
            for name, score in top3:
                lines.append(f"{name} {score:.2f}")

    lines.append(
        "[下一步] 综合反推标签、图片本身和用户的具体要求（增删标签、调整动作/场景/构图/画风），"
        "调用 generate_anima_image 作画；不要原样照抄反推结果。"
    )
    return "\n".join(lines)


# ============ 工具执行 ============

async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    idx = int(args.get("image_index") or 1)
    threshold = args.get("threshold")

    from ..openai_func import TextGenerator
    tg = TextGenerator.instance

    # 1) 按显示编号查本次请求可见图片表（与 vision / anime_trace 同源，与模型看到的 [图片N] 一致）
    table = dict(tg._visible_images or {})
    if not table:
        return "当前上下文没有可用图片（可能图片已过期或未开启多模态提取）。", []
    url = table.get(idx)
    if not url:
        available = ", ".join(str(k) for k in sorted(table))
        return f"图片 {idx} 已过期或不在当前上下文（可用编号：{available}），请检查 [图片N] 的 N。", []

    # 2) URL → base64（复用 image_cache 的 LRU 缓存 / QQ UA+Referer 下载）。
    # 强制 base64：上游服务无法访问 QQ 图床 URL，只接受 image_base64 / image_path
    from .. import image_cache
    try:
        resolved = await image_cache.resolve_urls([url], force_base64=True)
    except Exception as e:
        logger.warning(f"[anima_reverse_tag] 图片下载异常: {e!r} | {str(url)[:80]}")
        return "图片下载失败，请让用户重新发送。", []
    if not resolved:
        return "图片无法下载（可能已过期），请让用户重新发送。", []
    data_uri = resolved[0]
    # data:image/jpeg;base64,xxxx → 纯 base64
    b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri

    # 3) 提交反推
    base_url = getattr(config, "COMFYUI_BASE_URL", "http://127.0.0.1:8188").rstrip("/")
    payload: Dict[str, Any] = {"image_base64": b64}
    if threshold is not None:
        try:
            payload["threshold"] = float(threshold)
        except (TypeError, ValueError):
            pass
    try:
        async with httpx.AsyncClient(timeout=_REVERSE_TAG_TIMEOUT) as client:
            resp = await client.post(f"{base_url}/anima/reverse_tag", json=payload)
    except Exception as e:
        logger.warning(f"[anima_reverse_tag] 反推请求失败: {e!r}")
        return f"反推服务请求失败：{e!r}，请稍后再试；也可以直接根据自己看到的内容作画。", []

    if resp.status_code != 200:
        logger.warning(f"[anima_reverse_tag] HTTP {resp.status_code}: {resp.text[:200]}")
        return f"反推失败：HTTP {resp.status_code}。可以直接根据自己看到的内容作画。", []

    try:
        data = resp.json()
    except Exception:
        logger.warning(f"[anima_reverse_tag] 响应非 JSON: {resp.text[:200]}")
        return "反推服务返回格式异常，可以直接根据自己看到的内容作画。", []

    if not data.get("success"):
        err = data.get("error") or "未知错误"
        logger.warning(f"[anima_reverse_tag] 反推失败: {err}")
        return f"反推失败：{err}。可以直接根据自己看到的内容作画。", []

    result = _format_result(data)
    logger.info(
        f"[anima_reverse_tag] image_index={idx} model={data.get('model')} "
        f"耗时={data.get('seconds')}s | {result[:100]}{'...' if len(result) > 100 else ''}"
    )
    return result, []
