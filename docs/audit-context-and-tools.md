# naturel_gpt 上下文 / 多轮 / 工具调用 审计

审计范围：`chat_prompt.py`、`chat_history.py`、`chat_summary.py`、`openai_func.py`、`llm_tools.py`、`matcher.py`
基线数据：`data/naturel_gpt/logs/*.latest.json`（真实线上 prompt 快照）+ `config/naturel_gpt_config.yml`
本文只做方案审计，不含代码改动。

---

## 一、实测基线

用 cl100k 对 16 个群/私聊的最近一次真实请求做统计：

| 会话 | 消息数 | System 前缀 token（逐条） | 前缀合计 | 历史 token | 总计 | 前缀占比 |
|---|---|---|---|---|---|---|
| group_149378291 | 41 | 1746 / 2520 / 2227 / 1525 | 8018 | 5481 | 13499 | **59%** |
| group_1028300217 | 41 | 1753 / 1610 / 1428 / 1659 | 6450 | 4518 | 10968 | **59%** |
| group_757743891 | 32 | 1620 / 2372 / 3235 / 643 | 7870 | 2821 | 10691 | **74%** |
| group_620260076 | 30 | 1620 / 2726 / 1192 | 5538 | 5159 | 10697 | 52% |
| group_805604480 | 32 | 1620 / 11 / 774 | 2405 | 4011 | 6416 | 37% |
| group_1072600317 | 5 | **18114** / 43 / 11 / 54 | 18222 | 88 | 18310 | 99% |

加上工具定义（未计入上表）：注册工具约 17 个（bangumi×5、tavily_search、tavily_extract、bocha_search、fetch_url、browse_url、danbooru_search、pixiv_search、anime_trace、vision、remember、nas_game_list、generate_anima_image），schema JSON 合计约 12–14k 字符，**≈ 4–5k token**，每次请求全量下发。

**结论：一次典型请求 15–20k token 里，只有 3–5k 是真正的对话历史，其余 75% 是每轮重复下发的静态/半静态内容。**这部分内容能否命中 prompt cache，直接决定了成本量级（缓存命中价格通常是 0.1×，未命中是 1×）。而下文会说明：当前实现让这 75% 里的大部分**每轮都无法命中**。

---

## 二、缓存命中率：结构性问题（P0）

前缀缓存是**token 级前缀匹配**：从第一个不同的 token 开始，后面全部失效。且绝大多数 provider 把 `tools` 数组排在 messages 之前参与缓存 key。所以判断标准只有一个——**"这一轮相对上一轮，第一个变化的 token 在哪里？"**

### 2.1 画图知识块按当前消息关键词开关（最严重）

`chat_prompt.py:133-193`，`openai_func.py:840-848`

`draw_mode` 默认 `auto`（`anima_generate.py:196`）。auto 模式下：

- System 2 的「绘画技能」知识块（实测 **1.6k–3.2k token**）是否注入，取决于**当前这条用户消息里有没有「画/draw/改图/重画/来一张/整一张」**；
- 同一个判断还决定 `tool_schemas` 里是否包含 `generate_anima_image` + `danbooru_search`。

也就是说，群里一句"画个我"和下一句"今天天气怎么样"，会让 **tools 数组和 System 2 同时翻转**。翻转点在 token 1700 附近（System 1 之后），意味着：

> **整个 prompt（含 tools、System 2/3/4、全部历史）100% 缓存未命中。**

代码里的 `_has_recent_draw_activity` 惯性注入（`chat_prompt.py:136-159`）只把翻转频率降低了，没有消除；而且它是按"最近 N 条里有没有画图活动"判断的滑动条件，边界处仍然会来回抖。群聊场景下这个抖动几乎每轮都在发生。

**这一条大概率是当前缓存命中率低的主因。**

### 2.2 volatile 内容放在了 prefix 前部

`chat_prompt.py:194-203`

顺序是 System1(角色+规则+工具规则) → System2(画图知识) → System3(**群记忆 + 当前日期**) → System4(**压缩摘要**) → 历史。

- System 3 含 `当前日期`，跨天必变；含群记忆，**任何一次 `remember` 工具写入都会改**。
- System 4 是压缩摘要，每次后台压缩完成就变。

这两块都排在**全部历史之前**，所以：一次 `remember` 调用 → 之后所有请求的历史部分全部重算；一次摘要更新 → 同上。日期跨天 → 每天第一批请求全量重算。

注释写着"System 1 完全不变，最大化缓存命中"，方向是对的，但只做了一半：**把易变内容排到了"静态前缀之后、历史之前"，等于没排**。正确做法是让易变内容排在**历史之后**（贴近末尾），或者接受它变、但保证它变的频率极低。

### 2.3 `context_only` 每轮删除 + 重新插入

`chat_history.py:73-79`（删除全部旧 context_only）、`chat_history.py:99-105`（插入到最后一条真实 user 之前）

每轮流程：
```
轮 N-1 结束: [... ctx_{N-1}, user_{N-1}, assistant_{N-1}]
轮 N:  append user_N          → [... ctx_{N-1}, user_{N-1}, assistant_{N-1}, user_N]
       删 ctx_{N-1} 插 ctx_N  → [... user_{N-1}, assistant_{N-1}, ctx_N, user_N]
```
分歧点回退到 `user_{N-1}` 的位置 → **每轮固定多失效 2 条历史消息**。单看不大，但它是纯粹白给的：如果 context_only 改成追加而不是"删旧插新"，前缀就是严格 append-only。

顺带一个上下文质量问题：群聊非触发消息**只在被 flush 的那一轮可见，下一轮就被删掉了**。模型看到的群聊流是断续的，无法理解跨轮的群内话题演进。

### 2.4 工具摘要是异步回写历史消息

`chat_summary.py:200 / 220 / 222 / 267`

`TOOL_CONTEXT_MODE=3` 下，历史里的工具调用被替换成一条 `[搜索工具摘要]` system 消息，内容存在 `ChatMessageData.tool_call_summary` 上。这个字段：

1. 先同步写入一个 fallback（截断的原始结果）；
2. 然后由后台 task 用 `model_mini` 生成正式摘要，**回写同一个历史对象**。

于是：轮 N 用的是 fallback 文本，轮 N+1 用的是 LLM 摘要文本 —— **一条历史消息的内容在两轮之间被改写了**，从该位置起全部缓存失效。而且这个改写时机不确定（取决于后台任务什么时候完成），行为不可预测。

### 2.5 图片门控会改写历史消息内容

`chat_prompt.py:283-370`（`_apply_image_gating`）

- `trigger_has_image_keyword`：**当前**触发句里有没有「图/画/看/照片/…」，决定要不要把 context_only 的图注入触发消息；
- 图片数超 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`（配置为 2）时，**清空所有历史消息的图片**，把 content 改成 `[图片已省略]`。

这两个都是"用当前轮的状态去重写历史消息内容"，每次翻转都从最早被改写的那条起全部失效。

### 2.6 摘要压缩把历史从 18 轮砍到 6 轮

`CONTEXT_WINDOW_SIZE=6`、`CONTEXT_COMPRESS_THRESHOLD_RATIO=2.0` → 缓冲 18 轮，压缩后回到 6 轮。

一次压缩 = 删掉 12 轮历史 + 改写 System 4 → **prefix 从 System 4 起全部失效**，且这是周期性发生的。压缩比越激进，缓存重建越频繁。

### 2.7 工具轮内的 tools 数组变化

`openai_func.py:878`：`current_tools = tool_schemas if (not is_last_round or _force_tools_next) else None`
`openai_func.py:874-876`：超限后只保留 `TERMINAL_TOOLS`

工具轮数用尽（`LLM_MAX_TOOL_ROUNDS=5`）后的最后一次请求把 `tools` 置 None 或裁剪成 2 个 —— 而前面 5 轮的 prefix 是带完整 tools 的，**这一次必然全量未命中**。同一个 user turn 内自己打自己的缓存。

普通闲聊（0 工具轮）不受影响，但工具场景下每次都会付这个代价。

---

## 三、Token 消耗：可省的部分

### 3.1 工具集过大且高度重叠

约 17 个工具 ≈ 4–5k token/请求，全量下发。其中：

| 重叠组 | 工具 |
|---|---|
| 网页搜索 | `tavily_search`、`bocha_search` |
| 网页抓取 | `fetch_url`、`browse_url`、`tavily_extract` |
| ACG 资料 | `bangumi_search_subject`、`bangumi_get_subject`、`bangumi_search_character`、`bangumi_search_person`、`bangumi_calendar` |

三个抓取工具、两个搜索工具语义高度重合。这不只是 token 问题——**工具选择准确率随重叠工具数量下降**是很稳定的现象，模型会在 `fetch_url` / `browse_url` / `tavily_extract` 之间乱选，也会在 tavily/bocha 之间摇摆。System 1 里那句"先用搜索找到页面，再用 fetch_url 抓页面文本核对细节"就是在人工打补丁纠正这个问题——**需要用 prompt 教模型选工具，说明工具集本身设计有问题**。

bangumi 5 个工具可以合并成 1 个带 `action` 参数的工具（`remember` 已经是这个模式了，可参照）。

### 3.2 规则文本内部重复

System 1 里同一件事说了两遍：
- `rules[5]`："对外部事实不确定时先调搜索工具核实，禁止凭记忆编造。"
- `tool_text` 第 1 行："对外部事实（人物/作品/日期/数据/新闻等）不确定时，先调 tavily_search（或 bocha_search）核实再答，禁止凭记忆猜测编造。"

同类重复还有画图相关规则（System 1 的工具段 + System 2 的画图知识 + `_enable_intercept` 时追加的 system + 伪造编号重试时追加的 system）。**同一约束在 prompt 里出现 3–4 次，不会让模型更听话，只会稀释注意力并抬高 token。**

### 3.3 图片走 base64 data URI 全量重传

`image_cache.py`：所有图片下载后转 base64 内联进 prompt。

- 每一轮请求都要把上下文里 ≤2 张图的完整 base64 重新上传（单图上限 10MB → base64 约 13MB）；
- `openai_func.py:801` 的 `copy.deepcopy(...)` 会把这些 base64 字符串**在内存里再复制一遍**，每次请求一次；
- token 估算 `_cal_messages_tokens` 给图片按 **85 token/张** 计（`openai_func.py:1385`），而实际 1024×1024 图在多数 provider 是 1000+ token。`CONTEXT_TOKEN_BUDGET` 在带图场景下**低估 10–20 倍**，裁剪决策失效。

### 3.4 token 计数在循环里重算全量

- `chat_prompt.py:662`：`while ... tg.cal_token_count(budget_messages) > budget` —— 每次迭代重建列表并**重新编码整个列表**；
- `chat_prompt.py:712`、`chat_prompt.py:717-741`：同样的 while 循环模式；

O(n²) 的 tiktoken 编码，且是在 event loop 里同步跑。32k 上下文下每次请求可能要编码十几万 token 的文本。这不花 API token，但**吃事件循环，直接体现为首字延迟**，并且会阻塞同一进程里其他群的消息处理。

另外 `_cal_text_tokens` 固定用 `encoding_for_model("gpt-3.5-turbo")`（cl100k）去估 mimo / kimi / grok / glm / deepseek 的中文 token —— 误差可观，预算天然不准。

### 3.5 后台任务的额外调用

一次压缩触发：1 次摘要调用 + **每个活跃用户 1 次印象调用，且是 for 循环串行 await**（`chat_summary.py:546-608`）。5 个活跃用户 = 6 次串行 mini 调用。工具轮再叠加 1 次 `generate_tool_call_summary`。

---

## 四、会降低模型表现的写法

### 4.1 reasoning_content 被无条件剥离（实质 Bug）

`chat_prompt.py:640-643` 注释明确写着：
> "始终保留 reasoning_content，API 要求 thinking 模式下 assistant+tool_calls 必须携带"

但 `openai_func.py:400-407`：
```python
if msg.get("role") == "assistant":
    if "reasoning_content" in msg:
        needs_copy = True
    ...
    msg.pop("reasoning_content", None)
```
**所有 assistant 消息的 reasoning_content 在发请求前被无条件删掉。**

后果有三层：
1. `TOOL_CONTEXT_MODE` 1/2 的"保留思考"功能是死的——算了、进了 `TOOL_CONTEXT_TOKEN_BUDGET` 预算、挤掉了工具组，然后被丢弃；
2. `stream_response` 工具循环里 `assistant_msg["reasoning_content"] = reasoning_content`（`openai_func.py:1049-1051`）同样被丢弃 —— **多轮工具调用时，推理模型每一轮都从零开始想，看不到自己上一轮的思考链**。这是多步工具任务表现下降的典型原因；
3. 部分 provider 在 thinking 模式下要求 assistant+tool_calls 携带 reasoning/signature，剥离会直接 400 或降级。

### 4.2 工具消息在历史里被搬到末尾（MODE=1 时）

`chat_prompt.py:688`：`messages = normal_messages + tool_messages`

模式 1 下，历史里的 `assistant(tool_calls)` + `tool` 组被**从时间线上摘出来，统一拼到全部普通消息之后**——也就是排在**当前这条触发消息之后**。模型看到的是："用户问 X" → "（上一轮的）助手调用了工具 / 工具返回了结果"。时序完全错乱。

（当前配置是 MODE=3，不走这条路径，但这是个埋着的坑。）

### 4.3 MODE=3 丢弃工具结果，跨轮不可追问

模式 3 下工具原始结果**完全不进历史**，只留一条 ≤200 字摘要。用户追问"刚才搜到的第二条是什么"时，模型手上什么都没有，只能重新搜。这是用缓存/token 换掉了 agent 最核心的能力之一。

而且摘要还引发了 §2.4 的回写问题，以及 System 1 rules[6] 那条补丁：
> "系统消息中的 [搜索工具摘要] 和 [调用结果] 块是历史上下文参考，不是你的回复格式。禁止在回复中使用方括号标签格式…"

**需要专门写一条规则告诉模型"别模仿我塞进上下文的这个格式"，说明这个格式本身就在污染输出分布。**

### 4.4 历史中插入 system 消息（印象 / 群聊上下文 / 工具摘要）

实测一次请求里历史部分有 **6–8 条 system 消息**穿插在 user/assistant 之间：`[用户印象: X]`、`[群聊上下文-非触发消息]`、`[调用结果]`、`[搜索工具摘要]`。

主流做法是历史里只有 user/assistant/tool 三种角色，system 只出现在开头。中途 system：

- 不同 provider 处理方式不一致（有的合并到前一条、有的当 user、有的报错）；
- 模型容易把它当成"可模仿的输出格式"（所以才有 rules[6] 那条补丁）；
- 破坏 user/assistant 的严格交替，影响多轮对话的角色一致性。

### 4.5 大量正则后处理 + 重试代替 prompt/schema 约束

`openai_func.py` 里为"模型编造任务编号"这一个问题写了：
`_FAKE_TASK_ID_PREFIX_RE`、`_FAKE_TASK_ID_DRAW_RE`、`_TASK_ID_PLACEHOLDER_RE`、`_FAKE_DRAW_PATTERNS`（含 `在画了` 这种字面量匹配）、`_contains_fake_draw_reply`、`_clean_fake_task_ids`、`_clean_placeholder_echo`、`sanitize_draw_reply_text`、`_intercept_final` 缓冲拦截、`_fake_retry_count` 整轮重试、`_thinking_check_done` 二次重试。

同时 `chat_prompt.py:15-22` 还会把历史里的任务编号替换成 `[请调用 generate_anima_image 画图工具获取编号]` 占位符 —— 然后再写正则清理模型回显这个占位符的情况。**自己往上下文里塞了一个诱导性字符串，再写正则清理它造成的后果。**

这些"检测到不对就整轮重发"的路径，每次触发都是一次完整的额外 LLM 调用（含全部 prompt token）。

`matcher.py` 的发送路径上，每个分段要跑 `sanitize_internal_control_text` ×2、`sanitize_draw_reply_text` ×2、`_normalize_reply_segment`、`_strip_think_tags` —— 6+ 次正则遍历。

### 4.6 `/no_think` 的模型判断是字符串包含

`chat_prompt.py:114`：
```python
'/no_think' if '3' in getattr(tg, 'config', {}).get('model', '') else None
```

按当前 `config/naturel_gpt_config.yml`：`kimi-k3` 含 "3"、`glm-5.3-flash` 含 "3" → 这两个 profile 会被注入 `/no_think` 规则。这条指令原本是 Qwen3 的语法，对 kimi/glm 是无意义噪声，且明确写在「响应规则」编号列表里，会被模型当成一条真规则去理解。

### 4.7 工具并发执行被串行化

`openai_func.py:753-754`：
```python
async def _execute_tool_calls(self, messages, tool_calls, plugin_config):
    for idx, tool_call in enumerate(tool_calls):
        ...
        tool_content, attachments = await execute_tool(...)
```

模型一次返回 3 个 `tavily_search` 时，3 次网络请求串行跑。这是纯粹白给的延迟（3×5s vs 5s）。

### 4.8 其他

- `chat_prompt.py:571-600` 那段 `is_empty_tool_call` 判断 + 尾部 summary 追加是**死代码**：上面 `if item.role == "assistant" and item.tool_calls and not include_tool_history: ... continue` 已经吃掉了所有能到这里的分支（mode 1 走 tool_items，mode 2/3 走 continue）。
- `openai_func.py:701-706` 的双拼函数名修复（`name[:half] == name[half:]`）对合法名有误伤风险。
- `openai_func.py:801` 先 `_normalize_prompt(prompt)`（**原地修改调用方的 prompt_template**）再 deepcopy，副作用外泄。
- `PersistentDataManager.save_to_file` 同步 dump 11MB JSON（`indent=2, sort_keys=True`），错误路径上 `must_save=True` 直接阻塞 event loop。

---

## 五、与主流 Agent 写法的差距（对照）

| 维度 | 主流做法 | 本项目现状 |
|---|---|---|
| prefix 稳定性 | system + tools 全程不变，历史严格 append-only | System 2/3/4 和 tools 逐轮翻转 |
| 易变内容位置 | 放在最末（贴近当前 user turn） | 日期/记忆/摘要放在历史之前 |
| 历史消息 role | 只有 user/assistant/tool | 穿插 6–8 条 system |
| 工具结果 | 原样保留在 tool 消息里，靠窗口/裁剪控量 | 换成 ≤200 字摘要，原始结果丢弃 |
| 工具消息时序 | 严格 assistant(tool_calls) → tool 紧邻 | MODE=1 时整体搬到末尾 |
| 思考链 | 多轮工具调用间保留（含 signature） | 无条件剥离 |
| 工具集 | 少而正交，靠 description 自解释 | 17 个、多组重叠，靠 system prompt 教选型 |
| 行为约束 | 写进 tool description / JSON schema | 写进 system + 输出正则 + 整轮重试 |
| 并发工具 | `asyncio.gather` | for 循环串行 |
| 上下文压缩 | 少而稳，压缩点固定 | 18→6 轮激进压缩，周期性全量失效 |

---

## 六、优化方案

按「改动量 / 收益」排序。P0 三项改完，预期缓存命中率能从"基本不命中"提到 60–80%。

### P0-1 冻结 prefix：System 2 常驻化 + tools 常驻化

**做法**
- 画图知识块和 `generate_anima_image` / `danbooru_search` 的 schema，在 `draw_mode != off` 时**始终注入**，不再看当前消息关键词；
- 需要抑制模型乱画时，改用 tool description 里的触发条件描述（"仅当用户明确要求作画时调用"），而不是靠"不给它这个工具"；
- `auto` 模式的语义从"按关键词给/不给工具"改成"始终给工具 + 提示词约束调用时机"。

**权衡**：System 2 常驻会让每轮固定多 1.6–3.2k token 的**输入**，但这部分从此可缓存（0.1× 计价），而现在它是每轮 1× 全价、还顺带把后面 1 万多 token 一起打成未命中。净收益是大幅正的。

风险：闲聊轮暴露画图工具，可能提高误调用率。用 tool description + `_enable_intercept` 现有拦截兜底；若观察到误调用上升，可考虑保留一个「关键词命中时额外追加一条贴在最末尾的提示」而不是抽掉工具。

### P0-2 prefix 重排：易变内容下沉

**目标顺序**
```
[system] 角色设定 + 响应规则 + 工具规则     ← 完全静态
[system] 画图知识 + extra_prompt            ← 按 profile/draw_mode，改动罕见
[system] 压缩摘要                            ← 会话级
--- 历史消息（append-only）---
[system] 群记忆 + 当前日期 + 记忆提醒        ← 挪到这里：历史之后、触发消息之前
[user]   触发消息
```

把「群记忆 + 日期」从 System 3 挪到历史尾部，`remember` 写入和跨天就只影响最后一小段，不再打穿全部历史。摘要仍然要放前面（它是历史的替代品，语义上必须在前），但它变化频率低于记忆。

**权衡**：记忆放在末尾对模型的"记住这件事"效果实际更好（近因效应），没有质量损失。

### P0-3 context_only 改为 append-only

不再"删除全部旧 context_only 再插入"。改为：
- 每轮的群聊上下文作为一条新的历史消息**追加**在触发 user 消息之前，永不回溯删除；
- 旧的 context_only 随正常的轮次裁剪/摘要一起淘汰（和 impression 现在的处理方式一致）。

**收益**：前缀彻底 append-only，每轮省下 2 条消息的重算；同时群聊话题在多轮之间连续可见，上下文质量提升。

**权衡**：历史 token 增加（每轮多留一段群聊）。用 `CONTEXT_WINDOW_SIZE` 和 token 预算约束即可，且这些增量是可缓存的。

### P1-1 修复 reasoning_content 剥离

- `_completion_kwargs` 改为按 profile 决定是否剥离（新增 `keep_reasoning` 之类的 profile 开关），默认对带 `tool_calls` 的 assistant 消息**保留**；
- 或者至少在**同一个 user turn 的工具循环内**保留（跨轮再剥离）——这是收益最大、风险最小的一刀。

**收益**：多步工具任务的连贯性直接改善。**风险**：部分 provider 不接受该字段，需要按 profile 白名单开启并做 400 兜底。

### P1-2 工具集收敛

- 抓取三合一：保留 `browse_url`（能力最强），`fetch_url`/`tavily_extract` 下线或合并为 `browse_url` 的一个 `mode` 参数；
- 搜索二选一：`tavily_search` 和 `bocha_search` 保留一个作为默认，另一个作为 fallback 在服务端切换，不同时暴露给模型；
- bangumi 5 合 1：`bangumi(action=search_subject|get_subject|search_character|search_person|calendar, ...)`，参照现有 `remember` 的 action 模式；
- 低频工具（`nas_game_list`、`anime_trace`、`pixiv_search`）评估是否值得占常驻工具位。

预期从 17 个降到 8–9 个，schema token 减半（省 ~2k/请求），同时工具选择准确率上升，System 1 里那些"教模型选工具"的句子可以一并删掉。

### P1-3 工具并发执行

`_execute_tool_calls` 改 `asyncio.gather`，注意保持 `messages.append` 的顺序与 `tool_calls` 一致（先 gather 再按序 append）。纯延迟收益，无风险。

### P1-4 工具上下文策略调整

建议从 MODE=3 改为一个新的折中策略：

- 当前 user turn 内：工具结果**原样保留**（现在就是这样，保持）；
- turn 结束落历史时：**保留最近 1–2 组工具调用的完整 assistant(tool_calls)+tool 消息**（用标准 role，不转 system），更早的才降级为摘要；
- 摘要**同步生成**（用现有 fallback 即可，或让 tavily 的 AI answer 直接当摘要），**禁止异步回写历史** —— 消除 §2.4 的缓存抖动。

这样追问"刚才搜到的"至少能覆盖最近一两轮，同时历史里不再有 `[搜索工具摘要]` 这种诱导格式，rules[6] 那条补丁可以删掉。

### P2-1 图片处理

- 优先直接传 URL（provider 支持时），base64 只作为 fallback；
- `_cal_messages_tokens` 的图片估算改为按分辨率/provider 计（至少给个 1000+ 的保守值），让 `CONTEXT_TOKEN_BUDGET` 在带图时不再失真；
- `openai_func.py:801` 的 deepcopy 改为**浅拷贝 + 按需拷贝**（只在真要改的 msg 上 `dict(msg)`），避免复制 base64。

### P2-2 token 计数

- `cal_token_count` 加缓存：按 `id(msg)` 或消息内容 hash 缓存单条 token 数，while 循环里只做增量减法，消除 O(n²)；
- 或者干脆用 `len(text) * 系数` 的快速估算做裁剪决策，只在必要时用 tiktoken 精算；
- 编码器按 profile 走：非 OpenAI 模型用 `o200k_base` 或经验系数会比 cl100k 更接近。

### P2-3 提示词瘦身

- 删掉重复的搜索约束（rules[5] 和 tool_text 第一行二选一）；
- 修 `/no_think` 的模型判断（改成显式 profile 开关，如 `no_think: true`）；
- 把"什么时候调工具"的说明从 system 移到对应工具的 `description` 里；
- 画图的行为约束尽量写进 `generate_anima_image` 的 schema description，减少 system 里的重复。

### P2-4 压缩策略放缓

- `CONTEXT_COMPRESS_THRESHOLD_RATIO` 从 2.0 降到 1.0（缓冲 12 轮，压缩后 6 轮），或者反过来：`CONTEXT_WINDOW_SIZE` 提到 10、ratio 降到 0.5，让压缩触发更少、每次砍掉的更少；
- 目标是**降低"全量失效"事件的频率**。当前 32k 预算下，实测请求才 10–18k，说明还有空间用"多留历史、少压缩"换缓存命中。

### P2-5 后台任务

- 印象生成 for 循环改 `asyncio.gather`；
- `save_to_file` 移到 `asyncio.to_thread`，或改成增量/分片持久化（11MB JSON 全量 dump 已经明显偏重）。

### P3 结构性重构（可选）

- 历史消息里彻底去掉 system 角色：用户印象合并进末尾那条"当前状态" system（和记忆放一起），群聊上下文改成一条 `user` 消息（sender 标成"群聊"），工具摘要并入 assistant 消息的文本里；
- 建立一个"prefix 稳定性"单测：连续构造 N 轮 prompt，断言第 k 轮的前缀是第 k-1 轮的前缀（append-only），CI 里守住这条不变式 —— 这比任何一次性优化都值钱，能防止将来再引入 §2 那类问题。

---

## 七、不建议动的地方

- **按轮次裁剪 + 印象绑定到轮次**（`chat_prompt.py:456-498`、`chat_history.py:110-150`）：设计是对的，印象随轮次一起进出，保证了同一用户印象只出现一次且历史前缀稳定。这块思路已经踩在点上了。
- **工具调用链完整性校验**（`_cleanup_orphan_tool_messages`、`_cleanup_orphan_history_messages`）：必要且正确，多 provider 兼容的刚需。
- **搜索超限提示延后到 tool 响应之后再插入**（`openai_func.py:1238-1244`）：注释里对 assistant(tool_calls) → tool 必须紧邻的理解是准确的，别改。
- **流式 tool_calls 分片重组 + 参数 JSON 完整性校验**：处理得比多数实现细，保留。

---

## 八、预期收益汇总

| 措施 | 缓存命中 | 输入 token | 延迟 | Agent 质量 | 改动量 |
|---|---|---|---|---|---|
| P0-1 冻结 System2/tools | ↑↑↑ | +1.6~3.2k（可缓存） | ↓↓ | ~ | 小 |
| P0-2 易变内容下沉 | ↑↑ | ~ | ↓ | ↑（近因效应） | 小 |
| P0-3 context_only append-only | ↑ | +少量 | ~ | ↑↑（群聊连续） | 小 |
| P1-1 保留 reasoning | ~ | +中等 | ~ | ↑↑↑（多步工具） | 中 |
| P1-2 工具集收敛 | ↑ | −2k | ~ | ↑↑（选型准确率） | 中 |
| P1-3 工具并发 | ~ | ~ | ↓↓↓ | ~ | 小 |
| P1-4 工具上下文折中 | ↑↑ | +中等 | ~ | ↑↑（可追问） | 中 |
| P2-* 各项 | ↑ | −小 | ↓↓ | ↑ | 小~中 |

P0 三项合计改动量很小（都集中在 `chat_prompt.py` 的 messages 组装顺序和 `openai_func.py` 的 tool_schemas 过滤），是最划算的起点。
