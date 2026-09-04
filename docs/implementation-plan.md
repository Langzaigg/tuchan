# naturel_gpt 实施计划（缓存冻结 + 上下文结构修正 + 工具链收敛）

依据：`docs/audit-context-and-tools.md` 及补充审计。本文是可执行的实施计划，按阶段组织，每项标注改动文件/函数与验证方式。

## 设计约束（本次确认）

- **20k 仅为设计参考预算**：用于定型各区块的目标体积（见附录 A），**运行时不新增任何输入/输出截断**；现有 `CONTEXT_TOKEN_BUDGET=32768` 兜底裁剪保持原样不动。
- **`TOOL_CONTEXT_TOKEN_BUDGET=16384` 不调整**，工具轮允许超出参考预算。
- **移除过时设计**：`TOOL_CONTEXT_MODE` 1/2 分支及配套死代码、`/no_think` 模型名判断、伪造编号检测重试链、旧 `Chat._context_buffer` 兼容路径——只保留当前成熟方案（模式 3 行为 + 输出侧清洗兜底）。

## 目标 prompt 结构（冻结后）

```
tools: 常驻全量工具集（画图 knowledge 在 generate_anima_image 的 description 内）  ← 每群静态锚点
[system] S1: 角色设定 + 响应规则 + 工具规则 + 画图行为短规则（3-5 条）              ← 仅人格热加载/开关切换时变
[system] S2: extra_prompt（非空时）                                                ← 仅 profile 切换变
[system] S3: 压缩上下文摘要（非空时）                                               ← 摘要任务成功时变（低频）
[历史: （印象 system?）user / assistant / 工具结果摘要 / context_only …]            ← 严格 append-only
[system] 当前状态：群记忆 + 记忆提醒 + 日期                                         ← 高频变，贴近尾部
[system] context_only（本轮新 flush，非空时）                                       ← 每轮新增一条
[user] 触发消息
```

缓存不变式：**第 k 轮请求的 prefix 是第 k-1 轮的严格前缀**（除尾部状态块之后的内容）。所有改动以守住这条不变式为验收标准。

---

## 阶段 0：Bug 修复（独立小改，先行）

| # | 问题 | 改动点 | 内容 |
|---|---|---|---|
| 0-1 | 多 key 轮询失效 | `openai_func.py:325-348` `_request_state` | profile 路径固定 `api_keys[0]`，改为 per-profile 轮换索引；请求失败 `_rotate_key()` 时推进对应 profile 的索引 |
| 0-2 | 节流放弃留下孤儿触发轮 | `matcher.py:965-988` | 节流 sleep 后判定放弃时，回滚已写入的触发 user（`remove_last_prompt_user_message`）或改标 context 性质 |
| 0-3 | `"token" in raw_res.lower()` 误判清历史 | `matcher.py:1373` | 改为匹配 provider 错误码/明确的 context-length 错误特征（如 `context_length`、`maximum context`），一般错误不再清历史 |
| 0-4 | `/no_think` 按模型名含 "3" 触发 | `chat_prompt.py:113`、`config.py` | 改为 profile 显式字段 `no_think: bool`（默认 false）；`Config`/`CONFIG_TEMPLATE`/README 同步 |
| 0-5 | 文档漂移 | `AGENTS.md` | `MAX_TOTAL_TOOL_CALLS` 7→15；`THINK_LEAK_THRESHOLD` 默认口径统一（150） |

验证：语法编译 `openai_func.py`、`matcher.py`、`config.py`；手测 `rg model` 切换 profile 后失败换 key 生效。

---

## 阶段 1：缓存冻结（核心收益）

### 1-1 画图工具与 knowledge 常驻 + 拆分搬家

**改动点**：`openai_func.py:838-862`、`chat_prompt.py:133-192`、`llm_tools.py:38-46`、`llm_tool_plugins/anima_generate.py`

- `draw_mode != off` 时 `generate_anima_image`/`danbooru_search` **常驻注册**；删除 `openai_func.py:845-848` 的 `_DRAW_ONLY_TOOLS` 关键词过滤（`off` 仍卸载，`rg draw off` 行为不变）。
- `chat_prompt.py` 删除 S2 画图知识注入与 `_has_recent_draw_activity` 惯性扫描（133-161），S2 只保留 `extra_prompt`。
- `anima_generate._build_workflow_knowledge`：所有工作流统一走 base 同款压缩（expert 去默认参数、artist 只留列表、examples ≤3），目标 knowledge ≤ 2k token；产物拼入 `generate_anima_image` schema 的 `function.description`（随 `_schema_cache` 缓存）。`_COMMON_DRAW_RULES` 中属于行为约束的部分（禁止编造编号、先 tool_calls 后引用）移出，压缩为 3-5 条常驻 S1 工具段（`chat_prompt.py:71-79`）；参数文档部分随 knowledge 进 description。
- 漫画模式：`MANGA_RULES`（+解锁时 `MANGA_UNLOCK_RULES`）追加到动态默认工作流 schema 的 description 末尾；`chat_prompt.py:167-179` 的漫画 knowledge 注入同步删除。
- `auto` 语义变更：从"按关键词给/不给工具"改为"常驻 + description 写明触发时机"。`rg draw force` 的语义见 1-2。

**风险**：闲聊轮暴露画图工具，误调用率可能上升——description 里写清"仅当用户明确要求作画时调用"；观察一周，若误调用明显再评估。

### 1-2 force 模式用 `tool_choice` 替代伪造编号检测链

**改动点**：`openai_func.py:850-862, 981-996, 1088-1104`、`matcher.py` 相关

- `draw_mode == "force"` 且当前消息含画图关键词时，请求带 `tool_choice={"type":"function","function":{"name":"generate_anima_image"}}`，替代"预注引导 system + 伪造正则检测 + 斥责重试"。
- 删除：`_FAKE_TASK_ID_*` 正则、`_contains_fake_draw_reply`、`_fake_retry_count` 重试、thinking 检测强制重试（`_force_tools_next`/`_thinking_check_done`）、`_enable_intercept` 缓冲拦截链。
- **保留**：输出侧 `sanitize_draw_reply_text`（发送/保存兜底清洗）与历史中编号隐藏占位符（`chat_prompt.py:15-22`，防回显诱导）。
- **兼容兜底**：provider 不支持指定函数 `tool_choice`（400）时回退为现有提示注入路径，仅一次。

### 1-3 工具循环内不改 tools 数组

**改动点**：`openai_func.py:873-878, 1050-1060, 1157-1209`

- 最后一轮（`is_last_round`）不再 `tools=None`，改为 tools 数组保持不变 + `tool_choice="none"`（该字段不进 token 前缀，缓存保住）。
- 终端工具轮（`_allow_terminal_tools`）不再裁剪 tools 到 TERMINAL_TOOLS 子集，依赖已注入的限制提示（现状已有）；`TOOL_CONTEXT_TOKEN_BUDGET` 触发逻辑不变。
- 搜索超限维持"提示延后到 tool 响应后插入"的现状（`openai_func.py:1241-1245`，审计确认正确，不动）。

### 1-4 工具循环终止保护（轮数 + 时间双闸门）

**改动点**：`openai_func.py:869-878, 959-964, 1157-1263`、`config.py`

现状闸门（全部保留）：轮数 `LLM_MAX_TOOL_ROUNDS`（remember-only 轮不计数，`1261-1263`）；总次数 15 超限进终端轮（`1157-1209`）；工具上下文超 `TOOL_CONTEXT_TOKEN_BUDGET` 进终端轮（`1248-1254`，预算值不动）；每轮流式 300s 上限（`619-630`）。

已核实盲区（均可无界循环）：

- **终端轮无次数上限**：进入 `_allow_terminal_tools` 后总次数检查被跳过（`1157/1197` 的 `not _allow_terminal_tools` 条件），模型每轮调 `remember`/`generate_anima_image` 都会被放行执行、`round_idx` 冻结在 `max_rounds` → 无限循环，且画图调用每次都真实提交 ComfyUI 任务；
- 参数 JSON 全丢路径 `continue` 不计任何计数（`959-964`）；
- remember-only 轮不计轮数（`1261-1263`），最终汇入第一条。

改造（两级保护）：

- **进入终端轮**（任一命中即进）：轮数 ≥ `LLM_MAX_TOOL_ROUNDS` / 总次数 > 15 / 工具上下文超预算 / **循环总耗时 > `LLM_TOOL_LOOP_MAX_SECONDS`（新增配置，默认 180，CONFIG_TEMPLATE/README 同步）**；计时点 `stream_response` 入口记 `loop_start = time.monotonic()`，每轮 loop 头部检查；**耗时超 300s 同样只进终端轮——删除原"携部分文本强制返回"的硬兜底，所有超时路径统一为进终端轮优雅收尾**；
- **终端轮规则（终端轮自身不设任何计数/预算限制）**：模型在终端轮要么直接返回文本（循环结束），要么发起**单次**终端工具调用——执行完毕后进入无工具收尾轮（配合 1-3 用 `tool_choice="none"`）产出最终文本并结束循环；终端阶段维持现有跳过总次数/工具预算/轮数检查的逻辑不变；
- **已产出文本全保留**：进入终端轮前的所有中间文本（`intermediate_texts`）与收尾轮产出经 `_join_intermediate` 合并后，作为**正常成功回复**返回，matcher 按成功路径 `update_chat_history_row(is_bot_reply=True)` 写入历史；收尾轮产出为空时以已累积中间文本兜底——任何终止路径都不丢弃已产出内容、都必须进历史记录（替代原失败路径的 `raw_res_for_save` 部分保存逻辑）；
- 畸形 JSON 重试加独立计数器（≤ 1 次），超限撤工具转文本。

### 1-5 S3 易变内容下沉

**改动点**：`chat_prompt.py:194-203`、`_build_openai_history_messages` 尾部

- 群记忆 + 记忆提醒 + 日期从 S3 移到历史之后、本轮 context_only 之前，作为尾部"当前状态" system。
- S4 摘要保持在历史之前（语义上是历史的替代品）。
- S1/S2/S3 重新编号：S1 静态 / S2 extra_prompt / S3 摘要。

### 1-6 context_only append-only

**改动点**：`chat_history.py:74-105`、`matcher.py:990-1015`、`chat_summary.py` 裁剪段

- `update_chat_history_row` 不再"删除全部旧 context_only"，每轮 flush 在触发 user 前**追加**一条新 context_only；context_only 视为普通历史条目，**取消其裁剪/摘要豁免**，随所在区间被窗口裁剪或摘要删除一起自然淘汰（当轮新 flush 的 context_only 随触发轮存续；不淘汰会导致存储无界增长）。
- 删除旧 `Chat._context_buffer` 兼容路径（`chat.py:62-118`），只留 `_recent_context_buffers` 一条链路。
- 附带收益：非触发群聊上下文从"一轮记忆"变为窗口内连续可见。

### 1-7 打断方案重构：插入式 agent 循环

**改动点**：`matcher.py:830-960, 1485-1521`、`openai_func.py` 主循环与 `_pending_merge_input`（`1324-1354`）、`chat.py:120-128`

现状（全部删除）：

- 新消息到达 → cancel 旧任务（`matcher.py:958-960`）；CancelledError 捕获部分回复存 `Chat._last_interrupted_response`，下次请求以 `[上一轮被中断的回复]` 注入 context_only（`matcher.py:1501-1514`）；
- 工具调用阶段不可打断，新输入入 `TextGenerator._pending_merge_input`，旧请求完成后递归 `do_msg_response`（`matcher.py:1485-1499`）；
- 打断导致模型看不到自己说了一半的话，重发内容重复、上下文断裂。

新方案（邮箱插入式循环）：

- **per-chat 邮箱**：新增 `TextGenerator._loop_mailbox[chat_key]: deque`；新触发消息经 matcher 锁内判定后**不再 cancel 任务**，改为写入邮箱；user 消息仍由 matcher 侧先 `update_chat_history_row` 写入 `prompt_messages`（保证格式、`user_id`、时间戳正确），再入邮箱；
- **轮边界批量插入**：`stream_response` 主循环在每个轮边界（一轮流式结束、工具执行完毕后）检查邮箱；**一次性取空邮箱**，新输入作为**多条独立 user 消息**一次性 append 到 `messages` 最新位置（不合并成一条，保留各自 sender/时间戳），**重置 `round_idx` 与循环计时**（`loop_start`），继续循环；刚产出的回复文本作为 assistant 消息一并入列——模型完整看到自己说过的话，消除中断重复；
- **未处理标记**：插入批次前加一条 ephemeral system 提示（仅存在于当次循环，不落历史），让模型明确这批消息全部未处理：`[新消息提醒] 以下 N 条是尚未处理的新消息，请一并回应`；若上一轮以 tool_calls 结束（即被插入打断的那条触发**尚未完成回复**），提示必须明确包含这一点：`你上一条消息的回复尚未完成，请继续完成它；以下 N 条新消息也均未处理`。
- **日志记录（方便 debug）**：邮箱写入（chat_key、sender、文本截断预览）、轮边界批量消费（批量大小、senders、上一轮是否未完成回复）、终端轮因邮箱非空跳过插入、`on_reply_complete` 逐轮落库事件，均打 INFO 日志（风格仿现有 `触发回复 | 会话: ...`）；插入批次的完整内容随下一轮 prompt 快照自然进入 `{chat_key}.latest.json` debug 日志，无需额外文件。
- **逐轮落库（保留全部返回文本）**：每个完成的回复通过新回调 `on_reply_complete(text, tool_messages)` 立即通知 matcher 落历史（assistant 消息 + `save_tool_messages` + 摘要触发 + 统计），不等到整个循环结束；分段发送沿用现有 `on_text` 流式路径——**之前轮次的返回文本全部保留且全部进入历史记录**；
- **终端轮不接收插入**：进入终端轮后邮箱有新输入时，先完成终端收尾结束本次循环，邮箱输入由 matcher 走新请求正常处理；
- 保留锁内"尚未 recorded 的输入文本合并"（`matcher.py:905-925`，循环未启动前的合并场景）；插入时更新 `trigger_userid`/sender 上下文为最新触发者（记忆工具 user 维度归属最新发言者），request_profile 快照保持不变；
- 删除：cancel 路径、`_pending_merge_input`、递归处理、`_last_interrupted_response` 注入。

风险与验证：

- 单任务生命周期变长，异常路径必须保证已产出回复全部落库（`on_reply_complete` 逐轮落库 + `finally` 兜底）；
- 验证场景：回复流式中途插入新消息、工具调用中插入、连续两条插入——确认历史 user/assistant 交替正确、每轮回复独立落库、摘要触发正常、无重复回复、终端轮不被插入打断。

**阶段 1 验证**：
- 连续构造 3 轮 prompt 手工 diff，确认 append-only（除尾部块）；
- 编译 `chat_prompt.py`、`chat_history.py`、`chat_summary.py`、`openai_func.py`、`matcher.py`、`anima_generate.py`；
- 手测矩阵：`rg draw auto/on/off/force` ×（闲聊句/画图句/漫画模式）；force 模式确认 tool_choice 生效且无编号编造；
- 终止保护验证：构造模型连续只调 `remember` 的场景，确认终端轮单次调用后强制无工具收尾并结束、已产出文本完整返回且写入历史；畸形 JSON 重试 ≤1 次；循环总耗时超阈值（含 300s 绝对线）均进终端轮收尾，无强制丢弃路径；
- 上线后看 `cached=X(xx%)` 日志，预期命中率从近 0 提到 60-80%。

---

## 阶段 2：行为质量

### 2-1 reasoning_content 循环内保留（实质 bug）

**改动点**：`openai_func.py:400-407`

- `_completion_kwargs` 不再无条件剥离 assistant 的 `reasoning_content`：**同一 user turn 的工具循环内保留**（多步工具任务思考链连贯）；跨轮持久化历史中的 reasoning 是否携带按 profile 新字段 `keep_reasoning: bool`（默认 false，兼容不接受该字段的 provider，400 时自动剥离重试一次）。
- 相关死代码清理见 2-3。

### 2-2 工具结果策略定型（模式 3 唯一化）

**改动点**：`chat_prompt.py:544-688`、`chat_summary.py:122-281`、`config.py`

- 删除 `TOOL_CONTEXT_MODE` 配置及模式 1/2 分支：tool_groups 组装（621-681）、`messages = normal_messages + tool_messages` 尾部拼接（688）、`is_empty_tool_call` 死分支（585-607 中不可达部分）、budget_messages 循环（654-671）。
- 保留模式 3 行为为唯一路径：历史 tool 原文不进 prompt，assistant(tool_calls) 后以 system 注入结果摘要。
- **摘要同步化，禁止异步回写**：`generate_tool_call_summary` 改为同步生成一次终稿（tavily 有 AI answer 直接用，否则用截断 fallback；搜索类需要 LLM 摘要时在 `save_tool_messages` 后同步 await 一次 mini 调用再返回），删除 fallback→LLM 覆写两步写。
- S1 rules[6]（"[搜索工具摘要] 不是回复格式"补丁）在观察无模仿行为后删除。

### 2-3 工具集收敛 17 → 9

**改动点**：`llm_tools.py`、`llm_tool_plugins/`、`config.py`

- 搜索：只暴露 `tavily_search`；`bocha_search` 改为 tavily 失败时的服务端内部 fallback（不再注册独立 schema），`should_load` 动态注册逻辑删除。
- 抓取：保留 `browse_url`，吸收 `fetch_url`（当前已禁用，直接下线）与 `tavily_extract`（作为 `browse_url` 的 `mode` 参数或内部策略链一环）。
- bangumi 5 合 1：合并为 `bangumi(action=search_subject|get_subject|search_character|search_person|calendar, ...)`，参照 `remember` 的 action 模式。
- 低频工具 `nas_game_list`（白名单群）、`anime_trace`、`pixiv_search`、`danbooru_search` 保留但评估常驻必要性；不常用群用 `LLM_DISABLED_TOOLS` 管理。
- S1 工具段中"教模型选工具"的长句（`chat_prompt.py:77`）精简。

### 2-4 ~~工具并发执行~~（已取消）

用户确认工具执行不需要并发，`_execute_tool_calls` 保持 for 串行现状，本项不做。

**阶段 2 验证**：多步搜索任务（连续 2 轮以上工具调用）观察连贯性；`rg` 全指令回归；编译所有 `llm_tool_plugins/*.py`；配置迁移验证（旧 `TOOL_CONTEXT_MODE` 字段被忽略不报错、不丢其他字段）。

---

## 阶段 3：工程效率（可选，按收益择机）

| # | 内容 | 改动点 |
|---|---|---|
| 3-1 | token 计数缓存：按消息内容 hash 缓存单条 token 数，消除裁剪 while 循环的 O(n²) 重编码（event loop 同步阻塞） | `openai_func.py:1378-1393`、`chat_prompt.py:662-741` |
| 3-2 | 图片 token 估算 85→可配置常量（默认 1000），仅用于 32k 兜底裁剪与日志，不新增截断 | `openai_func.py:1385` |
| 3-3 | `stream_response` 入口 deepcopy 改浅拷贝+按需拷贝（避免每轮复制 base64 图片串） | `openai_func.py:801` |
| 3-4 | 压缩参数放缓：`CONTEXT_WINDOW_SIZE` 6→10、`CONTEXT_COMPRESS_THRESHOLD_RATIO` 2.0→1.0（降低全量失效频率；纯配置调整，先灰度一群） | `config/naturel_gpt_config.yml` |
| 3-5 | 印象生成 for 串行改 `asyncio.gather`；`save_to_file` 移 `asyncio.to_thread`（11MB JSON dump 阻塞 event loop） | `chat_summary.py:546-626`、`persistent_data_manager.py:492-515` |
| 3-6 | 图片 URL 直传优先（provider 支持时），base64 仅 fallback | `image_cache.py`、`chat_prompt.py:242-256` |

---

## 阶段 4：防回归

- **前缀稳定性单测**：构造连续 N 轮 prompt，断言第 k 轮是第 k-1 轮的严格前缀（尾部状态块之后除外）；覆盖：普通轮、含 context_only 轮、含工具调用轮、摘要完成后首轮。
- **AGENTS.md 同步**：本计划落地后更新受影响条目——系统消息结构（S1-S4 重编号）、knowledge 注入位置、context_only append-only、工具模式唯一化、`tool_choice` 机制、循环终止双闸门（1-4）、插入式打断/循环邮箱（1-7）、移除项清单（含 `_last_interrupted_response`、`_pending_merge_input`、伪造编号检测链）。
- 灰度策略：每阶段先在 1 个群开启观察 2-3 天（重点看 `cached%`、误调用率、回复风格漂移），再全量。

## 不做清单（明确排除）

- 不新增运行时输入/输出截断机制；不调整 `TOOL_CONTEXT_TOKEN_BUDGET`。
- 不动按轮裁剪 + 印象绑定轮次的设计（审计确认正确）。
- 不动孤立工具消息清理、搜索超限提示延后插入、流式 tool_calls 分片重组（审计确认正确）。
- 不做图片门控逻辑重构（`_apply_image_gating` 现状保留）。
- 不缩减搜索工具结果条数（tavily `max_results=20` 维持，代价接受）。

## 落地顺序

阶段 0（1 天量级）→ 阶段 1（核心；1-2/1-3/1-4 同处 `stream_response` 主循环，一起改避免冲突；1-7 打断重构体量最大，在 1-1~1-6 上线稳定后单独实施）→ 观察缓存命中率一周 → 阶段 2 → 阶段 3/4 择机。

---

## 附录 A：20k 设计参考预算（非运行时限制）

用于定型目标体积，基于线上 `latest.json` 实测（cl100k）：

| 区块 | 现状实测 | 设计目标 | 达成手段 |
|---|---|---|---|
| tools 块（schema 合计） | 4–5k | ≤ 2.5k | 阶段 2-3 收敛 17→9 |
| 画图 knowledge | 1.6–3.2k | ≤ 2k | 阶段 1-1 统一压缩 |
| S1（默认人格） | 1.6–1.75k | ≤ 1.8k | 规则去重 |
| S3 摘要 | 0.5–1.6k | ≤ 1.2k | 现状已达标 |
| 尾部状态块（记忆+日期） | 0.01–3.2k | 典型 ≤ 1k | 群记忆接近上限时提前 consolidate 提醒（不加硬截断） |
| 历史+context_only+印象 | 2.8–5.5k | ≤ 8k 自然水位 | append-only + 压缩参数放缓（3-4） |
| 典型单次输入 | 6.4–13.5k | ≤ 20k | 综合上述 |
