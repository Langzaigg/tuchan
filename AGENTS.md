# 项目概述

基于 NoneBot2 / OneBot v11 的 QQ 机器人，核心功能由 `naturel_gpt` 插件提供。所有功能开发、问题修复和配置调整均围绕该插件展开。

## 核心架构决策

- **LLM 后端**：`openai_func.py` 直调 OpenAI-compatible API（多 key 顺延、自定义 base_url、代理、流式）。多组配置存 `OPENAI_PROFILES`，`rg model` 按群切换。**请求级 profile 快照**：matcher / chat_summary 调用前显式传入 `chat.get_active_profile()`，本轮 `model`/`base_url`/`proxy`/`api_key`/`multimodal`/`enable_stream` 固定，避免并发群切换污染（尤其工具后续轮）。
- **多 API Key 策略（第一顺位优先，禁止粘性轮询）**：`_key_plan(scope, keys)` 恒定从**索引 0** 开始选 key，只跳过「冷却中的 key」和「本次请求内已试过的 key」；`_mark_key_failed` 把失败的 key 打进 `_key_cooldowns`（scope → {索引: 截止时间}，`KEY_FAILURE_COOLDOWN_SECONDS=300`），冷却到期自动回到第一顺位。**只有 `_is_key_level_error`（HTTP 401/402/403/429 或鉴权/额度/限流关键词）才允许顺延**——400 参数错误、超时、上下文超限、图片被拒都与 key 无关，换 key 只会把流量白白漂到备用 key。key 级失败时同一请求内顺延重试（`_tried_key_indices` 记录已试索引，全试过才返回失败）；`rg model` 显式切换会清空该 profile 的冷却。历史教训：旧实现是「任何异常都推进索引且成功不回退」的粘性索引，一次偶发失败就把该 profile 永久钉在第二个 key 上。视觉模型 key 用独立 scope（`<profile>#vision`）。
- **工具调用**：原生 OpenAI Tool Calling，定义在 `llm_tool_plugins/`，`llm_tools.py` 聚合调度。旧文本协议、Extension/PresetHub 已移除。
- **先搜后答**：对外部事实不确定时先 `tavily_search`（Tavily 不可用时工具内部直调博查 fallback）再答，禁止凭记忆编造；闲聊/情感/人格自述/上下文已给信息不受限。
- **流式回复**：`LLM_ENABLE_STREAM` 开启后按 `\n\n` 分段发送，受 `REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS` 控制；响应规则的段数上限读同一配置。**分段预算按工具轮放宽**：`_on_tool_call` 时 `sent_segments` 归零并 `_tool_rounds +1`（中间轮文本各算一轮）；收尾残余缓冲经 `_send_buffer_segmented` 按 `\n\n` 拆发，预算 = `REPLY_MAX_SEGMENTS + _tool_rounds - sent_segments`，超出部分合并为最后一段——任何路径都不得把整段回复压成一条长消息。
- **多模态输入 / 图片策略**（`chat_prompt._apply_image_policy`，所有 profile 唯一路径）：OneBot 图片解析为 `image_url`，文本以 `[图片N]` 占位（存储层为消息内本地编号）。
  - **就地保留**：图片留在它被发出的消息里（触发 user / 历史 user / context_only 块），每轮原样重发以命中前缀缓存；不往触发消息搬运，不对触发句做关键词门控。
  - **统一过期**：`MULTIMODAL_IMAGE_FRESH_MINUTES`（默认 60），截止点按 30 分钟量化（`_image_expiry_cutoff`：`floor((now-60min)/30min)*30min`），整点/半点批量退场，过期图渲染为 `[图片已过期]`；触发消息自身图片始终可见。
  - **容量滞后回收**：可见图片超过 `MULTIMODAL_MAX_IMAGES`（默认 8）时按最旧剥离到一半（触发图不参与）。
  - **渲染层全局编号**：可见图片按上下文顺序从 1 连续编号改写各消息文本；同一 URL 复用编号只注入一次。新图只在尾部追加故编号稳定；编号只在有图离开（过期/回收/裁剪，前缀本就已断）时前移。已知代价：过期事件后历史 assistant 文本里的旧编号可能指向另一张图，每半小时至多一次。
  - 可见图片表 `{编号: 原始URL}` 存 `chat._visible_images` → matcher 写入 `tg._visible_images`（ContextVar），vision / anime_trace 按编号取图，查不到返回可用编号列表。profile `multimodal:false` 时请求层剥离 `image_url`，编号表仍有效。支持 `http(s)://`、`data:image/`、`file:///`。
- **图片缓存**（`image_cache.py`）：异步下载 + 内存 LRU（单图 10MB、总 200MB，覆盖 1 小时内图片）。**直传优先**：公开 http(s) URL 原样传 API；provider 拉取失败（图片 400）时 matcher `mark_passthrough_failed()` 后原位转 base64 重试一次，仍失败再走无图重试。QQ 私有域（qpic.cn/qlogo.cn/gtimg.cn/qq.com）与内网地址始终 base64。下载失败返回空串（不回退原 URL），`_known_bad_urls` 跳过；QQ rkey 过期 400 属预期，降级 DEBUG。`resolve_urls_keep_order()` 按位置对齐返回供图片策略逐张判定；vision/anime_trace 用 `force_base64=True`。`get_chat_prompt_template()` 构建后清除不在 `prompt_messages` 中的缓存。
- **视觉工具（纯文本模型看图）**：profile 设 `model_vision`（默认 `deepseek-flash`，仅 `multimodal:false` 生效）时 `get_tool_schemas` 注入 `vision` 工具；`vision(image_index, prompt)` 读 `tg._visible_images` → `resolve_urls(force_base64=True)` → `_request_openai_compatible` 调视觉模型（可选 `model_vision_base_url`/`model_vision_api_keys`/`model_vision_max_tokens` 覆盖）→ 返回文字描述。视觉配置用 ContextVar 快照（`_CURRENT_VISION_CONFIG`/`_VISIBLE_IMAGES`），依赖全局 `MULTIMODAL_ENABLE=true`。
- **Think 处理**：流式回调实时拦截 `<think>` 到 `reasoning_content`，兜底正则二次过滤。**思考泄漏兜底**：profile `thinking: true`（默认）时若无 reasoning 也无 `<think>`，全部缓冲后按 `THINK_LEAK_THRESHOLD`（150）/`THINK_LEAK_SHORT_SEGMENT`（50）由 `_split_think_leak()` 切分：从第一个短段落起视为真实回复，全不短则取最后一段。缓冲期间若收到 `reasoning_content`（常见于工具轮：首轮 tool_calls 不带 reasoning），`on_reasoning_chunk` 立即退出缓冲并把已缓冲文本按正常分段发出。后台任务（摘要/印象）的输出经 `chat_summary._clean_llm_text` 去掉 `<think>` 块与孤立 `</think>` 前的内容，避免思考混入印象再注入 prompt。
- **括号噪音过滤**：实际调过工具的请求中，分段整条被一对括号包围（`_is_bracket_wrapped`）则不发送。
- **非触发消息缓冲**：不回复的群消息由 `matcher._recent_context_buffers` 按 `chat_key` 暂存（窗口 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * ratio)`）。`should_reply` 按本条消息独立计算。下一条触发消息通过节流后 flush 为一条 `context_only` 消息（文本头 `[群聊上下文-非触发消息]`，行格式 `[HH:MM] sender: text`）置于触发消息前。flush 时**时间衰减**：超过 `CONTEXT_BUFFER_MAX_AGE_MINUTES`（15）的条目丢弃，至少留 `CONTEXT_BUFFER_MIN_LINES`（3）条；**图片全部保留**，块内本地编号整体平移，`image_meta`（每张图 `{sender, timestamp}`）随 `update_chat_history_row(image_meta=...)` 写入供按张过期。存储层 `role="system"` + 标志 `context_only`，**渲染为 `user` 角色**（system 不接受 image 部件）；`_is_context_only_message` 按文本头识别，触发消息 = 最后一条非 context_only 的 user（`_find_trigger_msg_idx`）。不计轮数、不持久化、append-only，随所在区间被裁剪/摘要删除。
- **尾部触发标记**：`[当前触发] 回应下面这条来自 {sender} 的消息，它附带了图片/它不带图。其余上下文只作背景（，没被问到的图不用去看）。` 只进本次请求的 messages、不落库（`_on_reply_complete` 只持久化回复文本与工具消息），与 `[记忆提醒]` 同槽位、并存时合并一条。循环邮箱插入新消息前 `openai_func._drop_stale_trigger_marker` 去掉该标记行（保留同条里的其他行），避免"只回应那条"错套到新插入的消息。用户不知道图片编号，只会通过引用回复带图，故 vision / anime_trace 的 description 写成"用于当前消息自带或引用的图，明显在问上下文某张图时也可用，没人问的不主动认"。
- **Reply 上下文注入**：触发消息含 reply 段时，以 `[回复 xxx 的消息]` 前缀拼入被回复文本，被回复图片插到 `image_urls` 前并重编号。
- **自定义昵称**：`rg nn <昵称>`（≤30 字）存 `PersistentDataManager._custom_nicknames`（全局），优先于群名片。
- **个人印象注入（per-turn）**：只注入触发者印象。`update_chat_history_row` 记录触发 user 前，若上下文尚无该 `user_id` 的印象 system（`is_impression`），先 append 印象 system（`[用户印象: 昵称]\n正文\n[你的记忆]...`）再 append user。同一用户整个上下文只注入一次，随绑定轮被删除后下次触发重建；印象 system 不持久化。
- **人格系统**：来源固定 `config/personas/`，运行时热加载（`chat_preset` 5 秒 TTL 缓存）；YAML `PRESETS` 仅运行时容器。profile `extra_prompt` 注入 S1 末尾。
- **Debug / Error 日志**：每次请求保存 `data/naturel_gpt/logs/{chat_key}.latest.json`（`prompt` 为请求前快照；`loop_messages` 为工具循环结束时的完整消息列表，排查邮箱插入/工具轮以它为准；`tool_messages` 仅最终回复段）；失败即写 `{chat_key}.error.json`；摘要任务写 `{chat_key}.summary.json`。写入前经 `sanitize_internal_control_text()`。内部异常文本不得存为 assistant 历史、进 prompt 或发群。
- **循环邮箱（插入式打断）**：同群新触发消息不 cancel 旧任务，而是照常落库后 `push_loop_input` 进 `TextGenerator._loop_mailbox`（锁内，entry 保留 `recorded_msg`/`sender`/`userid`/`image_urls`）。`stream_response` 每个文本轮结束调 `_insert_mailbox_entries`：本轮回复先经 `on_reply_complete` 落库，再把邮箱 entry **合并为一条多行 user 消息**（`_build_loop_batch_message`，`[图片N]` 从 `_next_image_index` 续编并并入 `tg._visible_images`），其前注入 ephemeral system `[新消息提醒] ... 请逐条分别回应：每条各自成段、开头点名对象...`（不落历史），重置轮数/计时继续循环；entry 去重按行比对。`on_reply_complete(text, tool_messages, reply_entries)` 逐回复回调：`reply_entries` 非空时用户维度历史归属到批次内各用户。终端阶段不插入，残留 entry 由 `_finalize_chat_task()` 收尾时逐条重走 `do_msg_response`（复用 `recorded_msg`）。
- **运行统计**（`stats.py`）：按日分桶持久化 `data/naturel_gpt/stats.json`；触发回复、各模型 token（兼容 OpenAI/Anthropic/DeepSeek 缓存字段）、工具调用计数。`rg stat` / `rg stat reset`。

## 关键路径

```
ATRI/plugins/nonebot_plugin_naturel_gpt/
├── chat.py / chat_memory.py / chat_history.py / chat_prompt.py / chat_summary.py   # Chat 类及 Mixin
├── matcher.py              # OneBot 入口
├── config.py               # 配置
├── openai_func.py          # LLM 调用
├── llm_tools.py / llm_tool_plugins/   # 工具
├── image_cache.py          # 图片缓存
├── persistent_data_manager.py         # 持久化
├── stats.py / command_func.py / persona_loader.py / utils.py / chat_manager.py
├── text_to_image.py / draw_db.py / store.py / singleton.py / logger.py
config/naturel_gpt_config.yml          # 主配置
config/personas/                       # 人格（.md 单文件 / skill 文件夹）
data/naturel_gpt/                      # 持久化状态、日志、draw.db（勿直接改）
```

# 模块说明

## `__init__.py`

- 加载配置与持久化状态；初始化 `TextGenerator`；导入 `matcher`。`init_tools(config)` 按 `LLM_DISABLED_TOOLS` 条件注册；`tavily_search` 在 Tavily key 可用或仅有 `BOCHA_API_KEY` 时注册（博查无独立工具）。
- Anima 画图：启动 health check 通过则 `COMFYUI_ENABLED = True`；工作流从 `GET /anima/workflows` 动态拉取。`draw_db.init_db()`。

## `config.py`

- `GlobalConfig` / `Config` / `PresetConfig`；缺失键由 `CONFIG_TEMPLATE` 补齐并回写；`reload_config()` 整体替换对象。
- `OPENAI_PROFILES`（键=profile 名，值=模型配置 dict）+ 默认指针（见下）；每群独立 `active_profile`。`DEFAULT_PERSONA`；人格目录 = 配置旁 `personas/`。
- **默认模型配置用指针，不再有名叫 `default` 的独立配置**：`OPENAI_PROFILES.default` 的值是**字符串**（真实 profile 名，如 `default: ds`），不是模型配置。解析顺序 `resolve_profile_name()`：会话自选 → `default` 指针 → 旧字段 `OPENAI_ACTIVE_PROFILE`（仅兼容回落）→ 第一个真实 profile；指针失效/名字被删都会回落。配套 **Config 的方法**（⚠️ 必须是方法：各模块里 `config` 拿到的是 Config **实例**（`from .config import config`），写成模块级函数调用会 `AttributeError: 'Config' object has no attribute ...`）：`get_profile_names()`（只列真实 profile，供 `rg model` 展示与校验）、`get_default_profile_name()`、`get_profile(name)`（恒返回 dict，自动解指针，**所有读取 profile 的地方都用它，不要再直接 `.get` OPENAI_PROFILES**）、`DEFAULT_PROFILE_KEY`（ClassVar，值 `"default"`）。**全局默认只由配置文件的指针决定**：`rg model` 只写本会话 `active_profile`，`Chat.apply_profile()` 只跟随，**两者都不改写指针**（没有任何代码路径会改它，改默认就编辑 yml + `rg reload_config`）。旧配置里 `default` 仍是 dict 时按真实 profile 处理（向后兼容），旧扁平键迁移为 `main` + `default: main`。

## `persona_loader.py`

- 简单 `.md`（整文件为 prompt）与 Skill 文件夹（含 `SKILL.md`，名=文件夹名首个 `-` 前）共存。Skill 注入顺序：`SKILL.md` → `soul.md` → `limit.md` → `resource/behavior_guide.md` → `key_life_events.md` → `relationship_dynamics.md` → `speech_patterns.md`。

## `openai_func.py`

- `TextGenerator`（Singleton）。`stream_response()` 返回 `(text, success, tool_messages, reasoning_content)`；入口对 prompt 逐 dict 浅拷贝，循环内改动均为 dict 级替换，调用方 prompt 保持请求前快照。工具附件按 `chat_key` 分桶（`consume_tool_outputs(chat_key)`）。
- **Content 兜底**：`content: None` → `""`；空 assistant content 填 `"[无内容]"`（防 Moonshot 400）；assistant 消息保留 `reasoning_content`（历史中的按 profile `keep_reasoning` 剥离）。
- **工具轮**：中间轮文本经 `on_text` 实时输出；`LLM_MAX_TOOL_ROUNDS` 不计 remember-only 轮；总次数 `LLM_MAX_TOTAL_TOOL_CALLS`（默认 15）、搜索 `MAX_SEARCH_TOOL_CALLS=3`。控制文本由 `sanitize_internal_control_text()` 清洗后才发送/持久化。**搜索超限提示必须在 tool 响应全部 append 之后插入**（assistant(tool_calls) 与 tool 响应之间不得插任何消息，否则上游 400）。
- **终端轮（双闸门）**：`TERMINAL_TOOLS = {"generate_anima_image", "remember"}`。轮数 ≥ `LLM_MAX_TOOL_ROUNDS`、总次数超限、工具 token 超 `TOOL_CONTEXT_TOKEN_BUDGET`、耗时 > `LLM_TOOL_LOOP_MAX_SECONDS`（180）任一进终端阶段：tools 数组保持全量（缓存前缀不变）、非终端调用执行侧过滤、只允许单次终端调用，之后无工具收尾轮（`tool_choice="none"`）出最终文本。所有终止路径返回已累积文本。
- **强制画图**：`draw_mode == "force"` 且含画图关键词时尾部注入 `_FORCE_DRAW_HINT_TEXT`（不用 tool_choice 指定函数，思考模式 provider 不兼容）；`tool_choice="none"` 被拒时省略字段重试一次。
- **Console Go 会话头**：base_url host 含 `opencode.ai` 时自动注入 `x-opencode-session`（`atri-<md5(chat_key)[:24]>`，由 `_CURRENT_CHAT_KEY` 派生，同群稳定；无 chat_key 用进程级 uuid 兜底）与自定义 User-Agent（opencode Go 强制要求，见 `_build_provider_headers`，非流式/流式两处请求头均应用）；其他 provider 不加额外头。
- 流式工具名双拼修复；`arguments` JSON 校验（全丢重试 ≤1 次，再撤工具强制出文本）；`stream_options.include_usage` 采集缓存命中到 `_last_stream_usage`；循环异常始终 return 由 matcher 重试；httpx read 超时每 chunk 重置、总上限 5 分钟；`type='summarize'/'impression'` 用 `model_mini`（空回退 `model`）；公开方法不用可变默认参数。

## `llm_tools.py` / `llm_tool_plugins/`

每个工具一个文件：schema + `run(args, config)`。`get_tool_schemas()` 按 `chat_key` 注入画图 schema（函数名统一 `generate_anima_image`）。

- **`tavily_search.py`**：唯一搜索 schema。多 key 选额度最多；`advanced`、`max_results=20`、单条 300 字、总长受 `WEB_FETCH_MAX_CHARS`。失败标记 `_tavily_disabled` 并直调博查 fallback（`bocha_search.py` 仅内部 helper）。
- **`browse_url.py`**：短链还原 → 社交平台 SSR → Playwright → trafilatura → Tavily Extract。
- **`pixiv_search.py`**、**`bangumi_search.py`**（`bangumi(action=...)` 单 schema）、**`danbooru_search.py`**（随画图工具暴露，`_DRAW_ONLY_TOOLS`）、**`nas_game_list.py`**（`NAS_GAME_WHITELIST_GROUPS` 白名单，路径全走配置）。
- **`anime_trace.py`**：AnimeTrace 以图识角色。`anime_trace(image_index)` 从 `tg._visible_images` 按显示编号取图 → `resolve_urls(force_base64=True)` → POST `/v1/search`（`is_multi=1`、`ai_detect=1`）；模型经 `/v1/model/list` 动态选取（缓存 1 小时）。所有 profile 暴露。
- **`vision.py`**：见核心架构决策「视觉工具」。
- **`memory.py`**：长期事实记忆（只记客观长期事实；性格由印象、话题由摘要负责）。scope `group`/`user`；action `save`/`delete`/`consolidate`（批量）。接近上限 80% 注入整理提醒不阻断。
- **`anima_generate.py`**：ComfyUI 画图。
  - `rg draw [force/on/auto/off]`，持久化 `ChatData.draw_mode`；工作流动态发现（`/anima/workflows`，`deprecated` 过滤，不可达降级 `fuse`）；`select_default_workflow()` 首选 `fuse` → 含 "turbo" → API `default` → 兜底；`LEGACY_MODEL_MAP` 处理旧名。
  - knowledge 由 `_build_workflow_knowledge()` 统一压缩（≤ 2k token）拼入 schema `function.description`（`_schema_cache`）；行为短规则 `get_draw_s1_rules` 常驻 S1。
  - `run()` `POST /anima/generate`（body 带 `workflow`），后台任务，结果入 `_pending_results` 由 matcher 发图；成功返回编号 `draw-XXXXXX` + 预计秒数，提示词存 `draw.db`。队列 >5 拒绝；参数全空拒绝。
  - **漫画模式**（`rg manga on/off/[画风]`）：动态默认工作流，无编号、不存 DB；`MANGA_RULES`/解锁时 `MANGA_UNLOCK_RULES` 追加到漫画 schema description；空闲检测 `should_inject_manga_idle()`（`MANGA_IDLE_MINUTES`/`MANGA_IDLE_ROUNDS`），`manga_idle_draw()` 用 mini 模型按上下文设计场景（`pending_request` 非空时严格按该请求画）。

## `chat.py` 及 Mixin

- **`chat_history.py`**：`update_chat_history_row`（触发 user 写 `user_id`；context_only 传 `image_meta`）、`save_tool_messages`（修双拼工具名、剔除未注册工具、deep copy 后规范化，返回 assistant tool-call 消息供摘要绑定）、`remove_last_prompt_user_message`、`cleanup_after_bad_request`、`_trim_prompt_messages_without_summary`、`_cleanup_orphan_*`、`update_chat_history_row_for_user`。
- **`chat_prompt.py`**：`get_chat_prompt_template`、`_build_openai_history_messages`、`_trim_messages_to_request_budget`、`_apply_image_policy`、`_image_expiry_cutoff`、`_is_context_only_message`、`_find_trigger_msg_idx`、`_last_trigger_sender`。历史 assistant 中的任务编号替换为占位符 `[请调用 generate_anima_image 画图工具获取编号]`（常量勿改）。`_apply_image_policy` 在 `item_to_msg_idx` 构建后回写 normal_messages，之后不得再插入/删除消息。
- **`chat_summary.py`**：`generate_tool_call_summary`、`_compress_prompt_messages_if_needed`、日志保存。
- **prompt 结构**：`[S1 角色+响应规则+工具规则+画图短规则][S2 extra_prompt?][S3 压缩摘要?][S4 当前状态：群记忆+日期][历史轮: system印象? user assistant system工具摘要? user context_only?]...[system 触发者印象?][user context_only（本轮 flush）?][system [当前触发](+记忆提醒?)][user 触发句]`。S4 放头部（低频变化，保住整段历史的前缀缓存）；`[记忆提醒]` 与 `[当前触发]` 在尾部（每轮必变段）。
- **响应规则要点**：段数上限读 `REPLY_MAX_SEGMENTS`；`[群聊上下文-非触发消息]` 块是背景，未被点名不主动回应或点评其中图片；只回应当前触发消息与其发送者，不合并话题；不用 Markdown（`ENABLE_MSG_TO_IMG` 时允许）。
- **上下文窗口**：轮数只计非 context_only 的 user。`CONTEXT_WINDOW_SIZE` 为摘要后目标窗口，请求侧缓冲窗口 = 窗口 + 窗口×`CONTEXT_COMPRESS_THRESHOLD_RATIO`；裁剪/摘要按完整旧轮删（含前导印象与该轮 context_only）。
- **孤立历史清理**：`_cleanup_orphan_history_messages()` 按**计数**状态机：非 context_only user `open_users +1`，普通 assistant `-1`，计数为 0 时的 assistant 丢弃；tool_calls 链保持开轮至最终 assistant。用计数而非布尔：循环邮箱会产生 `user(A) user(B) assistant(答A) assistant(答B)`，布尔"首条 assistant 关轮"会丢答 B（持久化 `_serializable`/`_load_from_dict` 同样计数）。裁剪、摘要、400 清理、prompt 构造前、持久化都用同一逻辑。
- **Token 截断**：`_cal_text_tokens` 模块级 LRU；单图估算 `IMAGE_TOKEN_ESTIMATE = 1000`。预算裁剪按最旧完整轮删，保护头部 system 与触发消息。
- **摘要压缩**：异步，只在 bot 回复后 `require_summary=True` 路径触发；溢出 > 窗口×ratio 才启动；先生成成功再按 `id()` 删旧段（context_only 不豁免，印象随绑定轮删）；失败 120 秒冷却；任务必须有 `finally`/`done_callback` 清 `_compressing_overflow_item_ids` 并恢复 pending。软限 `max_summary_tokens` 或 `CONTEXT_SUMMARY_TARGET_CHARS`（800），硬截 2 倍；输出 `[当前话题]`/`[群历史]` 两节。
- **用户印象**：依据本次溢出中该用户的 user 消息生成，多用户 `asyncio.gather` 并发；软限 `IMPRESSION_TARGET_CHARS`（200）。
- **记忆**：超限只警告，由 LLM `consolidate` 整理；群记忆提醒在尾部，用户记忆提醒在印象 system。`rg reset` 不清记忆/印象。
- **工具调用摘要（模式 3，唯一路径）**：搜索类（`tavily_search`/`browse_url`）同步一次成稿为 `[搜索工具摘要]`；其他工具截断原文 `[调用结果]`；`generate_anima_image` 结果不进历史，改为 `[作画记录]`（tags/nltags 直出，并声明新作画请求必须重新调用工具）。摘要绑定 `save_tool_messages()` 返回的 `target_msg`。历史注入：tool 原文不进 prompt，assistant(tool_calls) 有摘要→摘要 system 替代，无摘要→跳过。
- 实例变量（`_compress_task`、`_pending_overflow_*`、`_compressing_overflow_item_ids`、`_compress_failure_time`、`_persona_cache*`）不得共享类状态。

## `matcher.py`

- `utils.gen_chat_payload()` 提取文本/图片 URL；允许纯图片消息。`do_msg_response()` 锁内独立算 `should_reply`：False 写 `_recent_context_buffers` 即返回；True 在节流后 flush 缓冲、构建 prompt、写 `tg._visible_images`、调 `stream_response()`。
- **`_on_reply_complete`**：每个完成回复恰好一次：流式收尾、发工具图、`save_tool_messages` + 同步工具摘要、落库群历史与用户维度历史（`reply_entries` 非空时归属批次内各用户）、记 `回复完成 | prompt=X cached=X(xx%) ...` 日志、漫画兜底检查（`_maybe_manga_autodraw` 按 `reply_entries` 锚定，尾部仅补查"无回复完成"或"最近检查后又有工具调用"）。
- **重试链**：图片 400 → 直传转 base64 重试一次 → 清历史图片无图重试（≤2 次）；空 content 400 填 `[无内容]` 重试；空响应清历史至 5 条重试一次；content 混入工具 XML 时注入提示重试。
- **Reply + At 修复**：OneBot 删 reply 段后 at 段丢失 → 检查 `original_message` 补 `to_me`。
- **唤醒**：前缀唤醒词/角色名句首触发；名称提及走 `REPLY_ON_NAME_MENTION_PROBABILITY`；句中/句尾走 `RANDOM_CHAT_PROBABILITY`。
- `_chat_response_lock` 保护 `_chat_running_tasks`/`_chat_active_inputs`；`loop_data` 默认 `None`。

## `persistent_data_manager.py`

- `ChatMessageData`：`user`/`assistant`/`tool`/`system`；`context_only`、`is_impression` 加载时强制 `role="system"` 且不持久化；`image_meta` 与 `images` 平行（仅 context_only 用）。
- `PresetData`（记忆、印象、`prompt_messages`、摘要）、`ChatData`（`active_profile`、`draw_mode`、`draw_model`、`manga_mode`、`manga_style`、`unlock_content_limit`）、`_custom_nicknames`。
- 默认 `data/naturel_gpt/naturel_gpt.json`；`save_to_file()` 60 秒节流（`must_save=True` 跳过），事件循环运行中卸载到执行器线程（`_SAVE_LOCK` 串行），原子写 `.tmp` → `os.replace`；`save_to_file_blocking()` 仅 shutdown 用。加载/保存过滤孤儿 assistant/tool（计数版）、内部异常文本。

## `command_func.py`

- `rg` / `rg list` / `rg set <persona>`；`rg draw [force/on/auto/off | <model> | <json> | -XXXXXX]`；`rg turbo`（兼容别名）；`rg manga [on|off|画风|clr]`；`rg nolimit [on|off]`；`rg model [profile]`；`rg nn [昵称]`；`rg mem` / `rg mem clear <group|user|all>`；`rg help`；`rg stat` / `rg stat reset`；`rg reset`（只清上下文）。
- `cmd.register()` 用 `params: Optional[list] = None`；`execute()` 返回 `{'error': str(e)}`；`rg draw-XXXXXX` 返回 `no_img: True` 强制纯文本。

## 其他

- **`draw_db.py`**：SQLite `data/naturel_gpt/draw.db`，表 `draw_prompts(task_id PK, prompt_data, created_at, updated_at)`，`threading.Lock`。
- **`utils.py`**：`gen_chat_payload()`、`_extract_message_text_and_images()`（`[图片N]`）、`async_fetch()`、用户名解析容忍 API 失败。
- **`singleton.py`**：双重检查锁；**`chat_manager.py`**：全局会话管理器；**`text_to_image.py`**：依赖 `nonebot_plugin_htmlrender`，导入失败自动关。

# Prompt 与回复规范

- 像真实群聊成员自然简短回复，段数不超过 `REPLY_MAX_SEGMENTS`；正常文本不用 Markdown。分段用 `\n\n`。
- 工具调用过程不出现在最终回复；`[调用结果]`/`[搜索工具摘要]`/`[作画记录]` 只是上下文参考，禁止模仿其格式。

# 配置字段速查

## LLM

- `OPENAI_PROFILES`：`default`（指针，值为 profile 名）/`api_keys`/`base_url`/`proxy`/`timeout`/`model`/`model_mini`/`temperature`/`top_p`/`max_tokens`/`max_summary_tokens`/`frequency_penalty`/`presence_penalty`/`multimodal`/`model_vision`/`extra_prompt`/`thinking`/`no_think`（true 时注入 `/no_think`）/`keep_reasoning`（默认 false，历史 reasoning 剥离；true 时 provider 拒绝自动剥离重试）/`reasoning_effort`（仅主对话透传）
  - 当前默认：`OPENAI_PROFILES.default: ds` → `deepseek-flash`（多模态，主模型与 `model_mini` 同款；旧 `mimo-v2.5` 已下线替换）。`api_keys` 第一顺位即为首选 key（见「多 API Key 策略」）。
- `OPENAI_ACTIVE_PROFILE`（旧字段，兼容回落）、`LLM_ENABLE_STREAM`、`LLM_SHOW_REASONING`、`LLM_ENABLE_TOOLS`、`LLM_DISABLED_TOOLS`、`LLM_MAX_TOOL_ROUNDS`、`LLM_MAX_TOTAL_TOOL_CALLS`、`LLM_TOOL_LOOP_MAX_SECONDS`
- 旧扁平字段（`OPENAI_API_KEYS`、`CHAT_MODEL` 等）有 `OPENAI_PROFILES` 时可省略。代码常量：`MAX_SEARCH_TOOL_CALLS=3`、`IMAGE_TOKEN_ESTIMATE=1000`。

## 上下文

- `CONTEXT_TOKEN_BUDGET`（默认 4096）、`CONTEXT_WINDOW_SIZE`、`CONTEXT_COMPRESS_THRESHOLD_RATIO`（默认 0.5，同时决定缓冲窗口）、`CONTEXT_SUMMARY_ENABLED`、`CONTEXT_SUMMARY_TARGET_CHARS`（800）、`IMPRESSION_TARGET_CHARS`（200）、`TOOL_CONTEXT_TOKEN_BUDGET`（16384）
- `CONTEXT_BUFFER_MAX_AGE_MINUTES`（15）、`CONTEXT_BUFFER_MIN_LINES`（3）：非触发缓冲时间衰减；`CONTEXT_BUFFER_SIZE` 仅兼容

## 多模态 / 分段

- `MULTIMODAL_ENABLE`、`MULTIMODAL_MAX_IMAGES`（8）、`MULTIMODAL_IMAGE_FRESH_MINUTES`（60，30 分钟量化）、profile 级 `multimodal`
- `NG_ENABLE_MSG_SPLIT`、`REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS`、`THINK_LEAK_THRESHOLD`、`THINK_LEAK_SHORT_SEGMENT`

## 工具 / 人格

- `TAVILY_API_KEY`（多 key）、`BOCHA_API_KEY`、`BOCHA_API_BASE`、`BOCHA_SEARCH_COUNT`（10-20）；`COMFYUI_BASE_URL`、`MANGA_IDLE_MINUTES`、`MANGA_IDLE_ROUNDS`；`WEB_FETCH_TIMEOUT`、`WEB_FETCH_MAX_CHARS`、`PLAYWRIGHT_TIMEOUT`；`NAS_GAME_*`；`UNLOCK_CONTENT_LIMIT`（每群 `rg nolimit` 覆盖）
- `DEFAULT_PERSONA`；人格目录 = 配置文件旁 `personas/`

# 开发规范

- 改动范围默认限于 `ATRI/plugins/nonebot_plugin_naturel_gpt/` 与显式配置/人格。
- 改 matcher 前梳理消息流与 `rg` 指令流；改 prompt 前读 `get_chat_prompt_template()`；改持久化前读 `persistent_data_manager.py`，保持 JSON 兼容。
- 改配置字段同步：`Config`、`CONFIG_TEMPLATE`、迁移/默认值、`README.md`、本文件。
- 新增消息处理同步 `utils.gen_chat_payload()` 与 `matcher.do_msg_response()`；新增指令用 `cmd.register(...)` 保留权限检查；新增工具加文件于 `llm_tool_plugins/`。
- 守住缓存不变式：第 k 轮请求前缀是第 k-1 轮的严格前缀（尾部触发标记/记忆提醒除外）；历史轮写入后不得改写。
- 非必要不执行访问外部 API 的命令。

# 验证清单

- 语法检查所有变更 Python 文件；大范围变更编译核心文件与 `llm_tool_plugins/*.py`。
- Matcher 变更覆盖群聊/私聊/纯图片消息、`rg` 指令、忽略前缀、禁用户/禁群、`at` 与 `at all`。
- 人格变更验证 `.md` 与 skill 文件夹共存；配置变更验证不丢未知字段、正确补默认值。
- 上下文/图片变更：用 `{chat_key}.latest.json` 的 `loop_messages` 回放，检查触发消息只带自身图片、context_only 块图片就地编号、尾部有 `[当前触发]`、`cached_tokens` 比例不降。
