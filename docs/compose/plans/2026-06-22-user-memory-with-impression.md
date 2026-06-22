# User Memory Co-location with Impression Implementation Plan

> [!NOTE]
> This document may not reflect the current implementation.
> See the final report for up-to-date state:
> [Final Report](../reports/user-memory-with-impression.md)

> **For agentic workers:** REQUIRED SUB-SKILL: Use compose:subagent (recommended) or compose:execute to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Co-locate user memories with user impressions in the same per-turn system message, improving prompt cache stability by keeping user-specific data bound to the triggering user's round.

**Architecture:** Currently, user memories are injected globally in System 3 (alongside group memories and date), while user impressions are injected per-turn as system messages before the triggering user's message. This change moves user memories into the impression system message, so each user's impression+memory block is stable and bound to their first trigger round. This maximizes cache hits because the system prefix (System 1-4) becomes more stable when user-specific data is removed from System 3.

**Tech Stack:** Python, NoneBot2, OpenAI-compatible API

---

## File Structure

- `ATRI/plugins/nonebot_plugin_naturel_gpt/chat_history.py` - Modify `update_chat_history_row` to include user memory in impression system
- `ATRI/plugins/nonebot_plugin_naturel_gpt/chat_prompt.py` - Modify `_message_text_for_prompt` to format impression+memory; modify `get_chat_prompt_template` to remove user memory from System 3

---

### Task 1: Modify `update_chat_history_row` to Include User Memory

**Covers:** Core change - bind user memory to impression system message

**Files:**
- Modify: `ATRI/plugins/nonebot_plugin_naturel_gpt/chat_history.py:107-130`

- [ ] **Step 1: Read the current implementation**

Read `chat_history.py` lines 107-130 to understand the current impression injection logic.

- [ ] **Step 2: Modify impression injection to include user memory**

In `update_chat_history_row`, when creating the impression system message, fetch the user's memory and append it to the impression text.

```python
# Current code (lines 117-129):
if not _has_imp:
    _imp_data = preset.chat_impressions.get(_uid)
    if _imp_data and _imp_data.chat_impression.strip():
        preset.prompt_messages.append(ChatMessageData(
            role="system",
            user_id=_uid,
            sender="",
            text=_imp_data.chat_impression.strip(),
            context_only=False,
            timestamp=time.time(),
            is_impression=True,
            impression_user_id=_uid,
        ))

# New code:
if not _has_imp:
    _imp_data = preset.chat_impressions.get(_uid)
    _imp_text = (_imp_data.chat_impression.strip() if _imp_data else "")
    # Fetch user memory and append to impression text
    _user_mem = self._get_user_memory(_uid)
    _user_mem_filtered = {k: v for k, v in _user_mem.items() if v}
    _user_memory_text = ""
    if _user_mem_filtered and config.MEMORY_ACTIVE:
        _mem_lines = []
        for _idx, (_k, _v) in enumerate(_user_mem_filtered.items(), 1):
            _mem_lines.append(f"{_idx}. {_k}: {_v}")
        _user_memory_text = "\n[你的记忆]\n" + "\n".join(_mem_lines)
    # Only inject if there's content (impression or memory)
    if _imp_text or _user_memory_text:
        _combined_text = _imp_text + _user_memory_text
        preset.prompt_messages.append(ChatMessageData(
            role="system",
            user_id=_uid,
            sender="",
            text=_combined_text.strip(),
            context_only=False,
            timestamp=time.time(),
            is_impression=True,
            impression_user_id=_uid,
        ))
```

- [ ] **Step 3: Verify the change compiles**

Run: `python -m py_compile ATRI/plugins/nonebot_plugin_naturel_gpt/chat_history.py`
Expected: No output (success)

---

### Task 2: Modify `_message_text_for_prompt` to Format Impression+Memory

**Covers:** Ensure the combined impression+memory is properly formatted in prompt

**Files:**
- Modify: `ATRI/plugins/nonebot_plugin_naturel_gpt/chat_prompt.py:216-221`

- [ ] **Step 1: Read the current implementation**

Read `chat_prompt.py` lines 216-221 to understand the current impression formatting.

- [ ] **Step 2: Update impression formatting to handle combined text**

The current code formats impression as `[用户印象: NickName]\n正文`. Since we're now including memory in the text, we need to ensure the format is clean.

```python
# Current code (lines 217-221):
if item.is_impression:
    imp_data = self.chat_preset.chat_impressions.get(item.impression_user_id)
    nickname = (imp_data.nickname or "").strip() if imp_data else ""
    label = f"[用户印象: {nickname}]" if nickname else "[用户印象]"
    return f"{label}\n{(item.text or '').strip()}"

# New code (no change needed - the text already contains memory)
# The format will naturally be:
# [用户印象: NickName]
# 印象正文
# [你的记忆]
# 1. key1: value1
# 2. key2: value2
```

Actually, no change is needed here. The `_message_text_for_prompt` already returns the full text, which now includes the memory section. The format will be:

```
[用户印象: NickName]
印象正文
[你的记忆]
1. key1: value1
2. key2: value2
```

- [ ] **Step 3: Verify the change compiles**

Run: `python -m py_compile ATRI/plugins/nonebot_plugin_naturel_gpt/chat_prompt.py`
Expected: No output (success)

---

### Task 3: Remove User Memory from System 3

**Covers:** Remove duplicate user memory injection from System 3

**Files:**
- Modify: `ATRI/plugins/nonebot_plugin_naturel_gpt/chat_prompt.py:52-81`

- [ ] **Step 1: Read the current implementation**

Read `chat_prompt.py` lines 52-81 to understand how user memory is currently added to System 3.

- [ ] **Step 2: Remove user memory from System 3 construction**

Remove the user memory retrieval and formatting from `get_chat_prompt_template`, keeping only group memory.

```python
# Current code (lines 52-69):
# 记忆模块 - 用户个人记忆
user_memory_text = ''
user_memory = ''
user_mem = self._get_user_memory(userid)
user_mem_filtered = {k: v for k, v in user_mem.items() if v}
if user_mem_filtered:
    idx = 0
    for k, v in user_mem_filtered.items():
        idx += 1
        user_memory_text += f"{idx}. {k}: {v}\n"

if config.MEMORY_ACTIVE:
    if group_memory_text:
        group_memory = f"[群记忆]\n{group_memory_text}\n"
    if user_memory_text:
        user_memory = f"[你的记忆]\n{user_memory_text}\n"

memory = group_memory + user_memory

# New code:
# 记忆模块 - 用户个人记忆（已移至 impression system 中，与用户印象绑定）
# 仅保留群记忆在 System 3
if config.MEMORY_ACTIVE:
    if group_memory_text:
        group_memory = f"[群记忆]\n{group_memory_text}\n"

memory = group_memory
```

- [ ] **Step 3: Update memory reminder to exclude user memory**

The user memory reminder should now be handled in the impression system, not System 3.

```python
# Current code (lines 71-81):
# 记忆接近上限时的整理提醒
memory_reminder = ''
if config.MEMORY_ACTIVE:
    max_len_mem = config.MEMORY_MAX_LENGTH
    threshold = max_len_mem * 4 // 5
    group_count = len(chat_memory_filtered)
    if group_count >= threshold:
        memory_reminder += f"\n[记忆提醒] 群记忆已达 {group_count}/{max_len_mem}，建议调用记忆整理工具精简。\n"
    user_count = len(user_mem_filtered)
    if user_count >= threshold:
        memory_reminder += f"\n[记忆提醒] 用户记忆已达 {user_count}/{max_len_mem}，建议调用记忆整理工具精简。\n"

# New code:
# 记忆接近上限时的整理提醒（仅群记忆，用户记忆提醒已移至 impression system）
memory_reminder = ''
if config.MEMORY_ACTIVE:
    max_len_mem = config.MEMORY_MAX_LENGTH
    threshold = max_len_mem * 4 // 5
    group_count = len(chat_memory_filtered)
    if group_count >= threshold:
        memory_reminder += f"\n[记忆提醒] 群记忆已达 {group_count}/{max_len_mem}，建议调用记忆整理工具精简。\n"
```

- [ ] **Step 4: Add memory reminder to impression system**

In `update_chat_history_row`, when building the combined text, add memory reminder if user memory is near limit.

```python
# In chat_history.py, after building _user_memory_text:
_user_mem_count = len(_user_mem_filtered)
_max_len_mem = config.MEMORY_MAX_LENGTH
_threshold = _max_len_mem * 4 // 5
_memory_reminder = ""
if _user_mem_count >= _threshold:
    _memory_reminder = f"\n[记忆提醒] 用户记忆已达 {_user_mem_count}/{_max_len_mem}，建议调用记忆整理工具精简。"

# Then include _memory_reminder in _combined_text
_combined_text = _imp_text + _user_memory_text + _memory_reminder
```

- [ ] **Step 5: Verify the change compiles**

Run: `python -m py_compile ATRI/plugins/nonebot_plugin_naturel_gpt/chat_prompt.py`
Run: `python -m py_compile ATRI/plugins/nonebot_plugin_naturel_gpt/chat_history.py`
Expected: No output (success)

---

### Task 4: Handle Edge Cases

**Covers:** Ensure robustness for edge cases

**Files:**
- Modify: `ATRI/plugins/onlybot_plugin_naturel_gpt/chat_history.py`

- [ ] **Step 1: Handle user with memory but no impression**

The current logic already handles this - if `_imp_text` is empty but `_user_memory_text` is not, we still inject the system message with just the memory.

- [ ] **Step 2: Handle user with neither memory nor impression**

The current logic already handles this - if both are empty, we don't inject the system message.

- [ ] **Step 3: Verify the change compiles**

Run: `python -m py_compile ATRI/plugins/nonebot_plugin_naturel_gpt/chat_history.py`
Expected: No output (success)

---

### Task 5: Update Documentation

**Covers:** Update AGENTS.md to reflect the new architecture

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 1: Update the "个人印象注入" section**

Update the description to mention that user memories are now co-located with impressions.

- [ ] **Step 2: Update the "记忆管理" section**

Update to mention that user memories are now injected with impressions, not in System 3.

---

## Verification Checklist

- [ ] User memories appear in the impression system message, not System 3
- [ ] Group memories remain in System 3
- [ ] Memory reminders for user memory appear in impression system
- [ ] Memory reminders for group memory remain in System 3
- [ ] Users with no impression but with memory still get their memory injected
- [ ] Users with neither impression nor memory don't get an extra system message
- [ ] Prompt cache stability is improved (user-specific data removed from system prefix)
