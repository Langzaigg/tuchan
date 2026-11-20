import asyncio
import importlib
import sys
import types
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, Mock, patch

import yaml


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


def load_mixins():
    # 加载真实组装与摘要逻辑，避免启动 NoneBot、读写存档或访问模型。
    name = "_naturel_context_fixture"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt")]
    config = types.SimpleNamespace(
        CONTEXT_WINDOW_SIZE=10, CONTEXT_COMPRESS_THRESHOLD_RATIO=1.0,
        CONTEXT_TOKEN_BUDGET=32768, DEBUG_LEVEL=0,
        CONTEXT_SUMMARY=types.SimpleNamespace(
            enabled=True, target_chars=800,
            pressure=types.SimpleNamespace(max_rounds=None, round_headroom=2, token_budget_ratio=0.875),
            idle=types.SimpleNamespace(minutes=30, keep_rounds=None, min_saved_tokens=2048),
        ),
        MULTIMODAL_ENABLE=False, MEMORY_ACTIVE=False, get_profile=lambda _: {},
    )
    generator = types.SimpleNamespace(
        cal_token_count=lambda value: len(value) if isinstance(value, str) else sum(
            len(message["content"]) for message in value
        ),
        get_response=AsyncMock(),
    )
    stubs = {
        "config": {"config": config, "get_config": lambda: config},
        "logger": {"logger": Mock()},
        "openai_func": {
            "TextGenerator": types.SimpleNamespace(instance=generator),
            "is_model_request_error_text": lambda _: False, "TRIGGER_MARKER_PREFIX": "[当前触发]",
        },
        "persistent_data_manager": {
            "ChatMessageData": Message, "PresetData": object, "ImpressionData": object,
            "PersistentDataManager": types.SimpleNamespace(instance=Mock()),
        },
        "llm_tool_plugins": {"TOOL_REGISTRY": {}},
        "llm_tool_plugins.anima_characters": {"PERSONA_BLOCK_TITLE": "[人设资料]"},
        "image_cache": {},
    }
    modules = {name: package}
    for part, fields in stubs.items():
        key = f"{name}.{part}"
        modules[key] = types.ModuleType(key)
        vars(modules[key]).update(fields)
    with patch.dict(sys.modules, modules):
        modules = [importlib.import_module(f"{name}.{module}") for module in ("chat_history", "chat_prompt", "chat_summary")]
    return (*modules, config, generator)


history, prompt, summary, config, generator = load_mixins()


class Chat(history.ChatHistoryMixin, prompt.ChatPromptMixin, summary.ChatSummaryMixin):
    chat_key = "test"
    _preset_key = "default"
    last_msg_time = 1

    def __init__(self, rounds, size=20):
        self.chat_preset = types.SimpleNamespace(
            preset_key=self._preset_key, context_summary="", tool_call_summary="", chat_impressions={},
            prompt_messages=self.rounds(0, rounds, size),
        )
        self.chat_preset_dicts = {self._preset_key: self.chat_preset}
        self._compress_task = None
        self._idle_compress_task = None
        self._compress_preset = None
        self._pending_overflow_text = ""
        self._pending_overflow_user_ids = None
        self._pending_overflow_item_ids = set()
        self._compressing_overflow_item_ids = set()
        self._compress_failure_time = 0

    @staticmethod
    def rounds(start, stop, size):
        return [
            Message(role, f"{index}:" + "文" * size)
            for index in range(start, stop)
            for role in ("user", "assistant")
        ]

    def get_active_profile(self):
        return "test"

    def _snapshot_request_profile(self):
        return {"model": "test"}


class ContextSummaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        generator.get_response.reset_mock(side_effect=True, return_value=True)
        generator.get_response.return_value = ("新的摘要", True)
        logs = patch.object(summary, "_save_summary_log")
        logs.start()
        self.addCleanup(logs.stop)

    async def test_burst_keeps_prefix_past_original_buffer(self):
        chat = Chat(20)
        before = await chat._build_openai_history_messages()
        chat.chat_preset.prompt_messages.extend(chat.rounds(20, 25, 20))
        await chat._compress_prompt_messages_if_needed(chat.chat_preset)
        after = await chat._build_openai_history_messages()
        self.assertEqual(after[:len(before)], before)
        self.assertEqual(len(after), 50)
        generator.get_response.assert_not_called()

    async def test_quiet_timer_compacts_only_when_useful(self):
        short = Chat(20)
        long = Chat(8, 650)
        with patch.object(summary.asyncio, "sleep", new_callable=AsyncMock) as sleep:
            short.schedule_idle_context_summary()
            await short._idle_compress_task
            generator.get_response.assert_not_called()
            long.schedule_idle_context_summary()
            await long._idle_compress_task
        await long._compress_task
        self.assertEqual([wait.args for wait in sleep.await_args_list], [(1800,), (1800,)])
        self.assertEqual(long._count_rounds(long.chat_preset.prompt_messages), 5)
        self.assertEqual(long.chat_preset.context_summary, "新的摘要")

    async def test_pressure_schedules_before_hard_round_limit_or_token_limit(self):
        for chat, tokens in ((Chat(28), 0), (Chat(17, 20), 28672)):
            await chat._compress_prompt_messages_if_needed(chat.chat_preset, request_tokens=tokens)
            self.assertIsNotNone(chat._compress_task)
            await chat._compress_task
            self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 10)

    async def test_idle_summary_is_applied_even_if_longer_than_estimated(self):
        chat = Chat(6, 1450)
        result = "文" * 1500
        generator.get_response.return_value = (result, True)
        await chat._compress_prompt_messages_if_needed(chat.chat_preset, idle=True)
        await chat._compress_task
        generator.get_response.assert_awaited_once()
        self.assertEqual(chat.chat_preset.context_summary, result)
        self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 5)

    async def test_idle_summary_requires_at_least_2048_net_saved_tokens(self):
        for net_tokens in (2047, 2048):
            with self.subTest(net_tokens=net_tokens):
                chat = Chat(6)
                user, assistant = chat.chat_preset.prompt_messages[:2]
                user.content_is_labeled = True
                user.text = "文" * (net_tokens + config.CONTEXT_SUMMARY.target_chars - 1)
                assistant.text = "文"
                await chat._compress_prompt_messages_if_needed(chat.chat_preset, idle=True)
                if net_tokens == 2047:
                    self.assertIsNone(chat._compress_task)
                    generator.get_response.assert_not_awaited()
                else:
                    self.assertIsNotNone(chat._compress_task)
                    await chat._compress_task
                    generator.get_response.assert_awaited_once()

    async def test_disabled_summary_uses_original_window_and_does_not_schedule(self):
        chat = Chat(21)
        with patch.object(config.CONTEXT_SUMMARY, "enabled", False):
            self.assertEqual(chat._history_buffer_round_limit(), 20)
            chat.schedule_idle_context_summary()
            await chat._compress_prompt_messages_if_needed(chat.chat_preset)
        self.assertIsNone(chat._idle_compress_task)
        self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 10)
        generator.get_response.assert_not_called()

    async def test_configured_pressure_limits_override_legacy_derivation(self):
        settings = config.CONTEXT_SUMMARY.pressure
        with patch.multiple(settings, max_rounds=26, round_headroom=4, token_budget_ratio=0.5):
            chat = Chat(21)
            self.assertEqual(chat._history_buffer_round_limit(), 26)
            await chat._compress_prompt_messages_if_needed(chat.chat_preset, request_tokens=16383)
            self.assertIsNone(chat._compress_task)

            for chat, tokens in ((Chat(22), 0), (Chat(21), 16384)):
                await chat._compress_prompt_messages_if_needed(chat.chat_preset, request_tokens=tokens)
                await chat._compress_task
                self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 10)

    async def test_configured_idle_delay_retention_and_savings(self):
        settings = config.CONTEXT_SUMMARY.idle
        with patch.multiple(settings, minutes=45, keep_rounds=4, min_saved_tokens=1200):
            with patch.object(summary.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                chat = Chat(5, 1100)
                chat.schedule_idle_context_summary()
                await chat._idle_compress_task
            await chat._compress_task
            sleep.assert_awaited_once_with(2700)
            self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 4)

    async def test_new_trigger_cancels_timer_and_prevents_inflight_idle_commit(self):
        chat = Chat(12, 650)
        chat.schedule_idle_context_summary()
        waiting = chat._idle_compress_task
        chat.cancel_idle_context_summary()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        generator.get_response.assert_not_called()

        resumed = asyncio.Event()

        async def finish_summary(*args, **kwargs):
            await resumed.wait()
            return "已经过时的摘要", True

        generator.get_response.side_effect = finish_summary
        await chat._compress_prompt_messages_if_needed(chat.chat_preset, idle=True)
        chat.last_msg_time = 2
        resumed.set()
        await chat._compress_task
        self.assertEqual(chat.chat_preset.context_summary, "")
        self.assertEqual(chat._count_rounds(chat.chat_preset.prompt_messages), 12)

    async def test_idle_cut_keeps_mailbox_group_together(self):
        chat = Chat(6, 3200)
        items = chat.chat_preset.prompt_messages
        # 最旧两条触发合并为同一条回复。只溢出一轮时应保留两条。
        del items[1]
        await chat._compress_prompt_messages_if_needed(chat.chat_preset, idle=True)
        self.assertIsNone(chat._compress_task)
        generator.get_response.assert_not_called()


class DriverConfig(dict):
    def __getattr__(self, name):
        return self[name]

    def __setattr__(self, name, value):
        self[name] = value


class ContextSummaryConfigMigrationTests(unittest.TestCase):
    def test_legacy_overlay_fields_survive_initial_save(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            base = root / "base.yml"
            overlay = root / "overlay.yml"
            base.write_text(yaml.safe_dump({
                "CONTEXT_WINDOW_SIZE": 10,
                "NG_DATA_PATH": str(root / "data"),
                "NG_LOG_PATH": str(root / "logs"),
            }), encoding="utf-8")
            overlay.write_text(yaml.safe_dump({
                "CONTEXT_SUMMARY_ENABLED": True,
                "CONTEXT_SUMMARY_TARGET_CHARS": 1200,
            }), encoding="utf-8")

            name = "_naturel_config_fixture"
            package = types.ModuleType(name)
            package.__path__ = [str(Path(__file__).resolve().parents[1] / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt")]
            driver = types.SimpleNamespace(config=DriverConfig(
                nickname={"兔酱"}, ng_config_path=str(overlay), ng_base_config_path=str(base),
                ng_endpoint_nicknames=["兔酱"], ng_persona_dir=str(root),
            ))
            atri = types.ModuleType("ATRI")
            atri.__path__ = []
            storage = types.ModuleType("ATRI.naturel_gpt_storage")
            storage.nickname_directory = lambda value: value
            stubs = {
                name: package,
                f"{name}.logger": types.SimpleNamespace(logger=Mock()),
                f"{name}.persona_loader": types.SimpleNamespace(load_personas_from_directory=lambda _: {}),
                "ATRI": atri,
                "ATRI.naturel_gpt_storage": storage,
            }
            with patch.dict(sys.modules, stubs), patch("nonebot.get_driver", return_value=driver):
                importlib.import_module(f"{name}.config")

            saved = yaml.safe_load(overlay.read_text(encoding="utf-8"))
            self.assertEqual(saved["CONTEXT_SUMMARY"]["enabled"], True)
            self.assertEqual(saved["CONTEXT_SUMMARY"]["target_chars"], 1200)
            self.assertNotIn("CONTEXT_SUMMARY_ENABLED", saved)
            self.assertNotIn("CONTEXT_SUMMARY_TARGET_CHARS", saved)


if __name__ == "__main__":
    unittest.main()
