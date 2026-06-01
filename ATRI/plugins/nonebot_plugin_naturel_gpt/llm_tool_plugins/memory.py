from typing import Any, Dict, List, Tuple

schema = {
    "type": "function",
    "function": {
        "name": "remember",
        "description": (
            "记忆工具，用于主动保存、删除和批量整理对话中的重要信息。"
            "你应该积极使用此工具，不要等到用户明确要求才记录。"
            "当对话中出现以下情况时应立即调用：用户提到自己的名字、称呼、偏好、习惯、生日等个人信息；"
            "群内讨论的规则、约定、重要决定、共同话题；任何你觉得以后会用到的关键信息。宁可多记不可遗漏。"
            "scope 选择：个人信息用 user，群信息用 group。"
            "consolidate 操作用于批量增删记忆：当需要同时修改多条记忆、合并去重或精简时使用，不受条数上限限制。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["save", "delete", "consolidate"],
                    "description": "操作类型：save=保存单条记忆，delete=删除指定记忆，consolidate=批量整理记忆（增/删/改）"
                },
                "scope": {
                    "type": "string",
                    "enum": ["group", "user"],
                    "description": "记忆范围：group=群记忆（所有人共享），user=用户记忆（仅对该用户有效）"
                },
                "key": {
                    "type": "string",
                    "description": "（save/delete）记忆的名称，如'用户名'、'喜欢的颜色'、'群规'"
                },
                "value": {
                    "type": "string",
                    "description": "（save）记忆的内容"
                },
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": ["save", "delete"],
                                "description": "操作：save=保存或更新该记忆，delete=删除该记忆"
                            },
                            "key": {
                                "type": "string",
                                "description": "记忆名称"
                            },
                            "value": {
                                "type": "string",
                                "description": "（op=save 时必需）记忆内容"
                            }
                        },
                        "required": ["op", "key"]
                    },
                    "description": "（consolidate）批量操作列表，按顺序执行。可同时进行新增、修改和删除多条记忆。"
                }
            },
            "required": ["action", "scope"],
        },
    },
}


def _get_memories(chat, preset, scope: str, trigger_userid: str = None) -> Dict[str, str]:
    if scope == "user":
        if not trigger_userid:
            return {}
        if chat.chat_data.global_memory_enabled:
            from ..persistent_data_manager import PersistentDataManager
            return PersistentDataManager.instance.get_global_user_memories(trigger_userid)
        if trigger_userid not in preset.user_memories:
            preset.user_memories[trigger_userid] = {}
        return preset.user_memories[trigger_userid]
    if chat.chat_data.global_memory_enabled:
        return chat.chat_data.global_chat_memory
    return preset.chat_memory


def _set_memories(chat, preset, scope: str, memories: Dict[str, str], trigger_userid: str = None) -> None:
    if scope == "user":
        if not trigger_userid:
            return
        if chat.chat_data.global_memory_enabled:
            from ..persistent_data_manager import PersistentDataManager
            PersistentDataManager.instance.set_global_user_memories(trigger_userid, memories)
        else:
            preset.user_memories[trigger_userid] = memories
    else:
        if chat.chat_data.global_memory_enabled:
            chat.chat_data.global_chat_memory.clear()
            chat.chat_data.global_chat_memory.update(memories)
        else:
            preset.chat_memory.clear()
            preset.chat_memory.update(memories)


async def run(args: Dict[str, Any], config) -> Tuple[str, List[Dict[str, Any]]]:
    action = str(args.get("action") or "save").strip()
    scope = str(args.get("scope") or "group").strip()
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "").strip()
    operations_raw = args.get("operations") or []

    from ..openai_func import TextGenerator
    from ..chat_manager import ChatManager

    tg = TextGenerator.instance
    chat_key = tg._current_chat_key
    trigger_userid = tg._current_trigger_userid

    if not chat_key:
        return "无法获取当前会话信息。", []

    chat = ChatManager.instance.get_or_create_chat(chat_key=chat_key)
    preset = chat.chat_preset
    max_len = config.MEMORY_MAX_LENGTH
    scope_label = "你的" if scope == "user" else ""

    # ── delete ──
    if action == "delete":
        if not key:
            return "请提供要删除的记忆名称。", []
        mem = _get_memories(chat, preset, scope, trigger_userid)
        if key in mem:
            del mem[key]
            return f"已删除{scope_label}记忆「{key}」。", []
        return f"{scope_label}记忆中没有「{key}」。", []

    # ── consolidate：批量增删改 ──
    if action == "consolidate":
        if not operations_raw:
            return "请提供 operations 批量操作列表。", []

        mem = _get_memories(chat, preset, scope, trigger_userid)
        add_count = 0
        del_count = 0
        skip_count = 0

        for item in operations_raw:
            op = str(item.get("op", "")).strip()
            op_key = str(item.get("key", "")).strip()
            op_value = str(item.get("value", "")).strip()

            if not op_key:
                skip_count += 1
                continue

            if op == "save":
                if not op_value:
                    skip_count += 1
                    continue
                mem[op_key] = op_value
                add_count += 1
            elif op == "delete":
                if op_key in mem:
                    del mem[op_key]
                    del_count += 1
                else:
                    skip_count += 1
            else:
                skip_count += 1

        result_parts = []
        if add_count:
            result_parts.append(f"更新{add_count}条")
        if del_count:
            result_parts.append(f"删除{del_count}条")
        if skip_count:
            result_parts.append(f"跳过{skip_count}条")
        result = f"已整理{scope_label}记忆：{'，'.join(result_parts)}，当前共 {len(mem)} 条。"
        return result, []

    # ── save：单条保存 ──
    mem = _get_memories(chat, preset, scope, trigger_userid)

    if not key:
        return "请提供记忆名称（key）。", []

    is_update = key in mem
    mem[key] = value

    return f"已{'更新' if is_update else '记住'}{scope_label}记忆：「{key}」=「{value}」", []


TOOLS = [("remember", schema, run)]
