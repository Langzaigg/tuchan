---
feature: user-memory-with-impression
status: delivered
specs: []
plans:
  - docs/compose/plans/2026-06-22-user-memory-with-impression.md
branch: main
commits: N/A
---

# User Memory Co-location with Impression — Final Report

## What Was Built

This feature co-locates user memories with user impressions in the same per-turn system message. Previously, user memories were injected globally in System 3 (alongside group memories and date), while user impressions were injected per-turn as system messages before the triggering user's message. This change moves user memories into the impression system message, so each user's impression+memory block is stable and bound to their first trigger round.

The primary benefit is improved prompt cache stability. By removing user-specific data from System 3, the system prefix (System 1-4) becomes more stable across different users and conversations, maximizing cache hits.

## Architecture

The implementation modifies two core files:

1. **`chat_history.py`** - `update_chat_history_row()` now fetches user memory when creating impression system messages and appends it to the impression text.
2. **`chat_prompt.py`** - `get_chat_prompt_template()` no longer injects user memory into System 3, keeping only group memory there.

### Design Decisions

**Co-location strategy**: User memories are appended to impression text with a `[你的记忆]` label, creating a single system message containing both impression and memory. This keeps user-specific data bound to the triggering user's round.

**Memory reminder placement**: User memory reminders (when memory approaches limit) are now included in the impression system message rather than System 3, ensuring the reminder is contextually relevant to the specific user.

**Edge case handling**: Users with memory but no impression still get their memory injected. Users with neither impression nor memory don't get an extra system message.

## Usage

No configuration changes required. The feature works automatically:

- When a user triggers a response, their impression system message now includes both their impression text and their memories
- System 3 now only contains group memories and the current date
- Memory reminders for user memory appear in the impression system message

## Verification

The implementation was verified by:
1. Compiling both modified files (`chat_history.py` and `chat_prompt.py`) without errors
2. Reviewing the code changes to ensure proper integration with existing impression injection logic

## Journey Log

- [lesson] Co-locating user-specific data with impressions improves cache stability by keeping the system prefix stable across different users

## Source Materials

| File | Role | Notes |
|------|------|-------|
| `docs/compose/plans/2026-06-22-user-memory-with-impression.md` | Implementation plan | Complete |
