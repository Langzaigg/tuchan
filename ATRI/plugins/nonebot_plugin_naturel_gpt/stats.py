"""插件运行统计模块。

按日期分桶，记录：
- 触发回复次数
- 各模型 token 消耗（prompt / completion / cached）
- 各工具调用次数

数据按当日累积，每日刷新（仅保留当天数据用于 rg stat 展示）。
持久化到 data/naturel_gpt/stats.json，重启不丢失。
"""
import json
import os
import threading
from collections import defaultdict
from datetime import date
from typing import Any, Dict, Optional

from .config import config
from .logger import logger

_STATS_FILE = os.path.join(config.NG_DATA_PATH, "stats.json")
_LOCK = threading.Lock()


def _today_str() -> str:
    return date.today().isoformat()


def _empty_day() -> Dict[str, Any]:
    return {
        "trigger_count": 0,
        "models": defaultdict(lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "requests": 0,
        }),
        "tools": defaultdict(int),
    }


class StatsManager:
    """统计单例。内存维护，落盘持久化。"""

    def __init__(self) -> None:
        self._days: Dict[str, Dict[str, Any]] = {}
        self._load()

    # ---- 持久化 ----
    def _load(self) -> None:
        try:
            if os.path.exists(_STATS_FILE):
                with open(_STATS_FILE, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                for day_key, day_data in (raw or {}).items():
                    day = _empty_day()
                    day["trigger_count"] = day_data.get("trigger_count", 0)
                    for model, mdata in (day_data.get("models") or {}).items():
                        day["models"][model] = {
                            "prompt_tokens": mdata.get("prompt_tokens", 0),
                            "completion_tokens": mdata.get("completion_tokens", 0),
                            "total_tokens": mdata.get("total_tokens", 0),
                            "cached_tokens": mdata.get("cached_tokens", 0),
                            "requests": mdata.get("requests", 0),
                        }
                    for tool, cnt in (day_data.get("tools") or {}).items():
                        day["tools"][tool] = cnt
                    self._days[day_key] = day
        except Exception as e:
            logger.warning(f"[stats] 加载统计文件失败，从空开始: {e!r}")
            self._days = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(_STATS_FILE), exist_ok=True)
            serializable = {}
            for day_key, day in self._days.items():
                serializable[day_key] = {
                    "trigger_count": day["trigger_count"],
                    "models": {k: dict(v) for k, v in day["models"].items()},
                    "tools": dict(day["tools"]),
                }
            tmp = _STATS_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(serializable, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _STATS_FILE)
        except Exception as e:
            logger.warning(f"[stats] 保存统计文件失败: {e!r}")

    def _get_day(self, day_key: Optional[str] = None) -> Dict[str, Any]:
        key = day_key or _today_str()
        if key not in self._days:
            self._days[key] = _empty_day()
        return self._days[key]

    # ---- 采集接口 ----
    def inc_trigger(self) -> None:
        """触发回复成功 +1"""
        with _LOCK:
            self._get_day()["trigger_count"] += 1
            self._save()

    def inc_tool_call(self, tool_name: str) -> None:
        """工具执行 +1"""
        if not tool_name:
            return
        with _LOCK:
            self._get_day()["tools"][tool_name] += 1
            self._save()

    def record_model_usage(self, model_name: str, usage: Optional[Dict[str, Any]]) -> None:
        """记录一次模型请求的 token 消耗。

        兼容多家 provider 的 usage 字段：
        - OpenAI: prompt_tokens / completion_tokens / total_tokens,
          prompt_tokens_details.cached_tokens
        - Anthropic: cache_read_input_tokens / cache_creation_input_tokens
        - DeepSeek: prompt_cache_hit_tokens / prompt_cache_miss_tokens
        """
        if not model_name or not usage:
            return
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))

        cached = 0
        # OpenAI 风格
        ptd = usage.get("prompt_tokens_details")
        if isinstance(ptd, dict):
            cached += int(ptd.get("cached_tokens") or 0)
        # Anthropic 风格（部分兼容层映射到 prompt_tokens_details，这里兜底）
        cached += int(usage.get("cache_read_input_tokens") or 0)
        # DeepSeek 风格
        cached += int(usage.get("prompt_cache_hit_tokens") or 0)

        with _LOCK:
            entry = self._get_day()["models"][model_name]
            entry["prompt_tokens"] += prompt_tokens
            entry["completion_tokens"] += completion_tokens
            entry["total_tokens"] += total_tokens
            entry["cached_tokens"] += cached
            entry["requests"] += 1
            self._save()

    # ---- 查询接口 ----
    def render_today(self) -> str:
        """渲染当日统计文本"""
        with _LOCK:
            day = self._get_day()
            trigger = day["trigger_count"]
            models = {k: dict(v) for k, v in day["models"].items()}
            tools = dict(day["tools"])

        total_prompt = sum(m["prompt_tokens"] for m in models.values())
        total_cached = sum(m["cached_tokens"] for m in models.values())
        hit_rate = (total_cached / total_prompt * 100) if total_prompt > 0 else 0.0
        total_tokens = sum(m["total_tokens"] for m in models.values())

        lines = [f"=== 插件统计（{_today_str()}） ===", ""]
        lines.append(f"触发回复次数: {trigger}")
        lines.append("")

        lines.append(f"模型 Token 消耗 (合计 {total_tokens:,}):")
        if models:
            # 按总 token 降序
            for name, m in sorted(models.items(), key=lambda x: x[1]["total_tokens"], reverse=True):
                m_cached = m["cached_tokens"]
                m_prompt = m["prompt_tokens"]
                m_hit = (m_cached / m_prompt * 100) if m_prompt > 0 else 0.0
                lines.append(
                    f"  {name}: {m['total_tokens']:,} tokens "
                    f"(prompt {m_prompt:,} / completion {m['completion_tokens']:,} / "
                    f"缓存命中 {m_cached:,} = {m_hit:.1f}%) × {m['requests']}次"
                )
        else:
            lines.append("  (暂无数据)")
        lines.append(f"  — 整体缓存命中率: {total_cached:,}/{total_prompt:,} = {hit_rate:.1f}%")
        lines.append("")

        lines.append("工具调用次数:")
        if tools:
            for name, cnt in sorted(tools.items(), key=lambda x: x[1], reverse=True):
                lines.append(f"  {name}: {cnt}")
        else:
            lines.append("  (暂无数据)")

        return "\n".join(lines)

    def reset_today(self) -> str:
        """清空当日数据"""
        with _LOCK:
            self._days[_today_str()] = _empty_day()
            self._save()
        return f"已清空 {_today_str()} 的统计数据"


stats: StatsManager = StatsManager()
