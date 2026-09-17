# naturel_gpt 修改计划：上下文聚焦 + 图片注意力（v2）

依据：2026-09-04 群 620260076 15:09–15:30 的实际对话、`data/naturel_gpt/logs/group_620260076.latest.json` 请求快照、`data/logs/info/20260904-10.log` 运行日志，以及对 `chat_prompt.py` / `matcher.py` / `openai_func.py` / `persistent_data_manager.py` / `chat_history.py` 的代码核对。

v2 相对 v1 的变化：图片方案从"触发句关键词门控 + 注入触发消息"整体换成"图片就地保留、渲染层全局编号、统一 1 小时过期（半小时点批量过期并重编号）"；邮箱多条插入合并为一条；验证部分精简。

**实施状态（2026-09-04）**：阶段 A、B、C、D 已全部落地（`persistent_data_manager.py`、`chat_history.py`、`chat_prompt.py`、`openai_func.py`、`matcher.py`、`image_cache.py`、`config.py`、`chat.py`、`anime_trace.py`、`vision.py`，配置与 AGENTS.md / README / changelog 已同步）。本地验证：A-1 计数不变量、图片策略（过期/编号/去重/容量回收）、触发定位、邮箱批次合并、缓冲 flush 时间衰减共 26 项断言通过。待办：线上观察一周（触发消息 image 部件数、`cached_tokens` 比例、回复段数）。

## 已确认的根因

| # | 根因 | 证据 |
|---|---|---|
| R1 | 邮箱批次的第二条回复在落库时被"user 开轮、首条 assistant 关轮、其后 assistant 丢弃"的不变量丢掉 | 15:11:26 / 15:11:35 两次「回复完成」，群历史只有第一条；15:30:02 / 15:30:14 同样。回雪莉的「这位是新面孔吧」只存在于乡窝宁（972370971）的印象历史里 |
| R2 | 图片门控关键词含「这/上/前/看/图」单字，命中后把窗口内所有 context_only 块的图追加到触发消息，无文本标记、无时效；工具侧 `_current_trigger_images` 只含触发消息自身图片 | 触发句「我这是表达对你的仰慕…」命中「这」→ 日志「tokens: 6477 + 3图」→ 回复点评 grok 截图/赛马娘/白脸笑；anime_trace 回「没有可用图片」「序号超出范围」 |
| R3 | `[群聊上下文-非触发消息]` 块在规则里没有定位；邮箱提醒写「请一并回应」；尾部没有"本轮回应谁"的指令；规则"最多3段"与 `REPLY_MAX_SEGMENTS=5` 不一致 | 15:29 回复主动接 5 分钟前 Le Triomphant「调时钟到6点」；17:52 主动提「花咲也发图了」；15:11 回复 5 段 |
| R4 | context_only 块按条数不按时间，一块可跨 20 分钟；缓冲区留图规则保留"触发者本人所有图" | 快照 L[19] 块 17 行含 3 张图，挂在 15:09 触发上 |

## 设计原则（v2）

1. **图片就地保留，作为稳定前缀。** 图片留在它被发出的那条消息里（触发 user、历史 user、context_only 块），每次请求原样重发，不再往触发消息里搬运。前缀不变即命中缓存；直传 URL 与 base64 都是逐字节相同的内容。
2. **渲染层全局编号，只在前缀已断的时刻重编。** 存储层保持现状（每条消息内部 `[图片1..k]` 本地编号）。渲染时把当前**可见**图片按上下文顺序从 1 连续编号，写进各消息文本的 `[图片N]`。新图只在尾部追加，已有编号不变；编号只在有图片离开上下文时才整体前移，而图片离开（过期、容量回收、窗口裁剪）本身就已经从最旧位置断了前缀，所以重编号不额外增加缓存失效。编号始终不超过可见图片数（上限 8 左右），不会越编越大。工具用同一编号取图。
3. **统一 1 小时过期，半小时点批量过期。** 所有位置的图片过期时间统一为 `MULTIMODAL_IMAGE_FRESH_MINUTES = 60`。过期判定用量化截止点 `cutoff = floor((now − 60min) / 30min) × 30min`，`timestamp < cutoff` 即过期。无状态、可重现；图片实际存活 60 到 90 分钟，在整点和半点一起退场并触发一次重编号，前缀每半小时至多断一次。
4. **容量滞后回收。** 可见图片总数超过 `MULTIMODAL_MAX_IMAGES`（默认 8）时，按最旧优先剥离到 `MULTIMODAL_MAX_IMAGES // 2`。剥离必然断前缀，滞后回收让它每 4 张新图最多发生一次，与现有"窗口 + 溢出比"压缩策略同频。
5. **不再对触发句做关键词门控。** 图片是否值得关注，交给"图片带发送者与时间就地出现 + 规则说明背景图不主动点评"来解决，而不是猜触发句有没有提到图。
6. **邮箱批次合并为一条。** 一个打断点上到达的多条新触发消息，合并为一条 user 消息插入循环，避免多条 user 连发让模型逐条作答、越答越散。

约束：守住现有缓存不变式（第 k 轮请求前缀是第 k-1 轮的严格前缀，尾部状态块除外）。下面每项标注对缓存的影响。

---

## 阶段 A：历史落库 bug（先行，独立小改）

### A-1 round 不变量改为计数

**改动点**：`persistent_data_manager.py:185-197`（`_load_from_dict`）、`persistent_data_manager.py:208-222`（`_serializable`）、`chat_history.py:321-372`（`_cleanup_orphan_history_messages`）。

**怎么改**：三处把 `round_open: bool` 换成 `open_users: int`。user 消息 `+1`；无 `tool_calls` 的最终 assistant：`open_users > 0` 时保留并 `-1`，否则丢弃；模型错误文本仍视作关轮（`-1` 后丢弃）。`active_tool_call_ids` 逻辑不动。

**原因**：邮箱路径下新触发消息到达时即落库（`matcher.py:1046-1053`），排在第一条回复之前，历史序列必然是 `user(A) user(B) assistant(答A) assistant(答B)`，布尔不变量把第四条当孤儿丢掉。计数版保留"assistant 必须有 user 承接"的原意。

**预期成效**：批次回复不再丢失。模型下一轮看到的是"B 问了、兔酱答了 B"，而不是"B 问了、兔酱用答 A 的话应付了 B"。这是串话题在后续轮次持续放大的直接推手。

**缓存影响**：无。

### A-2 批次回复的用户维度归属

**改动点**：`matcher.py:1383-1444`（`_on_reply_complete`）。

**怎么改**：`reply_entries` 非空时，对批次中每个 entry 的 `userid` / `sender` 各调用一次 `update_chat_history_row_for_user`（同一回复文本）；`reply_entries is None` 时维持原 `trigger_userid` / `sender_name`。

**原因**：闭包里的 `trigger_userid` 是原始触发者，回雪莉的话被记进乡窝宁的印象历史，印象摘要会把别人的对话学到错误的人身上。

**预期成效**：per-user 印象与用户记忆归属正确。

### A-3 注释纠正

`matcher.py:1178` 注释与实现相反。随阶段 B 删除注入逻辑时一并删掉。

---

## 阶段 B：图片就地保留 + 渲染层编号 + 统一过期

### B-1 渲染层全局编号

**改动点**：`chat_prompt.py:269-364`（`_apply_image_gating` 重写为 `_apply_image_policy`，与 B-3 同一函数）；`chat_prompt.py:226-241`（`_message_content_for_prompt`）；视觉 profile 路径的 `_vision_context_images` 重编号逻辑（`chat_prompt.py:596-613` 分支）。存储层、`matcher.py` 入口解析、缓冲区合成文本、`_build_loop_user_message`、`_next_image_index` 均不改。

**怎么改**：
1. 存储层保持每条消息内部 `[图片1..k]` 本地编号（现状）。
2. `_apply_image_policy` 先按 B-3 确定哪些图片本轮可见，再按上下文顺序（消息顺序 × 消息内顺序）从 1 连续分配显示编号；渲染每条含图消息时把本地 `[图片k]` 改写为显示编号，image 部件顺序与之一致。这正是现有视觉 profile 分支的做法，推广为所有 profile 的唯一路径，删掉分支。
3. 不可见（过期或被回收）的图片，文本改写为 `[图片已过期]`，不占编号。
4. 邮箱插入的 entry 图片继续用 `_next_image_index`（扫描 messages 中最大编号 +1）续编，与显示编号天然衔接。

**原因**：编号是给模型和工具在"这一次请求"里对齐用的，不需要终生唯一。可见集合只在尾部增长时编号不变；集合从头部缩小时前缀已断，顺势重编不增加代价，还能把编号压回 1 起。

**预期成效**：模型看到的编号始终是 1 到可见图片数之间的小数字；模型说「图 3」工具就取当前的图 3；两次过期事件之间编号完全稳定。

**缓存影响**：无额外影响。重编号只发生在过期 / 回收 / 裁剪已经断前缀的那一次渲染里。

**已知代价**：过期事件之后，历史 assistant 文本里提到的旧编号（如「图 3 是醒目飞鹰」）指向的可能已是另一张图。事件每半小时至多一次，且规则 6 已要求不主动翻旧图，接受这个代价。

### B-2 context_only 块就地携带图片

**改动点**：`chat_history.py:74-102`（context_only 行写入）、`chat_prompt.py:557-560`（context_only 渲染为 system）、`chat_prompt.py:448-455`（`_is_context_only_system_message`）、`chat_prompt.py:632-651`（`_trim_messages_to_request_budget` 中"最后一条 user"的判定需确认是按 item 标志而非 dict role）；`matcher.py:117-162`（`_flush_recent_context_buffer`）。

**怎么改**：
1. `_flush_recent_context_buffer` 不再按发送者丢图：所有图片都保留，块内按出现顺序本地编号 `[图片1..k]`（现有重编逻辑保留，只是不再区分发送者）；返回 `images` 与平行的 `image_meta: List[{sender, timestamp}]`，写入 `ChatMessageData` 新可选字段 `image_meta`（context_only 不持久化，无兼容负担）。
2. context_only 块渲染角色从 `system` 改为 `user`（OpenAI 兼容接口的 system 不接受 image 部件），文本仍以 `[群聊上下文-非触发消息]` 开头，内容为 `[HH:MM] sender: text` 行；含图时 content 为 multipart，图片部件按行内编号顺序排列，编号由 B-1 在渲染时改写为显示编号。
3. 与"触发消息 = 最后一条非 context_only user"相关的判定统一改为按 `ChatMessageData.context_only` 标志（`chat_prompt.py:279-284`、`618-621` 已是；`636-641` 需核对）。`_is_context_only_system_message` 改名为 `_is_context_only_message`，按文本头识别。
4. 删除 `_apply_image_gating` 中的关键词检测与"注入触发消息"两段（`chat_prompt.py:277`、`296-342`），只保留"为含图消息就地注入 image 部件"的逻辑，并扩展到 context_only 项。

**原因**：图片留在它出现的位置，模型看到的是「乡窝宁 14:48 发了 [图片3]」而不是"触发者刚发了三张无标注的图"，注意力有出处可循；就地重发每轮内容相同，命中缓存。

**预期成效**：15:11 那类"触发句没提图却点评三张旧图"消失；用户真要问「图 3 是谁」时模型和工具都能定位到它。

**缓存影响**：角色改变导致上线时窗口内现有 context_only 块断一次前缀；此后稳定。

### B-3 统一过期 + 容量滞后回收

**改动点**：`chat_prompt.py:269-364`（`_apply_image_gating` 重写为 `_apply_image_policy`）；`config.py`：`MULTIMODAL_IMAGE_FRESH_MINUTES` 默认改 60（运行配置已是 60），新增 `MULTIMODAL_MAX_IMAGES: int = 8`，删除 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`；`chat.py:325-331`（`_image_is_fresh` 死代码，改写为量化版本后复用）。

**怎么改**：
1. 遍历窗口内所有含图项（历史 user、context_only、触发 user），每张图取时间戳（user 项用 `item.timestamp`，context_only 用 `image_meta[i].timestamp`），`timestamp < cutoff` 者不可见：不注入 image 部件，文本改写为 `[图片已过期]`。`cutoff = floor((now − FRESH) / 30min) × 30min`。
2. 统计可见图片总数，超过 `MULTIMODAL_MAX_IMAGES` 时按时间最旧优先剥离，直到 `MULTIMODAL_MAX_IMAGES // 2`，被剥离的同样改写为 `[图片已过期]`。触发消息自身的图片不参与剥离。
3. 可见集合确定后交给 B-1 分配显示编号。
4. 过期改写只发生在请求渲染层，不改持久化数据；同一时间戳在两次请求中得到相同渲染。

**原因**：现状历史图"不限窗口、不限时效"；上限以消息计且把触发消息算进去，配置语义模糊。1 小时统一过期是产品决定；半小时量化把断前缀与重编号压到每半小时至多一次。

**预期成效**：图片存活 60 到 90 分钟，期间始终可见、可查、可缓存；整点和半点统一退场并重编号，请求体积与编号都有上限。

**缓存影响**：过期与回收都是"最旧位置的内容改变"，会从该位置断一次前缀，重编号搭同一次便车。频率：每半小时至多一次、每 4 张新图至多一次，与现有压缩摘要同级。

**配套**：`image_cache.py` 的总容量 50MB / 单图 10MB 要覆盖一小时内的图。QQ 私有域是 base64，缓存被 LRU 挤出后重下载若遇 rkey 过期会拿不到，此时该图退化为 `(已过期)` 文本并保持稳定。建议总容量提高到 200MB，并把"不在 prompt_messages 中的缓存清除"改为"不在窗口内且已过期的才清除"。

### B-4 工具可见图片表

**改动点**：`openai_func.py:267-272`（`_current_trigger_images` 属性）、`matcher.py:1208-1210`、`llm_tool_plugins/anime_trace.py:149-157`、`llm_tool_plugins/vision.py:49`、两者的 description 文案；`openai_func.py:1005-1017`（邮箱插入时的图片追加）。

**怎么改**：
1. `_current_trigger_images: List[str]` 改为 `_visible_images: Dict[int, str]`（显示编号 → URL），由 `_apply_image_policy` 在 B-1 分配显示编号时一并写入 `chat._visible_images`，matcher 在构建 prompt 后赋给 `tg`。
2. 邮箱插入的 entry 图片按 `_next_image_index` 续编的编号并入 `_visible_images`。
3. 工具按编号查表；查不到时返回「图片 N 已过期或不在当前上下文」。description 改为"N 为本次对话中出现的 [图片N] 编号，包括群聊上下文块里的图片"。
4. 视觉 profile 路径的 `_vision_context_images` 由 `_visible_images` 取代（B-1 已把其重编号逻辑推广为唯一路径）。

**原因**：模型能看见的图与工具能取到的图必须是同一集合、同一编号。

**预期成效**：anime_trace / vision 不再报"没有可用图片""序号超出范围"；用户问「图 3 是谁」时工具能查到 14:48 那张。

---

## 阶段 C：提示词定位 + 邮箱合并

### C-1 规则 6 / 7 改写

**改动点**：`chat_prompt.py:113-114`。

- 规则 6 末尾追加：`[群聊上下文-非触发消息] 块是其他群友之间的聊天背景，只用于理解语境；其中的话题、提问和图片，除非当前触发消息明确提到，否则不要主动回应或点评。`
- 规则 7 改为：`只回应当前触发消息（最后一条用户消息）的内容，回应对象是该消息的发送者；不要顺带回应历史中其他人的消息，不要把多个话题合并进一条回复。`

**原因**：模型没有任何依据判断 context 块的地位，把它当成待办清单。图片改为就地保留后，这条规则也是"背景图不主动点评"的唯一约束，与阶段 B 配套。

**预期成效**：不再主动接"调时钟到 6 点""花咲也发图了"这类背景消息，也不主动点评背景图。

**缓存影响**：S1 变一次，之后稳定。

### C-2 触发标记（临时 system，不落库）

**改动点**：`chat_prompt.py:174-187`（记忆提醒插入处）。

在最后一条非 context_only user 之前插入一条不落库的 system（与记忆提醒同槽位，并存时合并为一条）：

```
[当前触发] 下面这条是本轮需要回应的消息，来自 {sender}。上方的群聊上下文与其他人的历史消息只用于理解语境。
```

**原因**：尾部目前没有"回应谁"的指令；context_only 块改为 user 角色后，更需要显式标出哪条是触发。记忆提醒已验证这个槽位对各 provider 可用。

**缓存影响**：与记忆提醒相同，只影响尾部。

### C-3 邮箱批次合并为一条 user 消息 + 提醒改写

**改动点**：`openai_func.py:958-1034`（`_insert_mailbox_entries`）、`openai_func.py:304-331`（`_build_loop_user_message`）。

**怎么改**：
1. 一个打断点取空的全部 entries 合并为**一条** user 消息：文本为各 entry 的 `[HH:MM] sender: text` 按到达顺序换行拼接；图片编号从 `_next_image_index` 起对整批连续续编，图片部件按行顺序追加；`_user_text_already_in_messages` 去重改为按行去重后再合并。
2. 临时 system 提醒改写：

```
reply_completed=True:
[新消息提醒] 你上一条回复之后新到 {count} 条消息，见下一条。请逐条分别回应：每条各自成段、开头点名对象，不要把不同人的话题并进同一句；与新消息无关的旧上下文不要牵扯。

reply_completed=False:
[新消息提醒] 你上一条消息的回复尚未完成，请先完成它并单独成段；之后对下一条中的 {count} 条新消息逐条分别回应，每条各自成段、开头点名对象。
```

3. `_entries_for_next_reply` 仍记录整批 entries，供 A-2 归属与漫画兜底使用。

**原因**：多条 user 连发加「请一并回应」让模型把几个人的话揉成一段；合并为一条带行标的消息，模型把它当作"一批待回应的群消息"，配合分段要求逐条作答。

**预期成效**：批次回复以人为单位分段，读者能看出哪段回谁；历史里（A-1）每条回复各自成行。

**缓存影响**：循环内消息本就不入前缀，无影响。

### C-4 段数对齐

**改动点**：`chat_prompt.py:108`、`config/naturel_gpt_config.yml:137`。

规则 1 的"最多3段"改为 `f"最多{config.REPLY_MAX_SEGMENTS}段"`；运行配置 `REPLY_MAX_SEGMENTS` 从 5 调到 3；邮箱批次回复在提醒文本里放宽为 `3 + count` 段。

**原因**：规则说 3 段，发送侧放 5 段，多出来的空间就被无关话题填满。

---

## 阶段 D：context_only 块按时间衰减

**改动点**：`matcher.py:117-162`；`config.py` 新增 `CONTEXT_BUFFER_MAX_AGE_MINUTES: int = 15`、`CONTEXT_BUFFER_MIN_LINES: int = 3`。

flush 时丢弃距 flush 时刻超过 `CONTEXT_BUFFER_MAX_AGE_MINUTES` 的条目，但至少保留最后 `CONTEXT_BUFFER_MIN_LINES` 条。条数上限维持现有 `_context_buffer_limit()`。

**原因**：安静群里一块能跨半小时，早已翻篇的话题仍然贴在触发消息旁边。

**缓存影响**：无（该块每轮都是新增内容）。

---

## 验证（精简）

1. **回放一次**：用 `group_620260076.latest.json` 的 `loop_messages` 重建 15:11 触发的 prompt，检查三点：触发消息不带 image 部件；14:48 的三张图出现在 context_only 块内、编号为 1 到 3 且与 `_visible_images` 一致；尾部有 `[当前触发]`。再把时钟拨到 16:00 之后重建一次，三张图应变为 `[图片已过期]`，其余可见图从 1 重编。
2. **A-1 一个断言**：`[user, user, assistant, assistant]` 经 `_serializable` → `_load_from_dict` 后四条都在。纯函数，一行即可，不另起冒烟脚本。
3. **线上观察一周**：看 `latest.json` 中触发消息的 image 部件数量（应恒为触发自身图片数）、每轮请求 `cached_tokens` 比例（应不低于改动前）、回复段数分布。

## 落地顺序与工作量

| 顺序 | 阶段 | 预估 | 说明 |
|---|---|---|---|
| 1 | A-1、A-2 | 半天 | 数据正确性，无依赖 |
| 2 | B-2 → B-3 + B-1 → B-4 | 1 天 | B-3 与 B-1 在同一函数内完成（先定可见集合再编号）；B-3 依赖 B-2 的 `image_meta`；B-4 依赖 B-1 的编号表 |
| 3 | C-1 → C-2 → C-3 → C-4 | 半天 | 独立于 B |
| 4 | D | 1 小时 | 独立 |
| 5 | AGENTS.md / README 配置项同步 | 1 小时 | 删 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`，增 `MULTIMODAL_MAX_IMAGES`、`CONTEXT_BUFFER_MAX_AGE_MINUTES`、`CONTEXT_BUFFER_MIN_LINES` |

## 不做清单

- 不改并发合并路径（`matcher.py:1019-1039`）：注释已说明"实际很难命中"，日志中仅 17:45 一例。
- 不改邮箱 entry 的落库时机：A-1 保证不丢，C-3 的行标让模型能分清对象。
- 不做触发句关键词门控的任何变体：v2 用就地保留 + 规则约束替代，不再猜触发句。
- 不引入"每个邮箱 entry 单独生成一轮"：C-3 合并插入已覆盖。
