"""上游 AnimaTool 人设库（GET /anima/characters）客户端：缓存 + 文本匹配 + 人设块渲染。

不是 LLM 工具（不导出 schema/run），由 chat_history.inject_character_personas 调用：
画图功能实际启用时，触发句提到的角色（名称/别名，含发言者自己的名字）把人设带出到触发轮的前导 system 块。

- 拉取：列表响应带 ETag（= revision），用 If-None-Match 低成本校验；至多每 _REFRESH_INTERVAL 秒
  校验一次，且放后台进行，请求路径直接用上次缓存（只有从未拉取过时才同步等一次，受 _REQUEST_TIMEOUT 限制）。
  上游离线时沿用旧缓存（从未成功过则为空，不注入），不阻塞、不影响对话。
- 匹配：与上游 characters/store.py 的 match_characters 同口径——名称/别名命中，ASCII 名要求词边界
  （cy 不命中 cyan），被其他人设更长的命中词完全覆盖时丢弃（「Chaos前世」只命中前世人设）。
  本地计算，免去每条消息一次 HTTP。
- 渲染：render_persona_block / render_persona_entries，只输出画图用的内容（人设库只存外观；
  调用方只在画图功能实际启用时带出，见 anima_generate.is_draw_active）。
"""
import asyncio
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx
from nonebot import logger

from ..config import config

PERSONA_BLOCK_TITLE = "[人设资料]"

# 缓存校验间隔 / 请求超时（秒）
_REFRESH_INTERVAL = 60.0
_REQUEST_TIMEOUT = 3.0
# 单条人设渲染的安全上限（字段本身已由上游限长，这里只防异常数据撑爆上下文）
_MAX_ENTRY_CHARS = 3000

_ASCII_ALNUM = re.compile(r"[A-Za-z0-9]")

_characters: List[Dict[str, Any]] = []
_etag: str = ""
_revision: Optional[int] = None
_loaded: bool = False
_last_attempt: float = 0.0  # time.monotonic()，0 = 从未尝试
_fail_logged: bool = False
_refresh_task: Optional["asyncio.Task[None]"] = None


def is_enabled() -> bool:
    return bool(getattr(config, "ANIMA_CHARACTERS_ENABLE", True))


def _list_url() -> str:
    base = str(getattr(config, "COMFYUI_BASE_URL", "") or "http://127.0.0.1:8188")
    return f"{base.rstrip('/')}/anima/characters"


def _apply_response(status: int, headers: Any, body: Any) -> None:
    """解析列表响应并更新缓存（304 视为未变化）。"""
    global _characters, _etag, _revision, _loaded, _fail_logged
    _fail_logged = False
    if status == 304:
        return
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("characters"), list):
        logger.warning(f"[人设库] 列表响应异常: HTTP {status}")
        return
    chars = [
        c for c in body["characters"]
        if isinstance(c, dict) and str(c.get("id") or "").strip() and str(c.get("name") or "").strip()
    ]
    revision = body.get("revision")
    if not _loaded or revision != _revision:
        logger.info(f"[人设库] 已加载 {len(chars)} 个人设（revision={revision}）")
    _characters = chars
    _etag = str(headers.get("ETag") or "")
    _revision = revision
    _loaded = True


def _log_failure(e: Exception) -> None:
    global _fail_logged
    # 连续失败只报一次，恢复后重置（上游离线期间每分钟一条 warning 没有意义）
    if not _fail_logged:
        logger.warning(f"[人设库] 拉取失败（沿用缓存 {len(_characters)} 条）: {e.__class__.__name__}: {e}")
        _fail_logged = True


async def _refresh() -> None:
    global _last_attempt
    _last_attempt = time.monotonic()
    headers = {"If-None-Match": _etag} if (_loaded and _etag) else {}
    try:
        async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
            resp = await client.get(_list_url(), headers=headers)
        body = resp.json() if resp.status_code == 200 else None
        _apply_response(resp.status_code, resp.headers, body)
    except Exception as e:
        _log_failure(e)


def refresh_sync() -> None:
    """启动时同步拉取一次（画图服务 health check 通过后调用），运行期不必再同步等待首次加载。"""
    global _last_attempt
    if not is_enabled():
        return
    _last_attempt = time.monotonic()
    try:
        with httpx.Client(timeout=_REQUEST_TIMEOUT) as client:
            resp = client.get(_list_url())
        body = resp.json() if resp.status_code == 200 else None
        _apply_response(resp.status_code, resp.headers, body)
    except Exception as e:
        _log_failure(e)


async def get_characters() -> List[Dict[str, Any]]:
    """当前人设列表（缓存）。到期时后台校验，不阻塞调用方；从未尝试过拉取时同步等一次。"""
    global _refresh_task
    if not is_enabled():
        return []
    if _last_attempt == 0.0:
        await _refresh()
    elif time.monotonic() - _last_attempt >= _REFRESH_INTERVAL and (_refresh_task is None or _refresh_task.done()):
        _refresh_task = asyncio.create_task(_refresh())
    return _characters


# -------------------------
# 匹配（与上游 match_characters 同口径）
# -------------------------

def _term_spans(text_l: str, term: str) -> List[Tuple[int, int]]:
    """term 在文本中的全部出现区间；两端是 ASCII 字母数字时要求词边界，中文名不受影响。"""
    spans: List[Tuple[int, int]] = []
    start = 0
    n = len(term)
    while True:
        pos = text_l.find(term, start)
        if pos < 0:
            break
        end = pos + n
        ok = True
        if _ASCII_ALNUM.match(term[0]) and pos > 0 and _ASCII_ALNUM.match(text_l[pos - 1]):
            ok = False
        if ok and _ASCII_ALNUM.match(term[-1]) and end < len(text_l) and _ASCII_ALNUM.match(text_l[end]):
            ok = False
        if ok:
            spans.append((pos, end))
        start = pos + 1
    return spans


def match_characters(characters: Iterable[Dict[str, Any]], text: str) -> List[Dict[str, Any]]:
    """文本中按名称/别名命中的人设，按首次出现位置排序（调用方按优先级拼接文本即得优先级顺序）。
    最长词优先：某人设的全部命中位置都落在另一人设更长的命中词里时丢弃。"""
    text_l = (text or "").lower()
    if not text_l.strip():
        return []
    hits: List[Tuple[Dict[str, Any], str, List[Tuple[int, int]]]] = []
    for c in characters:
        best_term, best_spans = "", []
        for term in [c.get("name", "")] + list(c.get("aliases") or []):
            t = str(term or "").strip().lower()
            if not t:
                continue
            spans = _term_spans(text_l, t)
            if spans and len(t) > len(best_term):
                best_term, best_spans = t, spans
        if best_spans:
            hits.append((c, best_term, best_spans))

    result: List[Tuple[int, Dict[str, Any]]] = []
    for c, term, spans in hits:
        def covered(span: Tuple[int, int]) -> bool:
            s, e = span
            return any(
                o is not c and len(ot) > len(term) and any(os_ <= s and e <= oe for os_, oe in ospans)
                for o, ot, ospans in hits
            )
        if all(covered(sp) for sp in spans):
            continue
        result.append((spans[0][0], c))
    result.sort(key=lambda x: x[0])
    return [c for _, c in result]


def exclude_names(characters: Iterable[Dict[str, Any]], names: Iterable[str]) -> List[Dict[str, Any]]:
    """去掉名称/别名与 names 中任一项相同（忽略大小写）的人设。
    用于排除 bot 自己的人设：它已在人格 md 里定义，不必再带出。"""
    banned = {str(n or "").strip().lower() for n in names} - {""}
    if not banned:
        return list(characters)

    def _terms(c: Dict[str, Any]) -> set:
        return {str(t or "").strip().lower() for t in [c.get("name")] + list(c.get("aliases") or [])}

    return [c for c in characters if not (_terms(c) & banned)]


def character_version(c: Dict[str, Any]) -> float:
    """人设版本（上游 updated_at），用于判断已带出的人设是否已被修改。"""
    try:
        return float(c.get("updated_at") or c.get("created_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0


# -------------------------
# 渲染
# -------------------------

def _s(value: Any) -> str:
    return " ".join(str(value or "").split()) if not isinstance(value, str) else value.strip()


def _render_entry(c: Dict[str, Any], updated: bool) -> str:
    """单条人设（画图用）；没有任何可用内容（仅剩标题）时返回空串。"""
    aliases = [a for a in (_s(x) for x in (c.get("aliases") or [])) if a]
    title = f"◆ {_s(c.get('name'))}"
    if aliases:
        title += f"（别名: {'、'.join(aliases)}）"
    if updated:
        title += "（人设已更新，以此条为准）"
    lines: List[str] = []
    head = [
        f"{label}={val}" for label, val in (
            ("count", _s(c.get("count"))),
            ("角色", _s(c.get("tag_name"))),
            ("作品", _s(c.get("series"))),
            ("画师", _s(c.get("artist"))),
        ) if val
    ]
    if head:
        lines.append("画图: " + "; ".join(head))
    tags = _s(c.get("tags"))
    if tags:
        lines.append(f"基础外观: {tags}")
    outfits = []
    for o in c.get("outfits") or []:
        if not isinstance(o, dict):
            continue
        o_name = _s(o.get("name")) or "造型"
        o_body = _s(o.get("tags")) or _s(o.get("description"))
        if not o_body:
            continue
        outfits.append(f"{o_name}{'(默认)' if o.get('default') else ''} = {o_body}")
    if outfits:
        lines.append("造型: " + "；".join(outfits))
    # 上游唯一的自然语言字段：外观描述（绘制注意也写在里面）
    description = " ".join(_s(c.get("description")).split())
    if description:
        lines.append(f"描述: {description}")
    if not lines:
        return ""
    text = "\n".join([title] + lines)
    if len(text) > _MAX_ENTRY_CHARS:
        text = text[:_MAX_ENTRY_CHARS] + "…"
    return text


def render_persona_entries(
    characters: Iterable[Dict[str, Any]],
    updated_ids: Iterable[str] = (),
) -> List[Tuple[Dict[str, Any], str]]:
    """逐条渲染，返回 [(人设, 文本)]；跳过没有可用内容的人设。"""
    updated = set(updated_ids)
    out: List[Tuple[Dict[str, Any], str]] = []
    for c in characters:
        text = _render_entry(c, str(c.get("id")) in updated)
        if text:
            out.append((c, text))
    return out


def persona_block_header() -> str:
    return (
        f"{PERSONA_BLOCK_TITLE} 对话里提到的角色，画他们时按这里组合：外观 = 基础外观 + 所选造型"
        "（没指定用默认造型），描述里的注意事项要遵守，用户另有要求时从用户。"
    )


def render_persona_block(characters: Iterable[Dict[str, Any]], updated_ids: Iterable[str] = ()) -> str:
    """完整人设块（标题行 + 各条人设）；没有可渲染的人设时返回空串。"""
    entries = render_persona_entries(characters, updated_ids)
    if not entries:
        return ""
    return persona_block_header() + "\n" + "\n".join(text for _, text in entries)
