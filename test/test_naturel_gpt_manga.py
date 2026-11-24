import asyncio
import importlib
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch


class MangaReplyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        name = "_naturel_manga_fixture"
        plugin_dir = Path(__file__).resolve().parents[1] / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt"
        self.config = types.SimpleNamespace(LLM_ENABLE_TOOLS=True, LLM_MAX_TOOL_ROUNDS=3)
        registry = {}
        fixtures = {
            "": {"__path__": [str(plugin_dir)]},
            ".llm_tool_plugins": {"__path__": [str(plugin_dir / "llm_tool_plugins")], "TOOL_REGISTRY": registry},
            ".llm_tool_plugins.anima_characters": {"get_characters": AsyncMock(return_value=[])},
            ".draw_db": {"save_prompt": Mock()},
            ".config": {"config": self.config},
            ".image_cache": {},
            ".logger": {"logger": Mock()},
        }
        modules = {}
        for suffix, fields in fixtures.items():
            module = types.ModuleType(name + suffix)
            vars(module).update(fields)
            modules[module.__name__] = module
        self.enterContext(patch.dict(sys.modules, modules))
        self.anima = importlib.import_module(f"{name}.llm_tool_plugins.anima_generate")
        self.moss = importlib.import_module(f"{name}.llm_tool_plugins.moss_generate")
        registry["generate_anima_image"] = (
            {"type": "function", "function": {"name": "generate_anima_image"}}, self.anima.run,
        )
        registry["generate_moss_image"] = (self.moss.schema, self.moss.run)
        self.tools = importlib.import_module(f"{name}.llm_tools")
        self.openai = importlib.import_module(f"{name}.openai_func")
        self.generator = self.openai.TextGenerator.instance
        self.draw_mode = self.enterContext(patch.object(self.anima, "get_chat_mode", return_value="on"))
        self.moss_enabled = self.enterContext(patch.object(self.moss, "get_moss_enabled", return_value=False))
        self.enterContext(patch.object(self.anima, "get_draw_model", return_value=self.anima.get_default_model()))
        self.enterContext(patch.object(self.anima, "get_manga_style", return_value="水彩"))
        self.manga_mode = self.enterContext(patch.object(self.anima, "get_manga_mode", return_value=True))
        self.enterContext(patch.object(self.anima, "logger"))
        self.enterContext(patch.object(self.anima, "_check_queue", AsyncMock(return_value=None)))
        self.enterContext(patch.object(self.anima, "_request", AsyncMock(return_value={
            "success": True, "images": [{"url": "https://example.test/anima.png"}],
        })))
        self.enterContext(patch.object(self.generator, "cal_token_count", return_value=20))

    async def test_streaming_reply_resumes_after_image_and_before_mailbox(self):
        await self.check_reply_order(stream=True)

    async def test_nonstreaming_reply_resumes_after_image_and_before_mailbox(self):
        await self.check_reply_order(stream=False)

    async def test_image_only_reply_completes_before_mailbox(self):
        await self.check_reply_order(stream=True, lead_in="", continuation="")

    def test_repeated_sentences_are_removed_from_the_continuation(self):
        self.assertEqual(
            self.openai.strip_repeated_manga_text("接住！下次换你发球。看好球。", ["接住", "看好球！"]),
            "下次换你发球。",
        )

    def test_moss_tool_has_a_manga_specific_trigger(self):
        self.moss_enabled.return_value = True
        manga_schema = self.tools.get_tool_schemas(self.config, "group_1")[-1]["function"]
        self.manga_mode.return_value = False
        regular_schema = self.tools.get_tool_schemas(self.config, "group_1")[-1]["function"]

        rule = self.anima.MANGA_MOSS_TRIGGER_RULE
        self.assertIn(rule, self.anima.get_draw_s1_rules(True))
        self.assertIn(rule, manga_schema["description"])
        self.assertNotIn("分格漫画、海报级排版或精确构图时也调用", manga_schema["description"])
        self.assertIn("分格漫画、海报级排版或精确构图时也调用", regular_schema["description"])
        self.assertEqual(manga_schema["parameters"], regular_schema["parameters"])

    async def test_manga_force_mode_leaves_tool_selection_to_manga_rules(self):
        self.draw_mode.return_value = "force"
        self.moss_enabled.return_value = True
        self.generator.init([""], {"enable_stream": False})
        self.generator._current_chat_key = "group_1"
        model = self.enterContext(patch.object(self.generator, "_complete_once", AsyncMock(
            return_value=("收到。", [], {}),
        )))

        await self.generator.stream_response(
            [{"role": "user", "content": "帮我画四格，每格两句对白"}], plugin_config=self.config,
        )

        messages = model.await_args.args[0]
        self.assertEqual(messages[-1]["content"], self.openai._FORCE_MANGA_DRAW_HINT_TEXT)

    async def check_reply_order(self, stream, lead_in="接住！", continuation="下次换你发球。"):
        self.generator.init([""], {"enable_stream": stream})
        events = []
        requests = []
        buffer = []
        sending_image = asyncio.Event()
        image_sent = asyncio.Event()

        async def send_image(**_):
            events.append("sending image")
            sending_image.set()
            await image_sent.wait()
            events.append("image sent")

        self.enterContext(patch.object(self.anima, "get_bot", return_value=types.SimpleNamespace(send_group_msg=send_image)))

        def model_reply(messages):
            requests.append([dict(message) for message in messages])
            turn = len(requests)
            if turn == 1:
                return lead_in, [{"id": "draw-1", "function": {
                    "name": "generate_anima_image", "arguments": '{"tags": "catching a ball"}',
                }}]
            if turn == 2:
                return continuation, []
            return "这就回你。", []

        async def stream_once(messages, _type, _tools, on_text, *_args, **_kwargs):
            text, calls = model_reply(messages)
            await on_text(text)
            return text, calls, ""

        async def complete_once(messages, *_args, **_kwargs):
            text, calls = model_reply(messages)
            return text, calls, {"role": "assistant", "content": text, "tool_calls": calls}

        self.enterContext(patch.object(self.generator, "_stream_once", stream_once))
        self.enterContext(patch.object(self.generator, "_complete_once", complete_once))

        async def collect(text):
            buffer.append(text)

        async def flush():
            if any(buffer):
                events.append("".join(buffer))
            buffer.clear()

        async def finish(*_):
            await flush()

        async def reply():
            self.generator._current_chat_key = "group_1"
            self.anima.register_send_context("group_1", "1", group_id="1")
            try:
                return await self.generator.stream_response(
                    [{"role": "user", "content": "接球"}], plugin_config=self.config,
                    on_text=collect, before_manga_draw=flush, on_reply_complete=finish,
                )
            finally:
                self.anima.unregister_send_context()

        running = asyncio.create_task(reply())
        before_image = [lead_in] if lead_in else []
        try:
            await asyncio.wait_for(sending_image.wait(), timeout=2)
            self.assertEqual(events, [*before_image, "sending image"])
            self.assertEqual(len(requests), 1)
            self.generator.push_loop_input("group_1", {"text": "甲：新消息", "sender": "甲", "userid": "2"})
        finally:
            image_sent.set()
            _, success, _, _ = await asyncio.wait_for(running, timeout=2)

        self.assertTrue(success)
        after_image = [continuation] if continuation else []
        self.assertEqual(events, [*before_image, "sending image", "image sent", *after_image, "这就回你。"])
        assistant, tool = requests[1][-2:]
        self.assertEqual(assistant["content"], lead_in)
        self.assertEqual(tool["tool_call_id"], "draw-1")
        self.assertTrue(tool["content"].startswith(self.anima.MANGA_IMAGE_SENT))
        self.assertEqual(requests[2][-1]["content"], "甲：新消息")

    async def test_manga_send_failure_does_not_claim_delivery_or_queue_a_late_image(self):
        bot = types.SimpleNamespace(send_group_msg=AsyncMock(side_effect=RuntimeError("send failed")))
        self.enterContext(patch.object(self.anima, "get_bot", return_value=bot))
        self.anima.register_send_context("group_1", "1", group_id="1")
        result, _ = await self.anima.run({"tags": "a wave"}, self.config)
        self.assertTrue(result.startswith(self.anima.MANGA_IMAGE_FAILED))
        self.assertEqual(self.anima.consume_pending_results(), [])

    async def test_regular_anima_still_returns_a_task_number_before_sending(self):
        self.manga_mode.return_value = False
        release = asyncio.Event()
        bot = types.SimpleNamespace(send_group_msg=AsyncMock())
        self.enterContext(patch.object(self.anima, "get_bot", return_value=bot))

        async def generate(*_args, **_kwargs):
            await release.wait()
            return {"success": True, "images": [{"url": "https://example.test/anima.png"}]}

        self.anima._request.side_effect = generate
        async def draw():
            self.anima.register_send_context("group_1", "1", group_id="1")
            try:
                return await self.anima.run({"tags": "a wave"}, self.config)
            finally:
                self.anima.unregister_send_context()

        try:
            result, _ = await asyncio.wait_for(draw(), timeout=2)
            self.assertIn("任务编号：", result)
            bot.send_group_msg.assert_not_awaited()
        finally:
            release.set()
            await asyncio.gather(*self.anima._bg_tasks)
        bot.send_group_msg.assert_awaited_once()

    async def test_mini_submits_a_drawing_when_forced_tool_choice_is_unsupported(self):
        self.generator.init([""], {"model": "mini"})
        model = self.enterContext(patch.object(self.generator, "_request_openai_compatible", AsyncMock(side_effect=[
            RuntimeError("HTTP 400: tool_choice unsupported in thinking mode"),
            {"choices": [{"message": {"content": "the bot reaching for an umbrella"}}]},
        ])))
        log = self.enterContext(patch.object(self.anima, "_save_manga_draw_log"))
        bot = types.SimpleNamespace(send_group_msg=AsyncMock())
        self.enterContext(patch.object(self.anima, "get_bot", return_value=bot))
        self.anima.register_send_context("group_1", "1", group_id="1")
        chat = types.SimpleNamespace(chat_preset=None, _get_chat_memory=lambda: {})
        await self.anima.manga_idle_draw("group_1", chat, self.config, None, pending_request="帮我画撑伞")
        native, text_fallback = (call.args[0] for call in model.await_args_list)
        self.assertEqual(native["tool_choice"]["function"]["name"], "generate_anima_image")
        self.assertEqual(text_fallback["tools"], [])
        self.assertTrue(any("帮我画撑伞" in item["content"] for item in text_fallback["messages"]))
        self.assertIn("水彩", text_fallback["messages"][-1]["content"])
        self.assertEqual(self.anima._request.await_args.kwargs["json"]["tags"], "the bot reaching for an umbrella")
        bot.send_group_msg.assert_awaited_once()
        self.assertTrue(log.call_args.args[1]["success"])
