import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "ATRI" / "plugins" / "nonebot_plugin_naturel_gpt" / "anthropic_func.py"
)
SPEC = importlib.util.spec_from_file_location("anthropic_func", MODULE_PATH)
anthropic = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(anthropic)


def message(role, content, **fields):
    return {"role": role, "content": content, **fields}


class AnthropicRequestTests(unittest.TestCase):
    def test_trigger_changes_only_the_uncached_tail(self):
        history = [
            message("system", "固定角色"),
            message("system", "额外规则"),
            message("system", "[压缩上下文摘要]\n较早对话"),
            message("system", "[当前状态]\n群记忆"),
            message("system", "[用户印象: 甲]\n历史印象"),
            message("user", "上一轮问题"),
            message("system", "[搜索工具摘要]\n上一轮结果"),
            message("assistant", "上一轮回复"),
        ]

        def request(sender):
            return anthropic.build_request({"messages": [
                *history,
                message("system", f"[当前触发] 回应{sender}"),
                message("user", f"{sender}：你好"),
            ]})[2]

        first, second = request("甲"), request("乙")
        self.assertEqual(first["system"], second["system"])
        self.assertEqual(len(first["system"]), 4)
        self.assertEqual(
            [index for index, block in enumerate(first["system"]) if "cache_control" in block],
            [1, 3],
        )
        self.assertEqual(first["messages"][:-1], second["messages"][:-1])
        previous_user, previous_assistant, trigger = second["messages"]
        self.assertEqual(
            [block["text"] for block in previous_user["content"]],
            ["[系统补充]\n[用户印象: 甲]\n历史印象", "上一轮问题",
             "[系统补充]\n[搜索工具摘要]\n上一轮结果"],
        )
        self.assertIn("cache_control", previous_assistant["content"][-1])
        self.assertIn("[当前触发] 回应乙", trigger["content"][0]["text"])
        self.assertNotIn("cache_control", trigger["content"][0])

    def test_first_turn_reminder_stays_out_of_system(self):
        system, conversation = anthropic.split_messages([
            message("system", "固定角色"),
            message("system", "[人设资料]\n测试人设"),
            message("system", "[当前触发] 回应甲"),
            message("user", "你好"),
        ])
        self.assertEqual(system, ["固定角色"])
        self.assertEqual(len(conversation), 1)
        self.assertEqual(len(conversation[0]["content"]), 3)

    def test_tool_results_precede_inline_notes_and_image(self):
        thinking = {"type": "thinking", "thinking": "trace", "signature": "sig"}
        tool_calls = [
            {"id": call_id, "function": {"name": "lookup", "arguments": "{}"}}
            for call_id in ("toolu_1", "toolu_2")
        ]
        messages = [
            message("user", "查一下"),
            message("assistant", "", tool_calls=tool_calls, _anthropic_thinking_blocks=[thinking]),
            message("system", "搜完后直接回复"),
            message("tool", "结果甲", tool_call_id="toolu_1"),
            message("system", "[新消息提醒] 有新消息"),
            message("tool", "结果乙", tool_call_id="toolu_2"),
            message("user", [
                {"type": "text", "text": "[图片1]"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,YQ=="}},
            ]),
        ]
        first = anthropic.build_request({"messages": messages})[2]["messages"]
        second = anthropic.build_request({"messages": messages})[2]["messages"]
        self.assertEqual(first, second)
        self.assertEqual(first[1]["content"][0], thinking)
        self.assertNotIn("cache_control", thinking)
        results = first[2]["content"]
        self.assertEqual(
            [block["type"] for block in results],
            ["tool_result", "tool_result", "text", "text", "text", "image"],
        )
        self.assertEqual([block["tool_use_id"] for block in results[:2]], ["toolu_1", "toolu_2"])
        self.assertEqual(results[-1]["source"]["data"], "YQ==")


if __name__ == "__main__":
    unittest.main()
