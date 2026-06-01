from typing import Any, Dict, List, Tuple

import httpx

# Bangumi API 配置
BANGUMI_API_BASE = "https://api.bgm.tv"


def should_load(config) -> bool:
    """有非空 BANGUMI_ACCESS_TOKEN 时才加载。"""
    return bool(getattr(config, "BANGUMI_ACCESS_TOKEN", None))

# 条目类型映射
SUBJECT_TYPE_MAP = {
    "book": 1,      # 书籍
    "anime": 2,     # 动画
    "music": 3,     # 音乐
    "game": 4,      # 游戏
    "real": 6,      # 现实
}

SUBJECT_TYPE_NAME = {1: "书籍", 2: "动画", 3: "音乐", 4: "游戏", 6: "现实", 0: "其他"}


def _calc_rating_stats(count_dict: Dict[str, int]) -> Dict[str, Any]:
    """从评分分布计算统计量：中位数、四分位距均值、标准差。"""
    if not count_dict:
        return {}
    
    # 展开成列表
    scores = []
    for score_str, count in count_dict.items():
        try:
            score = int(score_str)
            scores.extend([score] * count)
        except (ValueError, TypeError):
            continue
    
    if not scores:
        return {}
    
    scores.sort()
    n = len(scores)
    
    # 中位数
    if n % 2 == 0:
        median = (scores[n // 2 - 1] + scores[n // 2]) / 2
    else:
        median = scores[n // 2]
    
    # 四分位距均值 (IQM)：去掉最低25%和最高25%，对中间50%求平均
    q1_idx = n // 4
    q3_idx = n * 3 // 4
    iqm_scores = scores[q1_idx:q3_idx]
    iqm = sum(iqm_scores) / len(iqm_scores) if iqm_scores else median
    
    # 标准差
    mean = sum(scores) / n
    variance = sum((x - mean) ** 2 for x in scores) / n
    std_dev = variance ** 0.5
    
    return {
        "median": round(median, 1),
        "iqm": round(iqm, 1),
        "std_dev": round(std_dev, 2),
        "total": n,
    }


# ============ 工具 Schema 定义 ============

# 搜索条目
search_subject_schema = {
    "type": "function",
    "function": {
        "name": "bangumi_search_subject",
        "description": "在 Bangumi 搜索动画、漫画、游戏、音乐、书籍等条目。返回匹配的条目列表（含ID、名称、评分、简介等）。如需更多信息（角色、关联作品等），用返回的 ID 调用 bangumi_get_subject。按会社搜索示例：keyword留空，tags设为['Key']可搜索Key社游戏。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（按会社搜索时可留空）",
                },
                "type": {
                    "type": "string",
                    "enum": ["anime", "book", "music", "game", "real"],
                    "description": "条目类型筛选：anime=动画, book=书籍/漫画, music=音乐, game=游戏, real=真人/三次元",
                },
                "sort": {
                    "type": "string",
                    "enum": ["match", "heat", "rank", "score"],
                    "description": "排序方式：match=匹配度(默认), heat=收藏人数, rank=排名, score=评分",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "标签列表（且关系），如 ['治愈系','日常'] 或 ['Key'] 搜索Key社游戏",
                },
                "air_date": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "播出/发售日期范围（且关系），格式 YYYY-MM-DD，如 ['>=2024-07-01','<2024-10-01'] 表示2024年7月新番",
                },
                "limit": {
                    "type": "integer",
                    "description": "返回数量上限，默认10",
                },
            },
            "required": [],
        },
    },
}

# 获取条目详情
get_subject_schema = {
    "type": "function",
    "function": {
        "name": "bangumi_get_subject",
        "description": "通过 ID 获取 Bangumi 条目的详细信息，包括角色、制作人员、关联作品等。",
        "parameters": {
            "type": "object",
            "properties": {
                "subject_id": {
                    "type": "integer",
                    "description": "条目 ID（从 bangumi_search_subject 返回结果中获取）",
                },
            },
            "required": ["subject_id"],
        },
    },
}

# 搜索角色（虚拟角色，直接返回出演作品）
search_character_schema = {
    "type": "function",
    "function": {
        "name": "bangumi_search_character",
        "description": "在 Bangumi 搜索虚拟角色（动画、游戏、漫画中的角色）。返回匹配的角色列表，包含出演作品和声优信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（角色名）",
                },
                "nsfw": {
                    "type": "boolean",
                    "description": "是否包含NSFW角色，默认false（不包含）",
                },
            },
            "required": ["keyword"],
        },
    },
}

# 搜索人物（制作人员、声优等，直接返回参与作品）
search_person_schema = {
    "type": "function",
    "function": {
        "name": "bangumi_search_person",
        "description": "在 Bangumi 搜索现实人物（制作人员、声优、导演、编剧等）。返回匹配的人物列表，包含参与作品信息。",
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {
                    "type": "string",
                    "description": "搜索关键词（人物名）",
                },
                "careers": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "职业筛选（且关系），如 ['artist','director']",
                },
            },
            "required": ["keyword"],
        },
    },
}

# 每日放送
calendar_schema = {
    "type": "function",
    "function": {
        "name": "bangumi_calendar",
        "description": "获取 Bangumi 每日放送列表，返回本周每天的动画放送信息（含名称、评分、放送日期等）。",
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}


# 自动发现接口：导出工具列表
# run 函数在文件末尾定义，这里用延迟引用
def _get_run_functions():
    from . import bangumi_search as _self
    return {
        "bangumi_search_subject": _self.run_search_subject,
        "bangumi_get_subject": _self.run_get_subject,
        "bangumi_search_character": _self.run_search_character,
        "bangumi_search_person": _self.run_search_person,
        "bangumi_calendar": _self.run_calendar,
    }


def get_tools():
    """返回 (name, schema, run) 列表，供自动发现使用。"""
    runs = _get_run_functions()
    return [
        ("bangumi_search_subject", search_subject_schema, runs["bangumi_search_subject"]),
        ("bangumi_get_subject", get_subject_schema, runs["bangumi_get_subject"]),
        ("bangumi_search_character", search_character_schema, runs["bangumi_search_character"]),
        ("bangumi_search_person", search_person_schema, runs["bangumi_search_person"]),
        ("bangumi_calendar", calendar_schema, runs["bangumi_calendar"]),
    ]


# ============ API 请求 ============

async def _api_request(method: str, path: str, token: str, json_data: Dict[str, Any] = None, params: Dict[str, Any] = None, proxy: str = None) -> Any:
    """Make a request to Bangumi API. Falls back to unauthenticated request if token is invalid."""
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "nonebot-plugin-naturel-gpt/NaturelGPT (https://github.com/topics/nonebot-plugin)",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    
    async with httpx.AsyncClient(proxy=proxy or None, timeout=30) as client:
        if method.upper() == "POST":
            resp = await client.post(f"{BANGUMI_API_BASE}{path}", json=json_data, headers=headers, params=params)
        else:
            resp = await client.get(f"{BANGUMI_API_BASE}{path}", headers=headers, params=params)
        
        # If 401 and we had a token, retry without token
        if resp.status_code == 401 and token:
            headers.pop("Authorization", None)
            if method.upper() == "POST":
                resp = await client.post(f"{BANGUMI_API_BASE}{path}", json=json_data, headers=headers, params=params)
            else:
                resp = await client.get(f"{BANGUMI_API_BASE}{path}", headers=headers, params=params)
        
        resp.raise_for_status()
        return resp.json()


# ============ 格式化函数 ============

def _format_subject_brief(subject: Dict[str, Any]) -> str:
    """格式化搜索结果中的条目简要信息。"""
    name = subject.get("name", "未知")
    name_cn = subject.get("name_cn", "")
    subject_id = subject.get("id")
    summary = subject.get("summary", "")
    subject_type = subject.get("type", 0)
    date = subject.get("date", "")
    platform = subject.get("platform", "")
    tags = [t.get("name", "") for t in (subject.get("tags") or [])[:5]]
    
    # rating 对象里有 score 和 rank
    rating = subject.get("rating") or {}
    score = rating.get("score")
    rank = rating.get("rank")
    count_dict = rating.get("count") or {}
    stats = _calc_rating_stats(count_dict)
    
    # 收藏信息
    collection = subject.get("collection") or {}
    wish = collection.get("wish", 0)
    collect = collection.get("collect", 0)
    doing = collection.get("doing", 0)
    
    type_name = SUBJECT_TYPE_NAME.get(subject_type, "")
    
    # 标题
    title = f"[{subject_id}] {name}"
    if name_cn and name_cn != name:
        title += f" ({name_cn})"
    
    lines = [title]
    
    # 基本信息行
    info_parts = []
    if type_name:
        info_parts.append(type_name)
    if platform:
        info_parts.append(platform)
    if date:
        info_parts.append(date)
    if info_parts:
        lines.append("  " + " | ".join(info_parts))
    
    # 评分排名
    rating_parts = []
    if score:
        rating_parts.append(f"⭐{score}")
    if rank:
        rating_parts.append(f"#{rank}")
    if stats.get("iqm"):
        rating_parts.append(f"IQM{stats['iqm']}")
    if stats.get("std_dev"):
        rating_parts.append(f"σ={stats['std_dev']}")
    if rating_parts:
        lines.append("  " + " | ".join(rating_parts))
    
    # 标签
    if tags:
        lines.append(f"  标签: {', '.join(tags)}")
    
    # 收藏数
    if wish or collect or doing:
        lines.append(f"  想看:{wish} | 看过:{collect} | 在看:{doing}")
    
    # 简介
    if summary:
        brief = summary[:100] + "..." if len(summary) > 100 else summary
        lines.append(f"  {brief}")
    
    return "\n".join(lines)


def _format_subject_detail(subject: Dict[str, Any], details: Dict[str, Any]) -> str:
    """格式化条目详细信息。"""
    name = subject.get("name", "未知")
    name_cn = subject.get("name_cn", "")
    summary = subject.get("summary", "")
    subject_type = subject.get("type", 0)
    date = subject.get("date", "")
    platform = subject.get("platform", "")
    tags = [t.get("name", "") for t in (subject.get("tags") or [])[:8]]
    
    # rating 对象里有 score 和 rank
    rating = subject.get("rating") or {}
    score = rating.get("score")
    rank = rating.get("rank")
    total = rating.get("total", 0)
    count_dict = rating.get("count") or {}
    stats = _calc_rating_stats(count_dict)
    
    # 收藏信息
    collection = subject.get("collection") or {}
    wish = collection.get("wish", 0)
    collect = collection.get("collect", 0)
    doing = collection.get("doing", 0)
    
    # infobox 信息
    infobox = subject.get("infobox") or []
    infobox_dict = {}
    for item in infobox:
        key = item.get("key", "")
        value = item.get("value")
        if key and value:
            if isinstance(value, list):
                # 处理多值情况
                vals = [v.get("v", "") if isinstance(v, dict) else str(v) for v in value]
                infobox_dict[key] = ", ".join(filter(None, vals))
            else:
                infobox_dict[key] = str(value)
    
    type_name = SUBJECT_TYPE_NAME.get(subject_type, "")
    
    # 标题
    title = f"【{name}】"
    if name_cn and name_cn != name:
        title += f" {name_cn}"
    
    lines = [title]
    
    # 基本信息
    info_parts = []
    if type_name:
        info_parts.append(type_name)
    if platform:
        info_parts.append(platform)
    if date:
        info_parts.append(date)
    if info_parts:
        lines.append(" | ".join(info_parts))
    
    # 评分排名
    rating_parts = []
    if score:
        rating_parts.append(f"⭐{score}")
    if rank:
        rating_parts.append(f"#{rank}")
    if total:
        rating_parts.append(f"{total}人评分")
    if stats.get("iqm"):
        rating_parts.append(f"IQM{stats['iqm']}")
    if stats.get("std_dev"):
        rating_parts.append(f"σ={stats['std_dev']}")
    if rating_parts:
        lines.append(" | ".join(rating_parts))
    
    # 标签
    if tags:
        lines.append(f"标签: {', '.join(tags)}")
    
    # 收藏数
    if wish or collect or doing:
        lines.append(f"想看:{wish} | 看过:{collect} | 在看:{doing}")
    
    # infobox 信息（制作人员、原作等）
    if infobox_dict:
        for key, value in infobox_dict.items():
            if key in ["导演", "原作", "脚本", "音乐", "人物设定", "动画制作", "开发", "发行"]:
                lines.append(f"{key}: {value}")
    
    # 简介
    if summary:
        brief = summary[:200] + "..." if len(summary) > 200 else summary
        lines.append(f"简介: {brief}")
    
    # 详情信息
    if details:
        # 角色
        characters = details.get("characters", [])
        if characters:
            char_parts = []
            for c in characters[:6]:
                c_name = c.get("name", "")
                actors = c.get("actors", [])
                cv = actors[0].get("name", "") if actors else ""
                if c_name:
                    char_parts.append(f"{c_name}(CV:{cv})" if cv else c_name)
            if char_parts:
                lines.append(f"角色: {', '.join(char_parts)}")
        
        # 关联作品
        relations = details.get("relations", [])
        if relations:
            rel_parts = []
            for r in relations[:5]:
                r_name = r.get("name", "")
                r_type = r.get("relation", "")
                if r_name:
                    rel_parts.append(f"{r_name}({r_type})" if r_type else r_name)
            if rel_parts:
                lines.append(f"关联: {', '.join(rel_parts)}")
    
    return "\n".join(lines)


def _format_character_brief(character: Dict[str, Any], subjects: List[Dict] = None, persons: List[Dict] = None) -> str:
    """格式化搜索结果中的角色简要信息，包含出演作品和声优。"""
    name = character.get("name", "未知")
    character_id = character.get("id")
    summary = character.get("summary", "")
    gender = character.get("gender", "")
    
    # infobox 信息
    infobox = character.get("infobox") or []
    infobox_dict = {}
    for item in infobox:
        key = item.get("key", "")
        value = item.get("value")
        if key and value:
            if isinstance(value, list):
                vals = [v.get("v", "") if isinstance(v, dict) else str(v) for v in value]
                infobox_dict[key] = ", ".join(filter(None, vals))
            else:
                infobox_dict[key] = str(value)
    
    lines = [f"[{character_id}] {name}"]
    
    # 基本信息
    info_parts = []
    if gender:
        gender_str = "男" if gender == "male" else "女" if gender == "female" else gender
        info_parts.append(gender_str)
    if infobox_dict.get("别名"):
        info_parts.append(f"别名: {infobox_dict['别名']}")
    if info_parts:
        lines.append("  " + " | ".join(info_parts))
    
    # 声优信息
    if persons:
        cv_parts = []
        for p in persons[:2]:
            p_name = p.get("name", "")
            p_relation = p.get("relation", "")
            if p_name and "声优" in p_relation:
                cv_parts.append(p_name)
        if cv_parts:
            lines.append(f"  声优: {', '.join(cv_parts)}")
    
    # 出演作品
    if subjects:
        subj_parts = []
        for s in subjects[:5]:
            s_name = s.get("name", "")
            s_relation = s.get("relation", "")
            if s_name:
                subj_parts.append(f"{s_name}({s_relation})" if s_relation else s_name)
        if subj_parts:
            lines.append(f"  出演: {', '.join(subj_parts)}")
    
    # 简介
    if summary:
        brief = summary[:80] + "..." if len(summary) > 80 else summary
        lines.append(f"  {brief}")
    
    return "\n".join(lines)


def _format_person_brief(person: Dict[str, Any], subjects: List[Dict] = None) -> str:
    """格式化搜索结果中的人物简要信息，包含参与作品。"""
    name = person.get("name", "未知")
    person_id = person.get("id")
    summary = person.get("summary", "")
    gender = person.get("gender", "")
    
    # infobox 信息
    infobox = person.get("infobox") or []
    infobox_dict = {}
    for item in infobox:
        key = item.get("key", "")
        value = item.get("value")
        if key and value:
            if isinstance(value, list):
                vals = [v.get("v", "") if isinstance(v, dict) else str(v) for v in value]
                infobox_dict[key] = ", ".join(filter(None, vals))
            else:
                infobox_dict[key] = str(value)
    
    lines = [f"[{person_id}] {name}"]
    
    # 基本信息
    info_parts = []
    if gender:
        gender_str = "男" if gender == "male" else "女" if gender == "female" else gender
        info_parts.append(gender_str)
    if infobox_dict.get("别名"):
        info_parts.append(f"别名: {infobox_dict['别名']}")
    if info_parts:
        lines.append("  " + " | ".join(info_parts))
    
    # 参与作品（按职位分组）
    if subjects:
        by_position: Dict[str, list] = {}
        for s in subjects:
            s_name = s.get("name", "")
            staff = s.get("staff") or {}
            position = ""
            if isinstance(staff, dict):
                position = staff.get("position", "")
            if not position:
                position = s.get("relation", "") or "参与"
            if position not in by_position:
                by_position[position] = []
            by_position[position].append(s_name)
        
        # 显示主要职位
        for position, works in list(by_position.items())[:2]:
            work_list = ", ".join(works[:4])
            if len(works) > 4:
                work_list += f" 等{len(works)}部"
            lines.append(f"  {position}: {work_list}")
    
    # 简介
    if summary:
        brief = summary[:80] + "..." if len(summary) > 80 else summary
        lines.append(f"  {brief}")
    
    return "\n".join(lines)


# ============ 工具执行函数 ============

async def run_search_subject(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """搜索条目。"""
    keyword = str(args.get("keyword") or "").strip()
    type_filter = str(args.get("type") or "").strip()
    sort = str(args.get("sort") or "").strip()
    tags = args.get("tags") or []
    air_date = args.get("air_date") or []
    limit = args.get("limit")

    if not keyword and not tags:
        return "请输入搜索关键词或标签。", []

    token = getattr(config, "BANGUMI_ACCESS_TOKEN", "")
    proxy = getattr(config, "TOOL_PROXY", "") or None
    
    try:
        # 构建搜索参数
        payload: Dict[str, Any] = {"keyword": keyword}
        if sort and sort in ("match", "heat", "rank", "score"):
            payload["sort"] = sort

        filter_obj: Dict[str, Any] = {}
        if type_filter and type_filter in SUBJECT_TYPE_MAP:
            filter_obj["type"] = [SUBJECT_TYPE_MAP[type_filter]]
        if tags and isinstance(tags, list):
            filter_obj["tag"] = [str(t) for t in tags if t]
        if air_date and isinstance(air_date, list):
            filter_obj["air_date"] = [str(d) for d in air_date if d]
        if filter_obj:
            payload["filter"] = filter_obj

        params = {}
        if isinstance(limit, int) and limit > 0:
            params["limit"] = min(limit, 50)

        data = await _api_request("POST", "/v0/search/subjects", token, payload, params=params, proxy=proxy)
        
        subjects = data.get("data", [])
        if not subjects:
            return f"在 Bangumi 上没有找到「{keyword}」的相关条目。", []
        
        # 格式化结果列表
        results = [_format_subject_brief(s) for s in subjects[:5]]
        
        response = f"找到 {len(subjects)} 个条目，显示前 {len(results)} 个：\n\n"
        response += "\n\n".join(results)
        response += "\n\n如需查看详情，请使用 bangumi_get_subject 并传入对应的 ID。"
        
        return response, []
        
    except httpx.HTTPStatusError as e:
        return f"Bangumi API 请求失败: {e.response.status_code}", []
    except Exception as e:
        return f"搜索出错: {e!r}", []


async def run_get_subject(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """获取条目详情。"""
    subject_id = args.get("subject_id")
    if not subject_id:
        return "请提供条目 ID。", []
    
    token = getattr(config, "BANGUMI_ACCESS_TOKEN", "")
    proxy = getattr(config, "TOOL_PROXY", "") or None
    
    try:
        # 获取条目基本信息
        subject = await _api_request("GET", f"/v0/subjects/{subject_id}", token, proxy=proxy)
        
        # 并行获取关联信息
        persons = []
        characters = []
        relations = []
        
        try:
            persons = await _api_request("GET", f"/v0/subjects/{subject_id}/persons", token, proxy=proxy)
        except Exception:
            pass
        
        try:
            characters = await _api_request("GET", f"/v0/subjects/{subject_id}/characters", token, proxy=proxy)
        except Exception:
            pass
        
        try:
            relations = await _api_request("GET", f"/v0/subjects/{subject_id}/subjects", token, proxy=proxy)
        except Exception:
            pass
        
        details = {
            "persons": persons,
            "characters": characters,
            "relations": relations,
        }
        
        return _format_subject_detail(subject, details), []
        
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return f"未找到 ID 为 {subject_id} 的条目。", []
        return f"获取详情失败: {e.response.status_code}", []
    except Exception as e:
        return f"获取详情出错: {e!r}", []


async def run_search_character(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """搜索角色，直接展开出演作品和声优信息。"""
    keyword = str(args.get("keyword") or "").strip()
    nsfw = args.get("nsfw")
    if not keyword:
        return "请输入搜索关键词。", []

    token = getattr(config, "BANGUMI_ACCESS_TOKEN", "")
    proxy = getattr(config, "TOOL_PROXY", "") or None

    try:
        payload: Dict[str, Any] = {"keyword": keyword}
        if nsfw is not None:
            payload["filter"] = {"nsfw": bool(nsfw)}
        data = await _api_request("POST", "/v0/search/characters", token, payload, proxy=proxy)

        characters = data.get("data", [])
        if not characters:
            return f"在 Bangumi 上没有找到「{keyword}」的相关角色。", []

        # 获取每个角色的出演作品和声优信息
        results = []
        for c in characters[:5]:
            char_id = c.get("id")
            subjects = []
            persons = []
            if char_id:
                try:
                    subjects = await _api_request("GET", f"/v0/characters/{char_id}/subjects", token, proxy=proxy)
                except Exception:
                    pass
                try:
                    persons = await _api_request("GET", f"/v0/characters/{char_id}/persons", token, proxy=proxy)
                except Exception:
                    pass
            results.append(_format_character_brief(c, subjects, persons))

        response = f"找到 {len(characters)} 个角色，显示前 {len(results)} 个：\n\n"
        response += "\n\n".join(results)

        return response, []

    except httpx.HTTPStatusError as e:
        return f"Bangumi API 请求失败: {e.response.status_code}", []
    except Exception as e:
        return f"搜索出错: {e!r}", []


async def run_search_person(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """搜索人物，直接展开参与作品。"""
    keyword = str(args.get("keyword") or "").strip()
    careers = args.get("careers") or []
    if not keyword:
        return "请输入搜索关键词。", []

    token = getattr(config, "BANGUMI_ACCESS_TOKEN", "")
    proxy = getattr(config, "TOOL_PROXY", "") or None

    try:
        payload: Dict[str, Any] = {"keyword": keyword}
        if careers and isinstance(careers, list):
            payload["filter"] = {"career": [str(c) for c in careers if c]}
        data = await _api_request("POST", "/v0/search/persons", token, payload, proxy=proxy)

        persons = data.get("data", [])
        if not persons:
            return f"在 Bangumi 上没有找到「{keyword}」的相关人物。", []

        # 获取每个人物的参与作品
        results = []
        for p in persons[:5]:
            person_id = p.get("id")
            subjects = []
            if person_id:
                try:
                    subjects = await _api_request("GET", f"/v0/persons/{person_id}/subjects", token, proxy=proxy)
                except Exception:
                    pass
            results.append(_format_person_brief(p, subjects))

        response = f"找到 {len(persons)} 个人物，显示前 {len(results)} 个：\n\n"
        response += "\n\n".join(results)

        return response, []

    except httpx.HTTPStatusError as e:
        return f"Bangumi API 请求失败: {e.response.status_code}", []
    except Exception as e:
        return f"搜索出错: {e!r}", []


WEEKDAY_CN = {0: "周一", 1: "周二", 2: "周三", 3: "周四", 4: "周五", 5: "周六", 6: "周日"}


async def run_calendar(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    """获取每日放送。"""
    token = getattr(config, "BANGUMI_ACCESS_TOKEN", "")
    proxy = getattr(config, "TOOL_PROXY", "") or None

    try:
        data = await _api_request("GET", "/calendar", token, proxy=proxy)

        lines = ["本周放送："]
        for day in data:
            weekday = day.get("weekday", {})
            day_name = weekday.get("cn") or WEEKDAY_CN.get(weekday.get("id", 0), "")
            items = day.get("items", [])
            if not items:
                continue
            day_lines = []
            for s in items[:8]:
                name = s.get("name", "")
                name_cn = s.get("name_cn", "")
                rating = (s.get("rating") or {}).get("score")
                air_date = s.get("air_date", "")
                display = name_cn if name_cn else name
                parts = [display]
                if rating:
                    parts.append(f"⭐{rating}")
                if air_date:
                    parts.append(air_date)
                day_lines.append(f"  [{s.get('id')}] {' | '.join(parts)}")
            if day_lines:
                lines.append(f"\n{day_name}（{len(items)}部）：")
                lines.extend(day_lines)

        return "\n".join(lines), []

    except httpx.HTTPStatusError as e:
        return f"Bangumi API 请求失败: {e.response.status_code}", []
    except Exception as e:
        return f"获取放送出错: {e!r}", []