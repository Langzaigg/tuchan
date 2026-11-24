import importlib
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock, patch


@dataclass
class Message:
    role: str
    text: str
    context_only: bool = False
    is_impression: bool = False
    content_is_labeled: bool = False
    sender: str = ""
    timestamp: float = 0
    images: list = field(default_factory=list)
    tool_calls: list = field(default_factory=list)
    tool_call_summary: str = ""


def load_prompt_module():
    """加载真实 chat_prompt，避免启动 NoneBot、读写存档或访问模型。"""
    name = "_naturel_trigger_fixture"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt")]
    config = types.SimpleNamespace(
        CONTEXT_WINDOW_SIZE=50, CONTEXT_COMPRESS_THRESHOLD_RATIO=1.0,
        CONTEXT_TOKEN_BUDGET=32768, DEBUG_LEVEL=0,
        CONTEXT_SUMMARY=types.SimpleNamespace(
            enabled=False, target_chars=800,
            pressure=types.SimpleNamespace(max_rounds=None, round_headroom=2, token_budget_ratio=0.875),
            idle=types.SimpleNamespace(minutes=30, keep_rounds=None, min_saved_tokens=2048),
        ),
        MULTIMODAL_ENABLE=False, MEMORY_ACTIVE=False, MEMORY_MAX_LENGTH=50,
        LLM_ENABLE_TOOLS=False, ENABLE_MSG_TO_IMG=False, REPLY_MAX_SEGMENTS=3,
        get_profile=lambda _: {},
    )
    tg = types.SimpleNamespace(
        extra_prompt="",
        cal_token_count=lambda value: sum(len(str(m.get("content") or "")) for m in value),
    )
    anima_generate = types.ModuleType(f"{name}.llm_tool_plugins.anima_generate")
    anima_generate.get_manga_mode = lambda _: False
    anima_generate.get_chat_mode = lambda _: "off"
    anima_generate.get_draw_s1_rules = lambda _: ""
    plugins_pkg = types.ModuleType(f"{name}.llm_tool_plugins")
    plugins_pkg.TOOL_REGISTRY = {}
    plugins_pkg.anima_generate = anima_generate
    stubs = {
        "config": {"config": config, "get_config": lambda: config},
        "logger": {"logger": Mock()},
        "openai_func": {"TextGenerator": types.SimpleNamespace(instance=tg), "TRIGGER_MARKER_PREFIX": "[当前触发]"},
        "persistent_data_manager": {"ChatMessageData": Message, "PresetData": object},
        "llm_tool_plugins": plugins_pkg,
        "llm_tool_plugins.anima_generate": anima_generate,
        "llm_tool_plugins.anima_characters": {"PERSONA_BLOCK_TITLE": "[人设资料]"},
        "image_cache": {"collect_active_urls": lambda _: [], "purge_stale": lambda _: None},
    }
    modules = {name: package, f"{name}.llm_tool_plugins": plugins_pkg,
               f"{name}.llm_tool_plugins.anima_generate": anima_generate}
    for part, fields in stubs.items():
        key = f"{name}.{part}"
        if key not in modules:
            modules[key] = types.ModuleType(key)
            vars(modules[key]).update(fields)
    _patcher = patch.dict(sys.modules, modules)
    _patcher.start()  # 常驻：函数内部有延迟 import（anima_generate），卸载会导致用例期找不到模块
    return importlib.import_module(f"{name}.chat_prompt")


prompt = load_prompt_module()


class Chat(prompt.ChatPromptMixin):
    chat_key = "test"
    _preset_key = "default"

    def __init__(self, messages):
        self.chat_preset = types.SimpleNamespace(
            preset_key="default", context_summary="", bot_self_introl="你是测试角色。",
            prompt_messages=messages,
        )
        self.chat_preset_dicts = {"default": self.chat_preset}
        self._chat_data = types.SimpleNamespace(chat_memory={})
        self._visible_images = {}

    def get_active_profile(self):
        return "test"

    def get_unlock_content_limit(self):
        return False

    def _is_vision_profile_active(self):
        return False

    def _get_chat_memory(self):
        return self._chat_data.chat_memory

    @staticmethod
    def _is_supported_image_url(url):
        return False


def make_history():
    """两条邮箱 entry（何为 在前、misuzu 在后）均已在历史中。"""
    return [
        Message("user", "[19:27] 何为。: 我好看吗", sender="何为。"),
        Message("user", "[19:30] misuzu: 最早跟ai谈恋爱的是不是旺达", sender="misuzu"),
        Message("user", "[19:30] misuzu: 再不回答我就变成你的老婆", sender="misuzu"),
    ]


def find_marker(messages):
    for i, m in enumerate(messages):
        if m.get("role") == "system" and str(m.get("content") or "").startswith("[当前触发]"):
            return i, m["content"]
    return -1, ""


class TriggerMarkerTests(unittest.IsolatedAsyncioTestCase):
    def test_find_trigger_msg_idx_tail_ordinal(self):
        msgs = [
            {"role": "user", "content": "甲"},
            {"role": "assistant", "content": "好"},
            {"role": "user", "content": "[群聊上下文-非触发消息]\n路人: 闲聊"},
            {"role": "user", "content": "乙"},
            {"role": "user", "content": "丙"},
        ]
        cls = prompt.ChatPromptMixin
        self.assertEqual(cls._find_trigger_msg_idx(msgs), 4)          # 默认：最后一条 user
        self.assertEqual(cls._find_trigger_msg_idx(msgs, 1), 4)       # 倒数第 1
        self.assertEqual(cls._find_trigger_msg_idx(msgs, 2), 3)       # 倒数第 2
        self.assertEqual(cls._find_trigger_msg_idx(msgs, 3), 0)       # 倒数第 3（context_only 不计）
        self.assertEqual(cls._find_trigger_msg_idx(msgs, 9), -1)      # 越界

    async def test_replay_marks_recorded_entry_not_newest(self):
        """邮箱残留重放：触发标记必须落在 entry 自己的消息上，而不是历史最新的 user。"""
        history = make_history()
        entry = history[0]  # 何为「我好看吗」是最早的 entry，历史里还有更新的 misuzu 消息
        chat = Chat(history)
        messages = await chat.get_chat_prompt_template(userid="1", chat_type="group", include_images=False, trigger_msg=entry)
        idx, content = find_marker(messages)
        self.assertGreaterEqual(idx, 0, "触发标记未注入")
        self.assertIn("何为。", content)
        # 标记必须紧跟在 entry 消息之前
        self.assertEqual(messages[idx + 1]["role"], "user")
        self.assertIn("我好看吗", str(messages[idx + 1]["content"]))

    async def test_default_marks_newest_user(self):
        """正常路径：无 trigger_msg 时标记落在历史最后一条 user（行为不回归）。"""
        chat = Chat(make_history())
        messages = await chat.get_chat_prompt_template(userid="1", chat_type="group", include_images=False)
        idx, content = find_marker(messages)
        self.assertGreaterEqual(idx, 0)
        self.assertIn("misuzu", content)
        self.assertIn("再不回答我就变成你的老婆", str(messages[idx + 1]["content"]))

    async def test_stale_recorded_msg_falls_back_to_newest(self):
        """entry 在 reset/摘要后已不在历史：回退到最后一条 user，不丢标记。"""
        orphan = Message("user", "[19:00] 路人: 已被清掉的消息", sender="路人")
        chat = Chat(make_history())
        messages = await chat.get_chat_prompt_template(userid="1", chat_type="group", include_images=False, trigger_msg=orphan)
        idx, content = find_marker(messages)
        self.assertGreaterEqual(idx, 0)
        self.assertIn("misuzu", content)


if __name__ == "__main__":
    unittest.main()
