import fnmatch
import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from nonebot import logger

_whitelist: set = set()
_index_cache: Optional[Dict] = None


def _load_config_whitelist(config) -> set:
    global _whitelist
    if not _whitelist:
        raw = getattr(config, "NAS_GAME_WHITELIST_GROUPS", [])
        _whitelist = {str(g) for g in raw} if raw else set()
    return _whitelist


def _check_whitelist(config, chat_key: str = "") -> Tuple[bool, str, str]:
    if not chat_key:
        from ..openai_func import TextGenerator
        tg = TextGenerator.instance
        chat_key = getattr(tg, "_current_chat_key", "")
    if not chat_key or not chat_key.startswith("group_"):
        return False, "", "此功能仅限群聊使用"
    group_id = chat_key.split("_", 1)[1]
    whitelist = _load_config_whitelist(config)
    if group_id not in whitelist:
        return False, group_id, "此功能未在此群开启"
    return True, group_id, ""


_BRAND_SCAN_MAX_DEPTH = 5


def _build_index(config) -> Dict:
    root_str = getattr(config, "NAS_GAME_ROOT_PATH", "")
    brands: List[Dict] = []
    if not root_str:
        return {"brands": brands, "index_time": time.time(), "total_games": 0, "root_missing": True}
    root = Path(root_str)
    if not root.exists():
        logger.warning(f"[NAS Game] 根目录不存在: {root}")
        return {"brands": brands, "index_time": time.time(), "total_games": 0, "root_missing": True}

    def _collect_children(dir_path: Path) -> List[Dict]:
        items: List[Dict] = []
        try:
            for child in sorted(dir_path.iterdir()):
                try:
                    stat = child.stat()
                    items.append({
                        "name": child.name,
                        "path": str(child.relative_to(root)).replace("\\", "/"),
                        "type": "dir" if child.is_dir() else "file",
                        "mtime": stat.st_mtime,
                    })
                except Exception:
                    continue
        except Exception as e:
            logger.warning(f"[NAS Game] 无法访问目录 {dir_path}: {e}")
        return items

    def _scan(dir_path: Path, depth: int):
        if depth > _BRAND_SCAN_MAX_DEPTH:
            return
        try:
            subdirs = sorted([e for e in dir_path.iterdir() if e.is_dir()])
        except Exception:
            return

        # 根目录本身不作为会社
        if depth > 0:
            children = _collect_children(dir_path)
            if children:
                brands.append({
                    "name": dir_path.name,
                    "path": str(dir_path.relative_to(root)).replace("\\", "/"),
                    "games": children,
                })

        # 始终递归子目录，确保深层游戏能被扫描到
        for sub in subdirs:
            _scan(sub, depth + 1)

    try:
        _scan(root, 0)
    except Exception as e:
        logger.warning(f"[NAS Game] 无法访问根目录 {root}: {e}")
        return {"brands": brands, "index_time": time.time(), "total_games": 0, "root_missing": True}

    total = sum(len(b["games"]) for b in brands)
    return {"brands": brands, "index_time": time.time(), "total_games": total, "root_missing": False}


def _get_index_file(config) -> Path:
    return Path(config.NG_DATA_PATH) / "nas_game_index.json"


def _load_index(config) -> Dict:
    global _index_cache
    if _index_cache is not None:
        return _index_cache

    index_file = _get_index_file(config)
    if index_file.exists():
        try:
            with open(index_file, "r", encoding="utf-8") as f:
                _index_cache = json.load(f)
            if _index_cache.get("root_missing") or not _index_cache.get("brands"):
                _index_cache = _build_index(config)
                _save_index(config)
            logger.info(
                f"[NAS Game] 已加载缓存索引: {_index_cache.get('total_games', 0)} 个游戏, "
                f"{len(_index_cache.get('brands', []))} 个会社"
            )
            return _index_cache
        except Exception as e:
            logger.warning(f"[NAS Game] 读取索引缓存失败: {e}")

    _index_cache = _build_index(config)
    _save_index(config)
    return _index_cache


def _save_index(config) -> None:
    global _index_cache
    if _index_cache is None:
        return
    index_file = _get_index_file(config)
    os.makedirs(index_file.parent, exist_ok=True)
    try:
        with open(index_file, "w", encoding="utf-8") as f:
            json.dump(_index_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"[NAS Game] 保存索引缓存失败: {e}")


def init_index(config) -> None:
    """启动时预加载索引（后台同步执行，不阻塞启动）。"""
    try:
        idx = _load_index(config)
        logger.info(
            f"[NAS Game] 索引预加载完成: {idx.get('total_games', 0)} 个游戏, "
            f"{len(idx.get('brands', []))} 个会社"
        )
    except Exception as e:
        logger.warning(f"[NAS Game] 索引预加载失败: {e}")


# 自动发现接口
init = init_index


_FILENAME_RE = re.compile(r"^\[([^\]]+)\]\[([^\]]+)\](.*)")


def _read_sync_records(config) -> List[Dict[str, Any]]:
    """读取同步记录，返回最近一批（同一天）的记录。"""
    records_path = getattr(config, "NAS_GAME_SYNC_RECORDS_PATH", "")
    if not records_path:
        return []
    try:
        with open(records_path, "r", encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return []

    if not records:
        return []

    # 按 sync_time 降序排序，取最近一天的
    records.sort(key=lambda r: r.get("sync_time", ""), reverse=True)
    latest_date = records[0].get("sync_time", "")[:10]
    batch = [r for r in records if r.get("sync_time", "").startswith(latest_date)]
    return batch


def _format_sync_records(records: List[Dict]) -> str:
    if not records:
        return ""
    lines = [f"=== 同步服务最近记录（{records[0].get('sync_time', '')[:10]}，{len(records)} 个） ==="]
    for r in records:
        status = "✓" if r.get("status") == "success" else "✗"
        err = f" [{r['error_msg']}]" if r.get("error_msg") else ""
        lines.append(f"  {status} {r.get('filename', '?')}{err}")
    return "\n".join(lines)


def _find_brand_dir(root: Path, brand_name: str) -> Optional[Path]:
    """在合集根目录下递归查找匹配的会社/父目录文件夹（大小写不敏感）。"""
    brand_lower = brand_name.lower()

    def _has_children(p: Path) -> bool:
        try:
            for _ in p.iterdir():
                return True
        except Exception:
            pass
        return False

    # 精确匹配
    try:
        for dir_path in root.rglob("*"):
            if dir_path.is_dir() and dir_path.name.lower() == brand_lower and _has_children(dir_path):
                return dir_path
    except Exception:
        pass

    # 模糊匹配
    try:
        for dir_path in root.rglob("*"):
            if dir_path.is_dir() and brand_lower in dir_path.name.lower() and _has_children(dir_path):
                return dir_path
    except Exception:
        pass

    return None


def _archive_games(config) -> Tuple[List[str], List[str], List[str]]:
    """
    扫描上传目录，将符合格式的游戏归档到合集对应会社文件夹。
    返回 (成功列表, 重复列表, 无匹配列表)。
    """
    upload_str = getattr(config, "NAS_GAME_UPLOAD_PATH", "")
    root_str = getattr(config, "NAS_GAME_ROOT_PATH", "")
    if not upload_str or not root_str:
        return [], [], []
    upload_dir = Path(upload_str)
    root = Path(root_str)

    if not upload_dir.exists():
        return [], [], []
    if not root.exists():
        return [], [], []

    ok_list: List[str] = []
    dup_list: List[str] = []
    no_match_list: List[str] = []

    for entry in upload_dir.iterdir():
        if not entry.is_file():
            continue
        m = _FILENAME_RE.match(entry.name)
        if not m:
            continue

        brand_name = m.group(2).strip()
        brand_dir = _find_brand_dir(root, brand_name)

        if not brand_dir:
            no_match_list.append(f"{entry.name}（会社: {brand_name}）")
            continue

        dest = brand_dir / entry.name
        if dest.exists():
            dup_list.append(f"{entry.name}")
            continue

        try:
            shutil.move(str(entry), str(dest))
            ok_list.append(f"{entry.name} → {brand_dir.name}/")
        except Exception as e:
            no_match_list.append(f"{entry.name}（移动失败: {e}）")

    return ok_list, dup_list, no_match_list


def _format_brands(index: Dict) -> str:
    brands = index.get("brands", [])
    if index.get("root_missing"):
        return "NAS 根目录不存在，无法提供会社列表。"
    if not brands:
        return "暂无会社数据"
    lines = [f"共有 {len(brands)} 个会社:"]
    for b in brands:
        game_count = len(b.get("games", []))
        lines.append(f"- {b['name']}（{game_count} 个游戏）")
    result = "\n".join(lines)
    if len(result) > 6000:
        result = result[:6000] + "\n...（结果过长已截断）"
    return result


def _format_games(brand_name: str, index: Dict) -> str:
    brands = index.get("brands", [])
    entries = _resolve_brand_entries(index, brand_name)
    if entries:
        lines: List[str] = []
        total = 0
        for b in entries:
            games = b.get("games", [])
            total += len(games)
            lines.append(f"会社「{b['name']}」共有 {len(games)} 个游戏:")
            for g in games:
                type_tag = "/" if g.get("type") == "dir" else ""
                mtime_str = time.strftime("%Y-%m-%d", time.localtime(g.get("mtime", 0)))
                lines.append(f"- {g['name']}{type_tag}（{mtime_str}）")
        if not total:
            return f"会社「{brand_name}」下暂无游戏"
        result = "\n".join(lines)
        if len(result) > 6000:
            result = result[:6000] + "\n...（结果过长已截断，可用 search 搜具体游戏）"
        return result

    close_matches = [b["name"] for b in brands if brand_name.lower() in b["name"].lower()]
    if close_matches:
        hints = "、".join(close_matches[:5])
        return f"未找到会社「{brand_name}」，你可能想找: {hints}"
    all_brands = "、".join(b["name"] for b in brands[:10])
    hint = f" 可用会社: {all_brands}" if all_brands else ""
    return f"未找到会社「{brand_name}」。{hint}"


# 中文圈常用会社昵称 → 目录/文件名里会出现的原名（只收无歧义的常见叫法）。
# 群里点游戏基本都用昵称，没有这张表模型要先猜再搜，一个昵称能白烧两轮工具调用。
_BRAND_ALIASES: Dict[str, Tuple[str, ...]] = {
    "音符社": ("ensemble",),
    "柚子社": ("ゆずソフト", "yuzusoft"),
    "八月社": ("AUGUST",),
    "键社": ("KEY",),
    "鍵社": ("KEY",),
    "方糖社": ("Lump of Sugar",),
    "紫社": ("Purple software",),
    "漩涡社": ("Whirlpool",),
    "中二社": ("minori",),
    "马戏团": ("Circus",),
    "马戏团社": ("Circus",),
    "猫猫社": ("ねこねこソフト",),
    "脐社": ("Navel",),
    "钩子社": ("HOOKSOFT",),
    "颜艺社": ("ASa Project",),
    "真红社": ("FAVORITE",),
    "真紅社": ("FAVORITE",),
    "爱丽丝社": ("ALICESOFT",),
    "型月": ("TYPE-MOON",),
    "戏画": ("戯画",),
    "竹子社": ("Atelier Kaguya", "アトリエかぐや"),
    "蜂巢社": ("Alcot",),
    "前翼社": ("FrontWing",),
    "尼特罗": ("Nitro+",),
}

# 文件名的 [日期][会社][标签] 前缀与压缩包扩展名，宽松匹配时去掉
_NAME_PREFIX_RE = re.compile(r'^(?:\s*\[[^\]]*\]\s*)+')
_NAME_EXT_RE = re.compile(r'\.(?:zip|rar|7z|iso|exe|arc|tar|gz|bin|img)$', re.I)


def _norm_game_name(name: str) -> str:
    """去掉 [日期][会社][标签] 前缀、扩展名和空白，小写化，用于宽松比对。"""
    name = _NAME_PREFIX_RE.sub("", name)
    name = _NAME_EXT_RE.sub("", name)
    return re.sub(r"\s+", "", name).lower()


def _resolve_brand_entries(index: Dict, brand: str) -> List[Dict]:
    """会社名解析：精确 → 忽略大小写 → 子串 → 中文昵称别名（别名再走前三步）。返回全部命中条目。"""
    brands = index.get("brands", [])

    def _lookup(name: str) -> List[Dict]:
        exact = [b for b in brands if b["name"] == name]
        if exact:
            return exact
        low = name.lower()
        ci = [b for b in brands if b["name"].lower() == low]
        if ci:
            return ci
        return [b for b in brands if low in b["name"].lower()]

    hits = _lookup(brand)
    if not hits:
        for alias in _BRAND_ALIASES.get(brand.strip(), ()):
            hits = _lookup(alias)
            if hits:
                break
    return hits


def _match_games(brand_entries: List[Dict], game: str) -> List[Tuple[str, Dict]]:
    """游戏名匹配：精确文件名 → 去前缀/扩展名后精确 → 归一化子串 → LIKE 通配。
    模型给的通常是「宿りし乙女の誓いと魔法」这种裸名，而文件叫
    「[250725][ensemble][GPT] 宿りし乙女の誓いと魔法.zip」，只做精确匹配会白跑一轮。"""
    all_games = [(b["name"], g) for b in brand_entries for g in b.get("games", [])]
    exact = [(bn, g) for bn, g in all_games if g["name"] == game]
    if exact:
        return exact
    key = _norm_game_name(game)
    if not key:
        return []
    norm_exact = [(bn, g) for bn, g in all_games if _norm_game_name(g["name"]) == key]
    if norm_exact:
        return norm_exact
    sub = [(bn, g) for bn, g in all_games if key in _norm_game_name(g["name"])]
    if sub:
        return sub
    return [(bn, g) for bn, g in all_games if _match_like(g["name"], game)]


def _match_like(name: str, pattern: str) -> bool:
    """SQL LIKE 风格匹配：% 匹配任意字符串，_ 匹配单个字符，不区分大小写。"""
    if "%" in pattern or "_" in pattern:
        # 将 % 转为 *，_ 转为 ?，然后用 fnmatch 匹配
        fn_pattern = pattern.replace("%", "*").replace("_", "?")
        return fnmatch.fnmatch(name.lower(), fn_pattern.lower())
    # 无通配符时回退为子串匹配
    return pattern.lower() in name.lower()


def _search_games(keyword: str, index: Dict) -> str:
    # 昵称展开：search 音符社 → 同时按 ensemble 匹配；关键词命中游戏名或会社名都算
    keywords = [keyword] + list(_BRAND_ALIASES.get(keyword.strip(), ()))
    matches: List[Tuple[str, Dict]] = []
    for brand in index.get("brands", []):
        brand_hit = any(_match_like(brand["name"], kw) for kw in keywords)
        for game in brand.get("games", []):
            if brand_hit or any(_match_like(game["name"], kw) for kw in keywords):
                matches.append((brand["name"], game))

    if not matches:
        return f"未找到匹配「{keyword}」的游戏（支持 % 匹配任意字符，_ 匹配单个字符）"

    lines = [f"搜索「{keyword}」找到 {len(matches)} 个结果:"]
    for brand_name, game in matches:
        type_tag = "/" if game.get("type") == "dir" else ""
        lines.append(f"- [{brand_name}] {game['name']}{type_tag}  path={game['path']}")
    result = "\n".join(lines)
    if len(result) > 6000:
        result = result[:6000] + f"\n...（结果过多共 {len(matches)} 条已截断，请缩小搜索范围）"
    return result


def _format_recent_updates(index: Dict, top_n: int = 10) -> str:
    """提取最近更新的 N 项文件。"""
    items: List[Tuple[float, str, str]] = []
    for brand in index.get("brands", []):
        for game in brand.get("games", []):
            mtime = game.get("mtime", 0)
            items.append((mtime, brand["name"], game["name"]))
    items.sort(key=lambda x: x[0], reverse=True)
    recent = items[:top_n]
    if not recent:
        return "暂无更新数据"
    lines = [f"最近更新（共 {len(items)} 项，展示前 {len(recent)} 项）:"]
    for mtime, brand_name, game_name in recent:
        time_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))
        lines.append(f"- [{brand_name}] {game_name}（{time_str}）")
    return "\n".join(lines)


def _get_download_url(config, index: Dict, brand: str = "", game: str = "", path: str = "") -> str:
    base_url = getattr(config, "NAS_GAME_BASE_URL", "")
    if not base_url:
        return "NAS_GAME_BASE_URL 未配置，无法生成下载链接"
    base_url = base_url.rstrip("/")

    def _link(rel_path: str) -> str:
        return f"{base_url}/{rel_path.replace(chr(92), '/').lstrip('/')}"

    if path:
        return f"下载链接（请单独一行展示给用户）:\n{_link(path)}"
    if not (brand and game):
        return "请提供 会社名+游戏名 或直接提供游戏路径"

    brand_entries = _resolve_brand_entries(index, brand)
    if not brand_entries:
        return f"未找到会社「{brand}」，可用 search 按游戏名关键词直接搜（keyword 参数）"
    matches = _match_games(brand_entries, game)
    if not matches:
        # 会社内没有：跨会社兜底一次，会社写错也不用再跑一轮
        matches = _match_games(index.get("brands", []), game)
    if not matches:
        return (
            f"未找到游戏「{game}」（会社: {brand}），"
            "可用 list_games 查看该会社的完整文件名，或用 search 搜关键词"
        )
    seen = set()
    uniq: List[Tuple[str, Dict]] = []
    for bn, g in matches:
        if g["path"] in seen:
            continue
        seen.add(g["path"])
        uniq.append((bn, g))
    if len(uniq) == 1:
        return f"下载链接（请单独一行展示给用户）:\n{_link(uniq[0][1]['path'])}"
    if len(uniq) <= 3:
        # 同一游戏常有 原版/汉化/GPT 几个包，一次全给，省得再点一轮
        lines = [f"匹配到 {len(uniq)} 个文件，下载链接（每个链接单独一行展示给用户）:"]
        for bn, g in uniq:
            lines.append(f"- [{bn}] {g['name']}\n  {_link(g['path'])}")
        return "\n".join(lines)
    lines = [f"「{game}」匹配到 {len(uniq)} 个文件，请用 path 参数指定其中一个:"]
    for bn, g in uniq[:20]:
        lines.append(f"- [{bn}] {g['name']}  path={g['path']}")
    return "\n".join(lines)


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    allowed, group_id, msg = _check_whitelist(config)
    if not allowed:
        return msg, []

    index = _load_index(config)
    action = str(args.get("action", "")).strip()

    if action == "list_brands":
        return _format_brands(index), []
    elif action == "list_games":
        brand = str(args.get("brand", "")).strip()
        if not brand:
            return "请提供会社名称（先用 list_brands 查看可用会社）", []
        return _format_games(brand, index), []
    elif action == "search":
        keyword = str(args.get("keyword", "")).strip()
        if not keyword:
            return "请提供搜索关键词", []
        return _search_games(keyword, index), []
    elif action == "get_download":
        path = str(args.get("path", "")).strip()
        brand = str(args.get("brand", "")).strip()
        game = str(args.get("game", "")).strip()
        if not path and not (brand and game):
            return "请提供 会社名(game 参数) + 游戏名(brand 参数)，或直接提供路径(path 参数)", []
        return _get_download_url(config, index, brand=brand, game=game, path=path), []
    elif action == "refresh_index":
        global _index_cache

        # 先归档上传目录中的游戏
        ok_list, dup_list, no_match_list = _archive_games(config)

        archive_lines: List[str] = []
        if ok_list or dup_list or no_match_list:
            archive_lines.append("=== 归档结果 ===")
            if ok_list:
                archive_lines.append(f"成功归档（{len(ok_list)} 个）:")
                for item in ok_list:
                    archive_lines.append(f"  {item}")
            if dup_list:
                archive_lines.append(f"跳过-重名（{len(dup_list)} 个）:")
                for item in dup_list:
                    archive_lines.append(f"  {item}")
            if no_match_list:
                archive_lines.append(f"跳过-无匹配会社（{len(no_match_list)} 个）:")
                for item in no_match_list:
                    archive_lines.append(f"  {item}")
            archive_lines.append("")

        # 重建索引
        _index_cache = _build_index(config)
        _save_index(config)
        total = _index_cache.get('total_games', 0)
        brands_count = len(_index_cache.get('brands', []))
        summary = f"索引已刷新：{total} 个游戏, {brands_count} 个会社"

        try:
            n = int(args.get("n", 10))
        except (TypeError, ValueError):
            n = 10
        n = max(1, min(n, 50))
        recent = _format_recent_updates(_index_cache, top_n=n)

        parts = archive_lines + [summary, recent]

        # 读取同步服务最近记录
        sync_records = _read_sync_records(config)
        sync_text = _format_sync_records(sync_records)
        if sync_text:
            parts.append(sync_text)

        return "\n".join(parts), []
    else:
        return f"未知操作: {action}，可用: list_brands, list_games, search, get_download, refresh_index", []


schema = {
    "type": "function",
    "function": {
        "name": "nas_game_list",
        "description": (
            "查询 NAS 上的 Galgame 合集目录。用法：\n"
            "- 找某个游戏：直接 search（keyword 写游戏名的一部分即可，日文/中文都行），结果自带 path，可直接用于 get_download\n"
            "- 找某会社的作品：直接 list_games（brand 支持原名或常见中文昵称，如 音符社=ensemble、柚子社=ゆずソフト），"
            "不必先 list_brands\n"
            "- get_download 的 game 只写游戏名即可，不用带 [日期][会社] 前缀和扩展名；同名多个包会一次返回全部链接\n"
            "- 返回链接时放在单独一行以便用户复制；不要虚构或猜测任何游戏信息，严格根据工具返回的数据回复。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list_brands", "list_games", "search", "get_download", "refresh_index"],
                    "description": (
                        "操作类型：list_brands=列出所有会社, list_games=列出某会社游戏(需brand参数), "
                        "search=搜索游戏(需keyword参数), get_download=获取下载链接(需brand+game或path), "
                        "refresh_index=归档上传目录游戏到合集并刷新索引(n参数控制条数，默认10)"
                    ),
                },
                "brand": {
                    "type": "string",
                    "description": "会社名，用于 list_games 和 get_download；支持原名或常见中文昵称（音符社/柚子社/八月社/键社/方糖社 等）",
                },
                "game": {
                    "type": "string",
                    "description": "游戏名，用于 get_download（需配合 brand）；可只写名字本身，不需要 [日期][会社] 前缀和扩展名",
                },
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词，支持 LIKE 风格通配符：%匹配任意字符，_匹配单个字符，如 %恋% 或 *gal*",
                },
                "path": {
                    "type": "string",
                    "description": "游戏相对路径，用于 get_download（替代 brand+game）",
                },
                "n": {
                    "type": "integer",
                    "description": "refresh_index 时返回的最近更新条数，默认 10，最大 50",
                },
            },
            "required": ["action"],
        },
    },
}
