"""DanbooruSearch 标签检索工具 - 把「角色名 / 视觉概念」落实成准确的 Danbooru 标签。

上游项目：https://github.com/SuzumiyaAkizuki/DanbooruSearchOnline
公共站点：魔搭创空间（国内直连，首选）+ HuggingFace Space（需代理，回落）

定位：只在画图任务里「拿不准具体作画 tag」时用。Anima 画图工具本身支持自然语言描述，
因此本工具不做整段提示词润色，只负责确定角色的标准标签与外观特征。

预制行为（角色模式）：搜到 Character 类标签后，自动再调一次 /api/related，
把该角色的共现外观/服饰标签直接展开给主模型，省掉模型二次调用的一轮。

NSFW 标签是否返回与提示词破限开关（rg nolimit / UNLOCK_CONTENT_LIMIT）联动，
详见 _get_show_nsfw。

站点选择：加载时一次性可用性判断（先魔搭直连，失败再试 HF + TOOL_PROXY）；
运行中活跃站点失败一次即永久回落到 HF + 代理，不再来回试探。
魔搭边缘节点会拦截非浏览器 UA（返回 403 SDK Token 提示），故所有请求都带浏览器 UA。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from ..logger import logger

# ============ 站点配置 ============

# 魔搭创空间：国内直连可用，首选
MS_BASE = "https://sakizuki-danboorusearchonline.ms.show"
# HuggingFace Space：需要代理，作为回落；空闲时会休眠，冷启动约 30~60 秒
HF_BASE = "https://sakizuki-danboorusearch.hf.space"

# 魔搭边缘节点按 UA 拦截：python-httpx / curl 等非浏览器 UA 一律 403，必须伪装浏览器
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

# 探测超时：魔搭直连给短超时快速失败；HF 走代理且可能冷启动，给宽一点
_MS_PROBE_TIMEOUT = 8
_HF_PROBE_TIMEOUT = 20
_SEARCH_TIMEOUT = 30
_RELATED_TIMEOUT = 20

# 搜索参数预设，与上游 MCP 的 search_mode 预设保持一致
_PRESETS: Dict[str, Dict[str, Any]] = {
    # precise_lookup：单一概念精确查词 / 拼写纠错，角色名查询用这个
    "character": {
        "top_k": 10, "limit": 10, "popularity_weight": 0.15,
        "use_segmentation": False, "group_mode": "off", "max_per_group": 2,
    },
    # subject_describe：只描述一个单一视觉概念时使用，关闭分词
    "concept": {
        "top_k": 20, "limit": 20, "popularity_weight": 0.15,
        "use_segmentation": False, "group_mode": "off", "max_per_group": 2,
    },
}

# 角色外观标签展开数量上限，超出对模型没有增量信息，只是白烧 token
_MAX_APPEARANCE = 24
# 角色 wiki 简介截断长度
_MAX_WIKI_CHARS = 120

# ============ 运行期站点状态 ============

# 当前活跃站点与其代理，由 should_load() 一次性判定
_base_url: str = MS_BASE
_proxy: Optional[str] = None
# 是否已用掉唯一一次回落机会（启动时就选中 HF、或运行中失败过一次都置 True）
_failed_over: bool = False


def _normalize_proxy(raw: Any) -> Optional[str]:
    """把 TOOL_PROXY 规范成 httpx 可用的代理串，空值返回 None（直连）。"""
    proxy = str(raw or "").strip()
    if not proxy:
        return None
    if not proxy.startswith(("http", "socks")):
        proxy = f"http://{proxy}"
    return proxy


def _probe(base: str, proxy: Optional[str], timeout: int) -> bool:
    """同步探测站点健康状态。should_load 在插件导入期（无事件循环）调用，故用同步客户端。"""
    try:
        with httpx.Client(proxy=proxy, timeout=timeout, headers=_HEADERS) as client:
            resp = client.get(f"{base}/api/health")
            resp.raise_for_status()
            data = resp.json()
        return bool(data.get("status") == "ok" and data.get("loaded"))
    except Exception as e:
        logger.warning(f"[danbooru_search] 站点探测失败 {base}: {e!r}")
        return False


def should_load(config) -> bool:
    """加载时一次性可用性判断：魔搭直连 → HF + 代理 → 都不通则不注册本工具。"""
    global _base_url, _proxy, _failed_over

    if _probe(MS_BASE, None, _MS_PROBE_TIMEOUT):
        _base_url, _proxy, _failed_over = MS_BASE, None, False
        logger.info("[danbooru_search] 已选用魔搭创空间站点（直连）")
        return True

    fallback_proxy = _normalize_proxy(getattr(config, "TOOL_PROXY", ""))
    logger.warning(
        f"[danbooru_search] 魔搭站点不可用，尝试 HF 站点（代理: {fallback_proxy or '无，直连'}）"
    )
    if _probe(HF_BASE, fallback_proxy, _HF_PROBE_TIMEOUT):
        # 启动就落到 HF，说明已无下一级回落目标，直接标记回落完成
        _base_url, _proxy, _failed_over = HF_BASE, fallback_proxy, True
        logger.info("[danbooru_search] 已选用 HuggingFace 站点")
        return True

    logger.warning("[danbooru_search] 魔搭与 HF 站点均不可用，本工具不注册")
    return False


async def _post_once(
    base: str, proxy: Optional[str], path: str, payload: Dict[str, Any], timeout: int
) -> Dict[str, Any]:
    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, headers=_HEADERS) as client:
        resp = await client.post(f"{base}{path}", json=payload)
        resp.raise_for_status()
        return resp.json()


async def _post(path: str, payload: Dict[str, Any], config, timeout: int) -> Dict[str, Any]:
    """向活跃站点发请求；失败且尚未回落过时，永久切到 HF + 代理并重试一次。

    「固定回落」意味着一次失败之后本进程后续所有请求都走 HF，不再回头试魔搭，
    避免每次调用都先吃一次超时。
    """
    global _base_url, _proxy, _failed_over
    try:
        return await _post_once(_base_url, _proxy, path, payload, timeout)
    except Exception as e:
        if _failed_over:
            raise
        _base_url = HF_BASE
        _proxy = _normalize_proxy(getattr(config, "TOOL_PROXY", ""))
        _failed_over = True
        logger.warning(
            f"[danbooru_search] 魔搭站点请求失败，永久回落到 HF 站点"
            f"（代理: {_proxy or '无，直连'}）: {e!r}"
        )
        return await _post_once(_base_url, _proxy, path, payload, timeout)


def _get_show_nsfw(config) -> bool:
    """NSFW 标签是否可见 —— 与提示词破限开关联动。

    用的就是 chat_prompt 注入破限规则、漫画模式追加 MANGA_UNLOCK_RULES 的同一个开关：
    群级 rg nolimit on/off 优先，群级未设置时回退全局 UNLOCK_CONTENT_LIMIT。
    破限关着时不该从检索侧漏 NSFW 标签进提示词，拿不到会话也按全局默认走（保守方向）。
    """
    try:
        from ..openai_func import TextGenerator
        from ..chat_manager import ChatManager

        chat_key = TextGenerator.instance._current_chat_key
        if chat_key:
            chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)
            return bool(chat.get_unlock_content_limit())
    except Exception:
        pass
    return bool(getattr(config, "UNLOCK_CONTENT_LIMIT", False))


# ============ 结果格式化 ============

def _cn(item: Dict[str, Any]) -> str:
    """取中文名的第一段。cn_name 形如「水手裙,制服,校服,日本服饰」，后面是语义扩展词，给模型看没意义。"""
    raw = str(item.get("cn_name") or "").strip()
    if not raw:
        return ""
    return re.split(r"[,，]", raw)[0].strip()


def _fmt(item: Dict[str, Any]) -> str:
    """标签渲染为「tag（中文名）」。"""
    tag = str(item.get("tag") or "").strip()
    cn = _cn(item)
    return f"{tag}（{cn}）" if cn else tag


def _pick_character(results: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    chars = [r for r in results if r.get("category") == "Character"]
    if not chars:
        return None
    return max(chars, key=lambda r: r.get("final_score") or 0)


def _format_character(
    query: str,
    char: Dict[str, Any],
    results: List[Dict[str, Any]],
    related: List[Dict[str, Any]],
) -> str:
    lines = [f"「{query}」→ 角色标签: {_fmt(char)}"]

    # 出处作品：优先取共现结果里的 Copyright（更贴该角色），其次搜索结果里的
    series = [r for r in related if r.get("category") == "Copyright"][:1]
    if not series:
        series = [r for r in results if r.get("category") == "Copyright"][:1]
    if series:
        lines.append(f"出处作品标签: {_fmt(series[0])}")

    wiki = str(char.get("wiki") or "").strip()
    if wiki:
        lines.append(f"角色简介: {wiki[:_MAX_WIKI_CHARS]}")

    # 外观特征：只要 General；剔除 xxx_(cosplay)（那是别人扮演该角色，不是角色自身特征）
    appearance = [
        r for r in related
        if r.get("category") == "General" and not str(r.get("tag") or "").endswith("_(cosplay)")
    ][:_MAX_APPEARANCE]
    if appearance:
        lines.append("常见外观/服饰特征标签（按共现强度排序）:")
        lines.append("  " + ", ".join(_fmt(r) for r in appearance))
    else:
        lines.append("（未取到该角色的共现特征标签，可直接用你已知的角色外观描述作画）")

    others = [
        r for r in results
        if r.get("category") == "Character" and r.get("tag") != char.get("tag")
    ][:3]
    if others:
        lines.append("其他候选角色（若上面这个不是用户想要的）: " + ", ".join(_fmt(r) for r in others))

    lines.append(
        "用法: 角色标签填入画图工具的 character 字段、作品标签填入 series 字段、"
        "外观特征填入 appearance 字段。画图模型支持自然语言，只挑与本次画面相关的特征即可，"
        "不必全部照抄，也不要把括号里的中文名当标签写进去。"
    )
    return "\n".join(lines)


def _format_concept(query: str, results: List[Dict[str, Any]], note: str = "") -> str:
    lines = [note] if note else []
    lines.append(f"「{query}」相关的 Danbooru 标签（按匹配度排序）:")
    for r in results:
        cat = str(r.get("category") or "")
        prefix = f"[{cat}] " if cat and cat != "General" else ""
        lines.append(f"  {prefix}{_fmt(r)}")
    lines.append(
        "用法: 挑选贴合用户描述的标签填入画图工具的 tags / appearance 字段，不相关的直接丢弃；"
        "画图模型支持自然语言，不必凑满标签。"
    )
    return "\n".join(lines)


# ============ 工具 Schema ============

schema = {
    "type": "function",
    "function": {
        "name": "danbooru_search",
        "description": (
            "Danbooru 标签检索。仅在画图相关任务中、你拿不准某个角色或视觉概念对应的标准作画标签时调用。"
            "典型场景：用户要画某个动漫/游戏角色，而你不确定该角色的标准标签或长相特征（发色、瞳色、服饰、配饰等）。"
            "查角色时会自动展开该角色的常见外观特征标签，你据此填写画图参数即可，不需要再追加调用。"
            "一次只查一个角色或一个概念，多个角色分多次调用。"
            "本工具不负责润色整段提示词——画图模型本身支持自然语言描述，"
            "只有「把角色/概念落实成准确标签」这件事才需要它。"
            "你已经确定要画什么标签、或用户描述本身就足够具体时，不要调用。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "检索内容，推荐中文（该引擎对中文优化更好）。"
                        "角色模式填角色名（可带作品名消歧，如「碧蓝档案 白洲梓」）；"
                        "概念模式填单个视觉概念（如「机械义肢」「水手服」）。"
                    ),
                },
                "target": {
                    "type": "string",
                    "enum": ["character", "concept"],
                    "description": (
                        "character（默认）: 查角色，返回角色标准标签 + 出处作品 + 展开的外观特征标签；"
                        "concept: 查单个服饰/道具/场景等视觉概念对应的标签。"
                    ),
                },
            },
            "required": ["query"],
        },
    },
}


# ============ 工具执行 ============

async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    query = str(args.get("query") or "").strip()
    if not query:
        return "检索内容为空，请给出要查询的角色名或视觉概念。", []

    target = str(args.get("target") or "character").strip().lower()
    if target not in _PRESETS:
        target = "character"

    show_nsfw = _get_show_nsfw(config)
    payload = {"query": query, "show_nsfw": show_nsfw, **_PRESETS[target]}

    try:
        data = await _post("/api/search", payload, config, _SEARCH_TIMEOUT)
    except Exception as e:
        logger.warning(f"[danbooru_search] 检索失败 query={query!r}: {e!r}")
        return (
            f"标签检索服务暂时不可用（{e!r}）。请直接用自然语言描述作画内容，画图模型能理解。",
            [],
        )

    # /api/search 的 results 不受 show_nsfw 约束（只有 tags_sfw 是过滤过的），
    # 必须按每条的 nsfw 字段自行过滤，上游 MCP 层同样是这么做的。
    # /api/related 则是服务端过滤，返回项不带 nsfw 字段，无需在此重复处理。
    results = data.get("results") or []
    if not show_nsfw:
        results = [r for r in results if str(r.get("nsfw") or "0") != "1"]
    if not results:
        return (
            f"没有检索到与「{query}」相关的 Danbooru 标签"
            f"（该库只收录频数 ≥100 的标签，冷门角色可能查不到）。"
            f"请直接用自然语言描述作画内容。",
            [],
        )

    if target == "concept":
        content = _format_concept(query, results)
        logger.info(f"[danbooru_search] concept query={query!r} → {len(results)} 个标签 @ {_base_url}")
        return content, []

    char = _pick_character(results)
    if not char:
        # 没匹配到角色标签：不空手而归，把搜到的普通标签给模型，够它继续画图
        content = _format_concept(
            query, results, note=f"未匹配到「{query}」对应的角色标签，以下是语义相近的标签："
        )
        logger.info(
            f"[danbooru_search] character query={query!r} → 未命中角色，"
            f"返回 {len(results)} 个相近标签 @ {_base_url}"
        )
        return content, []

    # 预制行为：命中角色后自动展开其共现标签，省掉模型再调一轮
    related: List[Dict[str, Any]] = []
    try:
        rel_data = await _post(
            "/api/related",
            {
                "tags": [char.get("tag")],
                "limit": _MAX_APPEARANCE + 12,  # 多取一些，抵消 cosplay 等标签被过滤掉的量
                "show_nsfw": show_nsfw,
                "target_categories": ["General", "Copyright"],
            },
            config,
            _RELATED_TIMEOUT,
        )
        related = rel_data.get("results") or []
    except Exception as e:
        logger.warning(f"[danbooru_search] 角色 {char.get('tag')} 共现标签展开失败: {e!r}")

    content = _format_character(query, char, results, related)
    logger.info(
        f"[danbooru_search] character query={query!r} → {char.get('tag')} "
        f"+ {len(related)} 个共现标签 @ {_base_url}"
    )
    return content, []
