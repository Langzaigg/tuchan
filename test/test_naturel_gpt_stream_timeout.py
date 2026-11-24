import asyncio
import importlib
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


def load_openai_func():
    """加载真实 openai_func，避免启动 NoneBot 与工具注册表。"""
    name = "_naturel_stream_fixture"
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt")]
    llm_tools = types.ModuleType(f"{name}.llm_tools")
    llm_tools.execute_tool = Mock()
    llm_tools.get_tool_schemas = Mock(return_value=[])
    stubs = {
        "logger": {"logger": Mock()},
        "llm_tools": llm_tools,
    }
    modules = {name: package, f"{name}.llm_tools": llm_tools}
    for part, fields in stubs.items():
        key = f"{name}.{part}"
        if key not in modules:
            modules[key] = types.ModuleType(key)
            vars(modules[key]).update(fields)
    patcher = patch.dict(sys.modules, modules)
    patcher.start()  # 常驻：函数体内有延迟 import
    return importlib.import_module(f"{name}.openai_func")


of = load_openai_func()


def make_generator(timeout: float = 180.0):
    tg = of.TextGenerator.__new__(of.TextGenerator)
    tg._completion_kwargs = lambda *a, **k: {"timeout": timeout, "model": "test"}
    return tg


def chunk(*, reasoning="", content="", tool_calls=None):
    delta = {}
    if reasoning:
        delta["reasoning_content"] = reasoning
    if content:
        delta["content"] = content
    if tool_calls:
        delta["tool_calls"] = tool_calls
    return {"choices": [{"delta": delta, "index": 0}]}


def scripted_stream(schedule):
    """schedule: [(delay_before_yield, chunk), ...]，播完即 StopAsyncIteration。"""
    async def gen():
        for delay, item in schedule:
            await asyncio.sleep(delay)
            yield item
    return gen


class StreamTimeoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_token_timeout(self):
        """首 token 30s（测试时缩到 0.2s）无任何 chunk → FirstTokenTimeoutError。"""
        tg = make_generator()
        tg._stream_iter_openai = lambda kwargs: scripted_stream([(1.0, chunk(content="晚"))])()
        with patch.object(of, "FIRST_TOKEN_TIMEOUT_SECONDS", 0.2):
            t0 = time.monotonic()
            with self.assertRaises(of.FirstTokenTimeoutError):
                await tg._stream_once([], "chat", None, None, None)
        self.assertLess(time.monotonic() - t0, 0.8)

    async def test_think_phase_timeout(self):
        """只有 reasoning 没有正文，累计超过 profile timeout → ThinkPhaseTimeoutError。"""
        tg = make_generator(timeout=0.4)
        tg._stream_iter_openai = lambda kwargs: scripted_stream([
            (0.0, chunk(reasoning="想")),
            (0.6, chunk(reasoning="还在想")),
            (0.6, chunk(content="晚")),
        ])()
        with self.assertRaises(of.ThinkPhaseTimeoutError):
            await tg._stream_once([], "chat", None, None, None)

    async def test_content_start_stops_the_clock(self):
        """正文开始输出后停表：后续流再慢也不计超时，正常完成。"""
        tg = make_generator(timeout=0.5)
        tg._stream_iter_openai = lambda kwargs: scripted_stream([
            (0.0, chunk(reasoning="想")),
            (0.3, chunk(content="第一句")),   # 0.3s 正文开始，停表
            (0.8, chunk(content="，第二句")),  # 已超 0.5s 保底，但不应超时
        ])()
        text, tool_calls, reasoning = await tg._stream_once([], "chat", None, None, None)
        self.assertEqual(text, "第一句，第二句")
        self.assertEqual(reasoning, "想")

    async def test_tool_calls_count_as_output(self):
        """tool_calls 增量同样视为输出开始（纯工具轮无正文也不超时）。"""
        tg = make_generator(timeout=0.5)
        tc = [{"index": 0, "id": "call_1", "type": "function",
               "function": {"name": "f", "arguments": "{}"}}]
        tg._stream_iter_openai = lambda kwargs: scripted_stream([
            (0.0, chunk(reasoning="想")),
            (0.3, chunk(tool_calls=tc)),
            (0.8, chunk(tool_calls=tc)),
        ])()
        text, tool_calls, reasoning = await tg._stream_once([], "chat", None, None, None)
        self.assertEqual(text, "")
        self.assertEqual(len(tool_calls), 1)

    async def test_normal_fast_stream(self):
        """正常快速流式：不触发任何超时，内容完整。"""
        tg = make_generator()
        tg._stream_iter_openai = lambda kwargs: scripted_stream([
            (0.0, chunk(content="你")),
            (0.05, chunk(content="好")),
        ])()
        text, _, _ = await tg._stream_once([], "chat", None, None, None)
        self.assertEqual(text, "你好")

    async def test_first_token_retry_budget_override(self):
        """重试时经 request_state 放宽首 token 预算：默认预算会超时，放宽后成功。"""
        tg = make_generator()
        tg._stream_iter_openai = lambda kwargs: scripted_stream([
            (0.4, chunk(content="来了")),
        ])()
        with patch.object(of, "FIRST_TOKEN_TIMEOUT_SECONDS", 0.2):
            # 默认 0.2s 预算 → 超时
            with self.assertRaises(of.FirstTokenTimeoutError):
                await tg._stream_once([], "chat", None, None, None, request_state={})
            # 重试放宽到 0.6s → 成功
            state = {"_first_token_timeout": 0.6}
            text, _, _ = await tg._stream_once([], "chat", None, None, None, request_state=state)
            self.assertEqual(text, "来了")


if __name__ == "__main__":
    unittest.main()
