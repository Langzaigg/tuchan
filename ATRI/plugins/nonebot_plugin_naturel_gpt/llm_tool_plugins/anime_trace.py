"""AnimeTrace 以图识角色工具 - 识别图片中的 ACGN 角色及其出处作品。

调用 AnimeTrace 开放 API（https://ai.animedb.cn/zh/api-docs）：
- POST /v1/search 识别图片，返回每个人物检测框的候选「角色名 + 作品名称」列表；
- GET  /v1/model/list 动态获取可用识别模型（文档要求不要写死模型名，本模块缓存 1 小时）。

图片获取方式与 vision 工具一致：从 tg._current_trigger_images 取 [图片N] 对应的真实
URL，经 image_cache 下载为 data URI 后剥前缀得纯 base64 提交（QQ 图床 URL 第三方无法
直接访问，故不传 url 参数）。识别模型是专用角色数据库，比通用视觉模型更准确，因此
多模态 profile 同样暴露本工具。
"""

import time
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..logger import logger

# API 配置
ANIMETRACE_API_BASE = "https://api.animetrace.com"
# 模型列表缓存失效时间（秒）：文档称可用模型动态增减，不能永久写死
_MODEL_LIST_TTL = 3600
# /v1/model/list 全部失败时的兜底模型（当前官方默认，仅作最后手段）
_FALLBACK_MODEL = "animetrace-yuri-4.2"

# 模型缓存: (模型id, 过期时间戳)
_model_cache: Optional[Tuple[str, float]] = None

# 业务状态码 → 用户可读说明（来自 API 文档状态码表）
_CODE_MESSAGES = {
    17701: "图片大小过大",
    17702: "服务器繁忙，请稍后重试",
    17703: "请求参数不正确",
    17704: "API 维护中",
    17705: "图片格式不支持",
    17706: "识别无法完成，请重试",
    17707: "内部错误",
    17708: "图片中的人物数量超过限制",
    17722: "图片下载失败",
    17728: "已达到本次使用上限",
    17731: "服务利用人数过多，请稍后重试",
}


# ============ 工具 Schema ============

schema = {
    "type": "function",
    "function": {
        "name": "anime_trace",
        "description": (
            "动漫角色识别（以图搜番/搜角色）。基于专用 ACGN 角色数据库识别图片中的角色名字和出处作品，"
            "比通用视觉模型更准确。当用户发送动漫/游戏图片并询问「这是什么角色」「出自什么作品」"
            "「求出处」「这是谁」等问题时调用。"
            "[图片N] 的编号在整个对话上下文中全局唯一（1..N），image_index 对应 [图片N] 的 N；"
            "图片中有多个人物时会在结果中分人物逐个返回候选。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_index": {
                    "type": "integer",
                    "description": "图片序号，对应 [图片N] 的 N（从 1 开始）",
                },
            },
            "required": ["image_index"],
        },
    },
}


# ============ 模型列表 ============

async def _pick_model() -> str:
    """获取可用的识别模型 ID。优先官方 default，其次第一个 enabled；缓存 1 小时。"""
    global _model_cache
    now = time.time()
    if _model_cache and _model_cache[1] > now:
        return _model_cache[0]

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{ANIMETRACE_API_BASE}/v1/model/list")
            resp.raise_for_status()
            payload = resp.json()
        models = [
            m for m in (payload.get("data") or [])
            if isinstance(m, dict) and m.get("enabled")
        ]
        chosen = next((m["id"] for m in models if m.get("default")), None)
        if not chosen and models:
            chosen = models[0].get("id")
        if chosen:
            _model_cache = (str(chosen), now + _MODEL_LIST_TTL)
            return chosen
    except Exception as e:
        logger.warning(f"[anime_trace] 获取模型列表失败，使用兜底模型: {e!r}")
    return _FALLBACK_MODEL


# ============ 结果格式化 ============

def _format_pos(box: List[float]) -> str:
    """把 0~1 相对坐标 box [x1,y1,x2,y2] 格式化为百分比位置描述。"""
    try:
        x1, y1, x2, y2 = box
        return f"({x1*100:.0f}%,{y1*100:.0f}%)~({x2*100:.0f}%,{y2*100:.0f}%)"
    except Exception:
        return "位置未知"


def _format_result(payload: Dict[str, Any]) -> str:
    """把 /v1/search 响应格式化为给主模型的纯文本结果。"""
    is_ai = payload.get("ai")
    data = payload.get("data") or []

    lines = []
    if is_ai is True:
        lines.append("检测：该图疑似 AI 生成图。")
    if not data:
        lines.append("图中未检测到可识别的人物。")
        return "\n".join(lines)

    lines.append(f"共检测到 {len(data)} 个人物：")
    for i, item in enumerate(data, 1):
        candidates = item.get("character") or []
        flags = []
        if item.get("not_confident"):
            flags.append("置信度较低，候选仅供参考")
        head = f"人物{i} [{_format_pos(item.get('box') or [])}]"
        if flags:
            head += "（" + "；".join(flags) + "）"
        lines.append(head)
        if not candidates:
            lines.append("  未识别出候选角色")
            continue
        for j, cand in enumerate(candidates, 1):
            work = str(cand.get("work") or "").strip() or "?"
            char = str(cand.get("character") or "").strip() or "?"
            lines.append(f"  {j}. {char} ——《{work}》")
    lines.append("候选按可能性从高到低排列，优先采用第 1 个；可用 bangumi 工具核实作品信息。")
    return "\n".join(lines)


# ============ 工具执行 ============

async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    idx = int(args.get("image_index") or 1)

    from ..openai_func import TextGenerator
    tg = TextGenerator.instance

    # 1) 取图片 URL 列表（与 vision 工具同源：vision profile 为全上下文全局列表，否则为触发消息图片）
    urls = list(tg._current_trigger_images or [])
    if not urls:
        return "当前会话没有可用图片（可能图片已过期或未开启多模态提取）。", []
    if idx < 1 or idx > len(urls):
        return f"图片序号超出范围：当前共 {len(urls)} 张图，请检查 [图片N] 的 N。", []
    url = urls[idx - 1]

    # 2) URL → base64（复用 image_cache 的 LRU 缓存 / QQ UA+Referer 下载）
    from .. import image_cache
    try:
        resolved = await image_cache.resolve_urls([url])
    except Exception as e:
        logger.warning(f"[anime_trace] 图片下载异常: {e!r} | {str(url)[:80]}")
        return "图片下载失败，请让用户重新发送。", []
    if not resolved:
        return "图片无法下载（可能已过期），请让用户重新发送。", []
    data_uri = resolved[0]
    # data:image/jpeg;base64,xxxx → 纯 base64
    b64 = data_uri.split(",", 1)[1] if "," in data_uri else data_uri

    # 3) 查询可用模型并提交识别（multipart/form-data，is_multi=1 返回多候选，ai_detect=1 附带 AI 图检测）
    model = await _pick_model()
    form = {
        "model": (None, model),
        "is_multi": (None, "1"),
        "ai_detect": (None, "1"),
        "base64": (None, b64),
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{ANIMETRACE_API_BASE}/v1/search", files=form)
    except Exception as e:
        logger.warning(f"[anime_trace] 识别请求失败: {e!r}")
        return f"识别服务请求失败：{e!r}，请稍后再试。", []

    if resp.status_code != 200:
        logger.warning(f"[anime_trace] HTTP {resp.status_code}: {resp.text[:200]}")
        # 413 对应 17701 图片过大
        hint = "图片大小过大" if resp.status_code == 413 else f"HTTP {resp.status_code}"
        return f"识别失败：{hint}。", []

    try:
        payload = resp.json()
    except Exception:
        logger.warning(f"[anime_trace] 响应非 JSON: {resp.text[:200]}")
        return "识别服务返回格式异常，请稍后再试。", []

    code = payload.get("code")
    if code != 0:
        hint = _CODE_MESSAGES.get(code, f"错误码 {code}")
        logger.warning(f"[anime_trace] 业务错误 {code}: {hint} | trace_id={payload.get('trace_id')}")
        return f"识别失败：{hint}。", []

    result = _format_result(payload)
    logger.info(
        f"[anime_trace] image_index={idx} model={model} → "
        f"{len(payload.get('data') or [])}个人物 | {result[:100]}{'...' if len(result) > 100 else ''}"
    )
    return result, []
