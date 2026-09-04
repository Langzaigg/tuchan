# 项目概述

基于 NoneBot2 / OneBot v11 的 QQ 机器人，核心功能由 `naturel_gpt` 插件提供。所有功能开发、问题修复和配置调整均围绕该插件展开。

## 核心架构决策

- **LLM 后端**：`openai_func.py` 直接调用 OpenAI-compatible API（多 key 轮询、自定义 base_url、代理、流式输出）。多组配置存于 `OPENAI_PROFILES`，`rg model` 指令按群运行时切换。
- **工具调用**：原生 OpenAI Tool Calling（旧 `/#tool&args#/`、`/#...#/` 文本协议与 Extension/PresetHub 已移除）。工具定义在 `llm_tool_plugins/`，由 `llm_tools.py` 聚合调度。
- **先搜后答**：`chat_prompt.py` 注入约束——对外部事实（人物/作品/日期/数据/新闻等）不确定时先调 `tavily_search` 核实再答（Tavily 不可用时工具内部直调博查 fallback，对模型透明），禁止凭记忆编造；闲聊/情感/人格自我描述/上下文已给信息不受限。
- **流式回复**：`LLM_ENABLE_STREAM` 开启后按双换行 `\n\n` 分段发送，受 `REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS` 控制。
- **多模态输入**：OneBot 图片解析为 `image_url`，文本中以 `[图片N]` 占位标记。**图片门控**：含图 user 消息（触发+历史）均注入图片，不限时效窗口；context_only 图片仅当触发句含图片关键词（图/画/上/这等）时注入触发消息。含图消息数超 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 时清空所有非触发消息的图片，仅保留触发消息的（避免缓存不命中）。每个 profile 有独立 `multimodal` 开关，关闭时请求层剥离 `image_url`。支持 `http(s)://`、`data:image/`、`file:///` 协议。
- **图片缓存**：`image_cache.py` 异步下载 + 内存 LRU。**直传优先**：公开可访问的 http(s) URL（非 QQ 私有域、非内网/环回地址）原样传给 API，不下载不缓存，省去每轮 base64 重传；provider 拉取失败（图片 400）时 matcher 调 `mark_passthrough_failed()` 标记并原位转 base64 重试一次（`_passthrough_bad` 记忆，仍失败再走无图剥离重试）。QQ 私有域（qpic.cn/qlogo.cn/gtimg.cn/qq.com，rkey 时效/Referer 限制）与内网地址始终转 `data:image/...;base64`。单图 10MB、总缓存 50MB。`get_chat_prompt_template()` 构建后清除不在 `prompt_messages` 中的缓存。下载带 `User-Agent` 和 QQ 域名 `Referer`；失败返回空字符串（不回退原 URL，避免 400），已知失败 URL（`_known_bad_urls`）跳过。QQ `rkey` 过期 400 属预期，降级 DEBUG。vision/anime_trace 等无 400 回退链的调用方用 `resolve_urls(..., force_base64=True)` 强制 base64。
- **视觉工具（纯文本模型看图）**：profile 可设 `model_vision`（默认 `"mimo"`，仅 `multimodal:false` 时生效）。纯文本主模型收到图片时，请求层剥离 `image_url`（文本 `[图片N]` 保留），`get_tool_schemas` 按本群 profile 门控注入 `vision` 工具；主模型调 `vision(image_index, prompt)` → 工具读 `tg._current_trigger_images`（**整个对话上下文图片的全局列表**）→ `image_cache.resolve_urls(..., force_base64=True)` 转 data URI（按需现下载，缓存命中即加速，无需预热；强制 base64 因视觉调用链无 400 回退）→ `_request_openai_compatible` 调视觉模型（复用 profile 的 `base_url`/`api_keys`，可选 `model_vision_base_url`/`model_vision_api_keys`/`model_vision_max_tokens` 覆盖）→ 返回纯文本描述给主模型。**全上下文 + 全局重编号**：视觉 profile 下 `_build_openai_history_messages` 跳过 `_apply_image_gating`（图片本就被剥离、门控的多模态注入无意义且会产生无占位符孤儿图），改走 `_apply_vision_image_context`：遍历 normal_items（user + context_only），按出现顺序把所有图片收成全局列表，`[图片N]` 重编号为全局唯一 1..N（无占位符的图追加 `[图片N]` 标记），结果存 `self._vision_context_images`；matcher 优先把它写入 `tg._current_trigger_images`（覆盖触发消息本身的 image_urls），故 `image_index` 能跨消息无歧义访问历史/context_only/触发消息任意一张图。视觉配置用 ContextVar 快照（`_CURRENT_VISION_CONFIG`/`_CURRENT_TRIGGER_IMAGES`，仿 `_current_chat_key`），`stream_response` 开始时按 `request_profile` 写视觉配置。原生多模态 profile（`multimodal:true`）忽略 `model_vision`、不暴露 vision 工具、走原 `_apply_image_gating`。注意：视觉流程依赖全局 `MULTIMODAL_ENABLE=true`（否则图片 URL 在 `gen_chat_payload` 阶段就被丢弃，全局列表为空）。
- **Think 标签过滤**：流式回调实时拦截 `<think>...</think>` 提取到 `reasoning_content`；兜底正则在最终响应二次过滤。
- **思考泄漏兜底**：部分模型思考直接混在 `content` 里（无 `reasoning_content` 字段、无 `<think>` 标签）。profile 可设 `thinking: bool`（默认 true）：思考模式下若无 reasoning 也无 `<think>` 就收到 content，置位 `_skip_think_buffer_mode`，停止分段提前发送、全部缓冲；流结束后 `content` 超 `THINK_LEAK_THRESHOLD`（默认 150）且含双换行，由 `_split_think_leak()`（matcher.py）按 `\n\n` 全部分段，从第一个长度 < `THINK_LEAK_SHORT_SEGMENT`（默认 50）的段落开始（含）视为实际回复——思考是连续长段落，真实回复常以短句/口头语开头，避免回复开头短句被误判为思考丢掉；若所有段落都不短，回退取最后一段。前段存入 `reasoning_content`（仅 debug 日志）。失败路径同样兜底 `raw_res_for_save`。`thinking: false` 走原逻辑。
- **工具调用括号噪音过滤**：模型在工具调用场景常输出 `（图片已发送）` 等元描述。`stream_response` 的 `on_tool_call` 回调使 matcher 置位 `_tool_called`；`send_segment` 中若 `_tool_called` 且分段 strip 后整条被一对括号包围（`_is_bracket_wrapped`，全半角多种括号、仅单层包裹、内层无括号字符）则跳过不发送。仅实际调过工具的请求生效，纯闲聊动作标记（如（摸头））不受影响。
- **非触发消息缓冲**：不回复的群消息由 `matcher.py` 的 `_recent_context_buffers` 按 `chat_key` 暂存（独立窗口 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)`，不依赖 `CONTEXT_BUFFER_SIZE`）。`should_reply` 必须基于本条新消息独立计算，非触发消息不能继承旧触发状态。下一条触发消息通过节流后、生成 prompt 前 flush，作为 `context_only` system 消息（前缀 `[群聊上下文-非触发消息]`）注入 `prompt_messages`，置于触发消息之前。`context_only` 用 `role="system"`，不计轮数、不持久化；**append-only**：每轮 flush 追加一条、不再清旧，作为普通历史条目随所在区间被窗口裁剪/摘要删除一并淘汰（当轮新 flush 的 context_only 位于最新触发轮之前，随该轮存续）。旧兼容路径 `Chat._context_buffer`（`push_context_buffer`/`flush_context_buffer`）已删除，只留 `_recent_context_buffers` 一条链路。
- **Reply 上下文注入**：触发消息含 reply 段时，从 `event.reply.message` 提取被回复消息文本和图片，以 `[回复 xxx 的消息]` 前缀拼到触发消息前，图片插入 `image_urls` 前面并重新编号 `[图片N]`。
- **自定义昵称**：`rg nn <昵称>` 设置（≤30 字符），存 `PersistentDataManager._custom_nicknames`（全局跨群），优先于 API 群名片；`rg nn` 查询，`rg nn 清除` 删除。
- **个人印象注入（per-turn）**：只注入触发者印象。`update_chat_history_row` 在记录触发 user 前，若上下文中尚无该 `user_id` 的印象 system（`is_impression=True`，按 `impression_user_id` 去重）且有印象或用户记忆，先 append 印象 system（印象正文+用户记忆，`_message_text_for_prompt` 格式化为 `[用户印象: 昵称]\n正文\n[你的记忆]\n1. key: value`），再 append user。用户记忆与印象绑定在同一 system，不再单独注入 System 3。同一用户印象整个上下文只注入一次，绑定首次触发轮；摘要/裁剪删除该轮时连同前导印象删除，下次触发重新注入最新印象——历史轮写入后固定不变，保证多人使用时前缀稳定、prompt 缓存可命中。印象 system 不持久化（`_serializable` 过滤 `role=system`），重启后由下次触发重建。
- **人格系统**：运行时动态加载，来源固定 `config/personas/`。YAML 的 `PRESETS` 仅作运行时容器，不作人工编辑源。
- **Debug 日志**：每次 LLM 请求完成（成功或失败）保存请求/响应到 `data/naturel_gpt/logs/{chat_key}.latest.json`（base64 替换占位符，reasoning 完整不截断）；摘要任务存 `{chat_key}.summary.json`（含上下文摘要、工具摘要、用户印象）。`latest.json.prompt` 必须是请求发出前的快照——`stream_response()` 入口对 normalize 后的消息做逐 dict 浅拷贝（循环内所有改动均为 dict 级替换，不污染调用方 prompt），避免工具调用追加的消息污染。`latest.json.loop_messages` 是工具循环结束时的最终完整消息列表（`TextGenerator._last_loop_messages_by_chat` 驻留循环内原地修改的 messages 引用，含循环邮箱插入的新触发消息、assistant tool_calls、tool 响应），排查插入式打断/工具轮问题以它为准；`tool_messages` 仅为最终回复段的工具消息（邮箱插入后会清零重计，为空则不写入）。`response`/`intermediate_responses`/`tool_messages` 写入前经 `sanitize_internal_control_text()` 清理。`请求大模型时发生错误: ...` 等内部异常可留在 debug 日志，但不得存为 assistant 历史、不得进 prompt、不得发群。
- **Error 日志**：`stream_response` 返回 `success=False` 时立即保存 prompt 到 `{chat_key}.error.json`（每群仅最新一份，重试前保存，即使重试成功也记录），经 `_sanitize_prompt_for_log` 处理。摘要/印象任务失败同样写入（`source: "summary_task"`）。
- **请求打断改为插入式 agent 循环（循环邮箱）**：同群新消息打断旧请求时**不再 cancel** 旧任务，而是把新触发消息写入 `TextGenerator._loop_mailbox`（按 `chat_key` 分桶的 deque，`push_loop_input`/`drain_loop_inputs`，entry 保留 `recorded_msg`/`sender`/`userid`/`image_urls` 等元数据，推送时记 INFO 日志）。触发 user 消息推入邮箱前已由 matcher 照常落库（保证格式/时间戳/user_id，均在锁内完成，与旧任务收尾的 drain 互斥，保证 entry 必定被消费。`stream_response` 循环每轮结束（无 tool_calls 的文本轮）调 `_insert_mailbox_entries`：本轮已产出回复先经 `on_reply_complete` 回调由 matcher 逐轮落库，再把邮箱内所有 entry **一次性批量插入** messages（多条 entry 各自成 user 消息，首条前注入 ephemeral system 未处理标记（`[新消息提醒] 以下 N 条是尚未处理的新消息...`/含被打断回复未完成提示，ephemeral 不落历史），重置 `round_idx`/`loop_start` 后继续循环。已产出回复文本逐轮经 `on_reply_complete` 落库且全保留（`_all_reply_texts` 合并返回）；`on_reply_complete` 第三参数 `reply_entries` 携带本回复应答的邮箱插入批次（None=应答原始触发消息），供 matcher 按批次做 per-reply 判定（如漫画兜底画图锚定）。**终端阶段不插入**：残留 entry 由 matcher `_finalize_chat_task()` 在任务收尾时 drain 后逐条重走 `do_msg_response`（经 `recorded_msg` 复用已落库的 user 消息，不重复写入）。旧 `_last_interrupted_response`/`_pending_merge_input`/`set_interrupted_response`/`pop_interrupted_response` 已删除。
- **模型专用提示词**：profile 可设 `extra_prompt`，注入 system1 末尾，用于模型行为调优。
- **人格热加载缓存**：`chat_preset` 属性 5 秒 TTL 缓存（`_persona_cache`/`_persona_cache_time` 实例变量），避免频繁磁盘 I/O。
- **运行统计（stats.py）**：`StatsManager` 单例按日分桶持久化到 `data/naturel_gpt/stats.json`。采集点：触发回复（matcher `stats.inc_trigger()`）、各模型 token（`openai_func.py` 每次 API 请求后 `stats.record_model_usage()`，兼容 OpenAI/Anthropic/DeepSeek 三种缓存字段）、工具调用（`_execute_tool_calls` 每个工具执行前 `stats.inc_tool_call(name)`，成败都计）。`rg stat` 查看当日，`rg stat reset` 清空。模型名取 profile 的 `model` 字段。

## 关键路径

```
ATRI/plugins/nonebot_plugin_naturel_gpt/
├── chat.py                 # 核心会话类（属性、基本操作）
├── chat_memory.py          # 记忆管理 Mixin
├── chat_history.py         # 对话历史管理 Mixin
├── chat_prompt.py          # Prompt 构造 Mixin
├── chat_summary.py         # 摘要/印象生成 Mixin
├── matcher.py              # 主 OneBot 集成入口
├── config.py               # 配置管理
├── openai_func.py          # LLM 调用
├── llm_tools.py            # 工具管理
├── llm_tool_plugins/       # 工具插件目录
├── image_cache.py          # 图片下载缓存（URL→base64 data URI）
├── persistent_data_manager.py  # 持久化数据管理
├── stats.py                # 运行统计（按日分桶）
├── command_func.py         # 命令管理
├── persona_loader.py       # 人格加载
├── utils.py                # 工具函数
├── chat_manager.py         # 会话管理器
├── text_to_image.py        # 文本转图片
├── draw_db.py              # 绘图提示词数据库（SQLite）
├── store.py                # 序列化工具
├── singleton.py            # 单例模式
└── logger.py               # 日志配置
config/naturel_gpt_config.yml          # 主配置
config/personas/                       # 人格加载目录（.md 单文件 / skill 文件夹）
data/naturel_gpt/                      # 持久化状态与日志（避免直接修改）
data/naturel_gpt/draw.db               # 绘图提示词数据库
```

# 模块说明

## `__init__.py`

- 导入时加载配置与持久化聊天状态；初始化 `TextGenerator`（传入当前 profile 的 `extra_prompt`）；导入 `matcher` 注册事件处理器。
- `init_tools(config)` 条件注册工具（`LLM_DISABLED_TOOLS` 中的模块跳过）。先 `tavily_search.init(config)` 检查所有 Tavily key 额度选剩余最多的；`tavily_search` 在 Tavily key 可用**或**仅有 `BOCHA_API_KEY` 时也注册（bocha-only 部署下搜索请求在工具内部直调博查 fallback）；博查不再有独立 schema/工具注册（`bocha_search.py` 仅存内部 helper 供 tavily fallback 直调。
- Anima 画图：启动时无条件 health check，通过则自动开启并写回 `COMFYUI_ENABLED = True`。可选画图工作流启动时从 `GET /anima/workflows` 动态拉取（deprecated 过滤，不可达降级 `fuse`）；每群独立 `draw_model`，默认由 `select_default_workflow()` 动态选择（首选 `fuse`）；漫画模式同用该动态默认。
- 初始化 `draw_db.init_db()`。

## `config.py`

- 定义 `GlobalConfig`、`Config`、`PresetConfig`；从 NoneBot 配置读 `ng_config_path`（默认 `config/naturel_gpt_config.yml`）。
- 缺失键由 `CONFIG_TEMPLATE` 补齐并回写规范化 YAML；`yaml.safe_load()`；`save_config()` 用 `Path.parent` 建目录（Windows 路径安全）；`reload_config()` 整体替换 config 对象（避免逐字段覆盖丢失新增字段）。
- `DEFAULT_PERSONA` 指定默认人格，为空/缺失时首个加载人格为默认；`get_persona_dir()` 返回配置旁 `personas/`；`load_dynamic_persona_presets()` 仅从该目录加载。
- **多 OpenAI 配置**：`OPENAI_PROFILES` + `OPENAI_ACTIVE_PROFILE`。旧扁平键（`OPENAI_API_KEYS` 等）自动迁移为 `default` profile；有 `OPENAI_PROFILES` 时旧字段可省略、`save_config()` 自动剔除不写回。每群独立 `active_profile`（持久化在 `ChatData`），消息到达自动切换。旧 `NG_EXT_*`、`PRESETHUB_*` 字段已移除。
- `LLM_DISABLED_TOOLS`：工具模块名列表，`_discover_tools()` 阶段直接跳过；默认空列表。

## `persona_loader.py`

- 两种人格格式同目录共存：简单 `.md` 文件（整文件为 prompt，名=文件名）；Skill 文件夹（含 `SKILL.md`，名=文件夹名第一个 `-` 前部分）。
- Skill 注入顺序：`SKILL.md` → `soul.md` → `limit.md` → `resource/behavior_guide.md` → `resource/key_life_events.md` → `resource/relationship_dynamics.md` → `resource/speech_patterns.md`。`SKILL.md` 剔除 front matter 和通用激活模板后注入。

## `openai_func.py`

- `TextGenerator`（Singleton）。`init()` 接受 `extra_prompt`。
- **Content 兜底**：API 返回 `content: None` 强制转 `""`；`_message_to_dict()` 修复 content 列表缺 `text` 字段问题并将纯文本列表简化为字符串；`_normalize_prompt()` 同等清理，空 assistant content 填 `"[无内容]"` 占位（防 Moonshot 等 provider 400）。
- **Thinking 兼容**：构造 assistant 消息保留 `reasoning_content`，防 tool call 消息缺字段 400。
- **工具结果临时性**：`stream_response()` 返回 `(text, success, tool_messages, reasoning_content)`，工具消息与思考不持久化。入口对传入 prompt 做逐 dict 浅拷贝再追加中间消息（`[dict(m) for m in ...]`：循环内改动均为 dict 级替换——reasoning pop、图片剥离、空 content 填充都是替换不原地改嵌套列表，调用方 prompt 保持请求前快照。工具附件输出按 `chat_key` 分桶，matcher 只能 `consume_tool_outputs(chat_key)` 消费当前会话（防并发串图）。
- **工具调用多轮分段**：中间轮（有 tool_calls）的 assistant 文本经 `on_text` 实时输出，工具执行完插入 `\n\n` 分隔；`intermediate_texts` 在最后一轮前注入 system 提示避免语义重复。
- **`LLM_MAX_TOOL_ROUNDS` 不限制记忆工具**：一轮工具名仅含 `remember` 时不计轮数。
- **工具调用次数限制**：单轮总上限由配置 `LLM_MAX_TOTAL_TOOL_CALLS` 决定（默认 15，代码常量 `MAX_TOTAL_TOOL_CALLS` 同为 15）；搜索工具（`tavily_search`/`bocha_search`）上限 `MAX_SEARCH_TOOL_CALLS=3`。超限只注入内部 system 提示（搜索超限不强制停止）。"已达上限"等内部控制文本必须由 `sanitize_internal_control_text()` 在流式发送/最终返回/失败保存/中间轮缓存前清理，不得作为 `success=True` 回复返回或持久化；已注入控制提示时后续流式文本先缓冲、完成后清洗再发送。**搜索超限提示插入时机**：必须在 `_execute_tool_calls()` 执行完、tool 响应全部 append 到 messages 之后才插入（`_search_limit_hit` 标志延迟插入），严禁在 assistant(tool_calls) 与 tool 响应之间插入 system——OpenAI 协议要求 assistant 的 tool_calls 后必须紧跟对应 tool 响应，中间插入任何消息都会触发上游 400 `"assistant message with 'tool_calls' must be followed by tool messages..."`（error.json 的 tool_messages 不含 system 因此看不出问题）。
- **终端工具轮与循环终止保护（轮数+时间双闸门）**：`TERMINAL_TOOLS = {"generate_anima_image", "remember"}`。进终端阶段的触发：轮数 ≥ `LLM_MAX_TOOL_ROUNDS`、总次数 > `LLM_MAX_TOTAL_TOOL_CALLS`、工具上下文 token 超 `TOOL_CONTEXT_TOKEN_BUDGET`、循环总耗时 > `LLM_TOOL_LOOP_MAX_SECONDS`（默认 180；`stream_response` 入口记 `loop_start`，每轮头部检查，超时注入收尾提示——若末尾恰为 assistant(tool_calls) 则跳过注入仅置标志，守住协议配对）。终端阶段 tools 数组保持全量（缓存前缀不变），非终端调用在执行侧过滤；只允许**单次**终端工具调用——执行后置 `_terminal_wrapup_pending`，下一轮为无工具收尾轮（全量 tools + `tool_choice="none"`）产出最终文本结束；收尾轮模型仍返回 tool_calls 时不再执行、直接以已产出文本收尾。终端阶段不套总次数/工具预算/轮数检查（remember-only 轮不计轮数保留）。所有终止路径返回值都含已累积文本（`_join_intermediate` 合并中间轮文本，收尾轮为空时以中间文本兜底，success=True）。
- **画图任务编号防伪（提示词强制）**：`draw_mode == "force"` 且触发消息含画图关键词（`画`/`draw`/`改图`/`重画`/`来一张`/`整一张`）时，在消息尾部注入一次强制画图提示（`_FORCE_DRAW_HINT_TEXT`，要求必须 tool_calls 调用、禁止只说不画/编造编号；末尾恰为 assistant(tool_calls) 时跳过注入守住协议配对）。**不使用 tool_choice 强制指定**——主流 provider 的思考模式均不兼容（400 "Thinking mode does not support this tool_choice"）；`tool_choice="none"` 仍用于终端收尾轮/撤工具重试，被拒时 `_tool_choice_dropped` 完全省略该字段重试一次。模型收到提示仍不调用时由 matcher 漫画强制画图兜底。历史编号隐藏占位符常量 `[请调用 generate_anima_image 画图工具获取编号]` 不要改动；输出侧 `sanitize_draw_reply_text()` 保留占位符回显与内部控制文本清洗（`allow_task_ids` 参数仅为调用方兼容保留）。
- **流式工具名双拼修复**：provider 重复发 name chunk 导致双拼（如 `generate_anima_imagegenerate_anima_image`），检测 `name[:half]==name[half:]` 去重。
- **工具参数校验**：`tool_calls` 的 `arguments` 逐个校验 JSON 合法性，失败丢弃并记 warning；全部丢弃时注入系统提示并 `continue` 重试（防畸形 JSON 引发 provider 502）；全丢重试独立计数 ≤1 次（`_bad_json_retries`），第二次撤工具（`tool_choice="none"`）强制出文本。
- **缓存命中采集**：请求体加 `stream_options: {"include_usage": True}`，从流末尾 chunk 提取 `usage` 存 `_last_stream_usage`。
- **异常处理**：`stream_response()` 内部循环异常始终 return（不多 key 死循环），由 matcher 外层重试统一处理。
- **并发控制**：工具调用状态按 `chat_key` 判断（`is_tool_calling(chat_key)`），`_current_chat_key`/`_current_trigger_userid` 用任务本地上下文；循环邮箱 entry 必须保留新消息的 `recorded_msg`/`sender`/`userid`/`image_urls` 元数据（见核心架构决策「循环邮箱」）。
- **Profile 切换**：`switch_profile()` 运行时重初始化连接参数。**请求级 Profile 快照**：matcher 调 `stream_response()` 前必须显式传入 `chat.get_active_profile()` 快照；chat_summary 后台任务同样快照传给 `get_response()`。请求开始即固定本轮 `model`/`base_url`/`proxy`/`api_key`/`multimodal`/`enable_stream`，后续都用快照，避免并发群切换 profile 污染当前请求（尤其工具调用后续写轮）。
- **多模态剥离**：profile `multimodal=False` 时 `_completion_kwargs()` 自动把 `image_url` 转文本占位符。
- **流式超时**：httpx `read` timeout 每 chunk 重置；总响应硬上限 5 分钟（`MAX_TOTAL_SECONDS`）。
- **model_mini 回退**：`type='summarize'/'impression'` 用 `model_mini`，为空回退 `model`；`kwargs["model"]` 用已计算的 `model_name`。
- **请求参数安全**：`_request_openai_compatible()`/`_stream_iter_openai()` 入口 `kwargs = dict(kwargs)` 浅拷贝，避免 `pop` 污染调用方。
- **可变默认参数**：公开/半公开方法不得用 `{}`/`[]` 默认参数，用 `None` 函数内新建。

## `llm_tools.py`

- 聚合工具定义；工具输出暂存供 matcher 文本流结束后统一发送。
- `get_tool_schemas()` 按 `chat_key` 经 `anima_generate.get_chat_draw_schema()` 注入画图 schema（函数名统一 `generate_anima_image`）：普通模式用 `draw_model` 对应工作流；漫画模式用动态默认工作流（`get_default_model()`）并在 description 末尾追加漫画规则（返回副本，不污染 `_schema_cache`）。

## `llm_tool_plugins/`

每个工具一个文件，模式：定义 schema + `run(args, config)` 入口。内置工具：

- **`pixiv_search.py`**：Pixiv 图片搜索。多关键词无结果取首个重试；返回不含图片 URL，仅告知模型图片自动发送。
- **`browse_url.py`**：网页抓取多策略链——短链还原→已知社交平台 SSR→Playwright 渲染→trafilatura 兜底→Tavily Extract 服务端爬取（策略 5，单 URL、advanced、markdown，读 `tavily_search._active_api_key`，Tavily key 可用时自动启用。公共函数在 `common.py`：`is_short_url`/`resolve_short_url`（仅命中已知短链域名才发请求，stream 只读响应头）。
- **`tavily_search.py`**：主搜索工具（唯一搜索 schema）。启动 `GET /usage` 检查 key 额度选剩余最多（`TAVILY_API_KEY` 支持多 key）。`include_answer`/`search_depth` 为 `advanced`，`max_results` 固定 20；单条 content 截 300 字符，总长受 `WEB_FETCH_MAX_CHARS` 限制，超预算降级 title+url。Tavily 失败（401/429/432/433 或网络异常）内存标记 `_tavily_disabled`，本次调用直调博查 fallback（`_fallback_to_bocha`，内部 helper 不注册新工具）。
- **`bocha_search.py`**：博查搜索（内部 fallback helper，无独立 schema/注册；`BOCHA_SEARCH_COUNT` 默认 20，强制 10-20）。
- **`tavily_extract.py`** 已删除，其能力并入 `browse_url` 策略 5（`_fetch_with_tavily_extract`，Tavily key 可用时自动启用）。`fetch_url.py`/`tavily_extract.py` 文件已删除。
- **`bangumi_search.py`**：Bangumi 番剧查询。5 合 1 单 schema：`bangumi(action=...)` 单工具 + action 枚举分发（search/get/related/calendar/search_person），旧 5 个独立 schema 已合并为 1 个。
- **`danbooru_search.py`**：Danbooru 标签搜索（为画图工具确定作画标签），仅画图工具注册时随附暴露（`_DRAW_ONLY_TOOLS`，draw_mode=off 时随画图工具一并过滤。
- **`anime_trace.py`**：AnimeTrace 以图识角色（ai.animedb.cn 开放 API）。`anime_trace(image_index)` 从 `tg._current_trigger_images` 取图（与 vision 同源：视觉 profile 为全上下文全局列表，其余为触发消息图片），经 `image_cache.resolve_urls(..., force_base64=True)` 转 data URI 后剥前缀取纯 base64，multipart POST `/v1/search`（`is_multi=1` 多候选、`ai_detect=1` AI 图检测），返回每个人物框的候选「角色名+作品」列表；识别模型经 `GET /v1/model/list` 动态选取（缓存 1 小时，兜底 `animetrace-yuri-4.2`）。业务错误码（17701~17731）映射用户可读提示。所有 profile 均暴露；依赖全局 `MULTIMODAL_ENABLE=true` 才有图片可查。
- **`memory.py`**：长期事实记忆工具，对用户透明。职责边界：只记重要且长期有效的客观事实（名字/称呼/生日/身份、群内规则约定、用户主动要求记住的内容）；性格爱好倾向由用户印象自动归纳、话题进展与群事件由上下文摘要负责，均不用本工具记录。scope：`group`（群共享，注入 `[群记忆]`）/ `user`（按用户，注入 `[你的记忆]`）；记忆与人格关联。action：`save`（key+value）、`delete`（key）、`consolidate`（`operations` 列表批量增删改）。保存透明返回 `已记住：「key」=「value」`。接近上限 80% 不阻断，仅 system2 注入整理提醒。
- **`anima_generate.py`**：ComfyUI Anima 画图工具。
  - `rg draw [force/on/auto/off]` 动态注册/卸载，默认 `auto`，模式持久化 `ChatData.draw_mode`。
  - **工作流动态发现（免适配）**：可选画图模型不再硬编码。启动/`rg draw` 开启时 `fetch_schema_and_knowledge_sync()` 先拉 `GET /anima/workflows` 缓存到 `_workflow_registry`，以其 `workflows` 键集为可选全集；`deprecated: true` 的工作流（如 turbo0.2/kira/nova/miao 系，以 API 返回为准）被 `_build_model_config()` 过滤不进 `MODEL_CONFIG`。API 不可达时降级内置最小默认值（仅 `fuse`），不影响插件启动。
  - **默认选型**：`select_default_workflow()`（可复用纯函数）：首选 `fuse` → 任意名字含 "turbo" 的未弃用工作流 → API 的 `default` 字段 → 内置兜底。`get_default_model()` 为其运行时入口，默认模式与漫画模式共用。
  - `MODEL_CONFIG` 由注册表动态重建（端点统一 `/anima/generate`，schema/knowledge 走 `?workflow=` 主 API；steps/cfg/est_seconds 仅为估算用启发值）；`LEGACY_MODEL_MAP` 处理旧内部名/简写（旧 `turbo`→`turbo_v1`、`turbo2`→`turbo` 等）；`resolve_model_alias()` 只解析当前可选集。旧指令 `rg turbo on/off` 保留为兼容别名（on→动态默认，off→base 或动态默认）。
  - schema/knowledge 拉取：逐工作流走主 API `GET /anima/schema|knowledge?workflow=X`，缓存时函数名统一 `generate_anima_image`。`get_schema(model)`/`get_knowledge(model)` 按名取，空参数取动态默认。
  - knowledge 构建：`_build_workflow_knowledge()` 对**所有工作流**统一压缩（expert 去默认参数/长宽比、artist 只留列表、examples 裁 3 个，目标 ≤ 2k token）；末尾追加参数文档类公共规则 `_COMMON_DRAW_RULES`（外观搜索方式/NSFW 如实写入 tags 等），行为类短规则移至 S1（`get_draw_s1_rules`）。压缩产物由 `_enhance_schema()` 拼入 `generate_anima_image` schema 的 `function.description` 并随 `_schema_cache` 缓存（替代旧 S2 知识注入）；提示词规则（字段写法/质量前缀/模型限制）全部由上游 knowledge 提供，不再按模型写死。
  - `run()` 统一 `POST /anima/generate`（body 顶层带 `workflow` 字段，旧独立端点 `/anima/generate_turbo*` 等不再使用）；漫画模式用动态默认工作流（无视 draw_model），返回简化内容（无编号/ETA），不存 DB。`_do_generate` 后台执行，httpx timeout 300s，`_bg_tasks` 保留 Task 引用防 gc。
  - 调用后即时返回第一人称作画描述 + 预计时间，后台 `asyncio.create_task` 提交；生成结果入 `_pending_results` 由 matcher 消费发图；OneBot 发图时任务编号拼在图前（漫画模式不拼）。
  - **任务编号**：成功返回随机 6 位字母数字 `draw-XXXXXX` 和预计秒数；schema/knowledge 禁止模型编造编号。提示词存 `draw.db` 供 `rg draw-XXXXXX` 查询（漫画模式不存）。
  - **队列限制**：ComfyUI 队列 >5 拒绝；预计时间公式 `当前图片预计时间 + 队列图片数*90秒 - 30秒`。
  - **空参数校验**：`args` 至少含一个有效字段（`character`/`appearance`/`nltags`/`artist`/`series`/`tags`/`style`/`environment`），全空直接返回错误不提交。
  - **漫画模式**（`rg manga on/off/[画风]`，持久化 `ChatData.manga_mode`/`manga_style`）：bot 主动画图增强沉浸感，使用动态默认工作流（首选 `fuse`），无编号/ETA，不存 DB。`MANGA_RULES`（场景列举/频率/防复读）与自定义画风、解锁时 `MANGA_UNLOCK_RULES` 追加到漫画 schema 的 description 末尾（`get_chat_draw_schema()`，由 `llm_tools.get_tool_schemas` 按群调用；`chat.get_unlock_content_limit()` 口径）。NSFW 规则与自然语言 tags 规则始终在 knowledge/description 中，由 system prompt 解锁规则控制激活。空闲检测 `should_inject_manga_idle()`（超 `MANGA_IDLE_MINUTES` 分钟或 `MANGA_IDLE_ROUNDS` 轮）；`manga_idle_draw()` 用 mini 模型按上下文设计场景调画图工具，不输出文字，日志存 `{chat_key}.manga_draw.json`，prompt 顺序：漫画技能（与 schema description 同一份压缩 knowledge，漫画规则不重复注入）→画图指令→群记忆→当前时间→最近对话（→`[未完成的画图请求]`，仅 `pending_request` 非空时注入，要求严格按该请求画面画、禁止自由发挥成无关内容）。`mark_manga_drawn()`/`increment_manga_round()` 维护计数。
- **`nas_game_list.py`**：NAS 游戏目录查询。`get_tool_schemas()` 按 `NAS_GAME_WHITELIST_GROUPS` 白名单过滤 schema（非白名单群工具不可见），`run()` 二次校验。扫描深度上限 `_BRAND_SCAN_MAX_DEPTH=5`，始终递归子目录。所有本地路径均走配置：`NAS_GAME_ROOT_PATH`/`NAS_GAME_UPLOAD_PATH`/`NAS_GAME_BASE_URL`/`NAS_GAME_SYNC_RECORDS_PATH`（同步记录文件，为空则不读取），代码内不得硬编码。

## `chat.py` 及其子模块

`Chat` 类 Mixin 分拆：

- **`chat.py`**：核心类、属性、基本操作（人格切换、Profile 管理）。
- **`chat_memory.py`**：记忆管理（`_get_chat_memory`、`_get_user_memory`、`set_memory`）。
- **`chat_history.py`**：历史管理（`update_chat_history_row`、`save_tool_messages`、`remove_last_prompt_user_message`、`cleanup_after_bad_request`、`_trim_prompt_messages_without_summary`、`_cleanup_orphan_tool_messages`、`_cleanup_orphan_history_messages`、`_count_rounds`、`update_chat_history_row_for_user`）。`save_tool_messages` 修复双拼工具名、剔除不在 `TOOL_REGISTRY` 的调用；按有效 `tool_call_id` 对齐写 tool result；必须 deep copy 嵌套 tool_call 再规范化（不改传入 `msg`），返回本次 assistant tool-call 消息供工具摘要显式绑定。`cleanup_after_bad_request` 只清超 `MULTIMODAL_IMAGE_FRESH_MINUTES` 的图片并清理孤立 assistant/tool。`update_chat_history_row` 为触发 user 写 `user_id`；`CONTEXT_SUMMARY_ENABLED=True` 时 `require_summary=False` 不得触发滑窗裁剪。
- **`chat_prompt.py`**：prompt 构造（`get_chat_prompt_template`、`_build_openai_history_messages`、`_trim_messages_to_request_budget`、`_message_text_for_prompt`、`_message_content_for_prompt`、`_format_prompt_message_for_summary`、`_apply_image_gating`）。
  - **历史单号隐藏**：`_message_text_for_prompt` 把 assistant 历史中的任务编号（`任务编号`/`单号`+可选分隔/加粗/`draw-` 前缀+6位，及裸 `draw-XXXXXX`）替换为 `[编号已隐藏，请调用 generate_anima_image 画图工具]`。
  - **图片门控**：`_apply_image_gating` 独立处理。普通分支只处理真实 user 消息（受 `_image_is_fresh` 有效期检查）；context_only 图片走专用分支（不受有效期限制，始终注入自身消息，关键词仅控去重，按 image_url 去重）；最后执行 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 全局限制。
  - **个人印象注入**：见核心架构决策。`_build_openai_history_messages` 的 `source_messages` 过滤、`msg_role` 判定（印象→system）、`start_idx` 选取（不跳保留轮印象）、token 裁剪 `_oldest_removable_round_indices`（回溯纳入前导印象与该轮 context_only、遇下一轮印象停止）、`_cleanup_orphan_history_messages`（印象绑定紧跟的 user，保持 [印象, context_only, user] 写入顺序）均已适配。最终上下文结构：`[S1][S2 extra_prompt?][S3 压缩摘要?][S4 当前状态（记忆+日期）][历史轮: system印象? user assistant system工具摘要? context_only?]...[system 触发者印象?][system context_only（本轮 flush）?][system 记忆提醒?][user 触发句]`。
- **`chat_summary.py`**：摘要/印象生成（`generate_tool_call_summary`、`_compress_prompt_messages_if_needed`、`_save_summary_log`、`_save_error_log`）。

核心功能：

- `Chat` 围绕持久化 `ChatData` 运作：人格切换、记忆、历史、摘要、印象、prompt 构造、时间戳。
- **实例变量**：`_compress_task`、`_pending_overflow_text`、`_pending_overflow_user_ids`、`_pending_overflow_item_ids`、`_compressing_overflow_item_ids`、`_compress_failure_time`、`_persona_cache`、`_persona_cache_time` 均为实例变量，不得共享类状态。（`_tool_summary_task` 已随摘要同步化删除；`_last_interrupted_response` 随插入式打断删除。）
- **Profile 管理**：`get_active_profile()`/`set_active_profile()`；`apply_profile()` 消息到达自动切换。
- **`get_chat_prompt_template()`** 系统消息 4 条（均在头部，历史之前）：
  - system 1 = 角色设定 + 响应规则 + 工具基础规则 + 画图行为短规则（**极稳定前缀，最大化缓存命中**）
  - system 2 = `extra_prompt`（非空追加）
  - system 3 = 压缩上下文摘要（印象不在此处，per-turn 注入历史）
  - system 4 = 「当前状态」块（`[当前状态]`：群记忆 + 日期）。**低频变化内容放头部**：记忆/日期改动频率低，平时整段历史（含上一轮的 context_only/触发句/回复）都能命中前缀缓存，仅记忆变更或跨天时失效一次；曾试过放尾部（1-5 的 S3 下沉），但上一轮尾部消息每轮都会移到状态块之前，前缀在旧历史末尾就断，每轮都要重算「上轮尾部 + 状态块」，长期更贵，已改回头部。**记忆整理提醒不在此块**：`[记忆提醒]` 是否出现取决于记忆条数是否达到/回落 80% 阈值，放头部会让阈值两侧各打穿一次前缀缓存，故单独以 system 注入尾部触发消息之前（context_only flush 之后；尾部本就是每轮必变的未缓存段，且行动指令离触发消息更近更容易被照做）。在 `_build_openai_history_messages` 返回后插入最终列表，不涉及图片门控下标。
  - 画图知识常驻 `generate_anima_image` schema 的 `function.description`（所有工作流统一压缩，见 anima_generate），不再注入 S2；漫画规则（自定义画风 + `MANGA_RULES` + 解锁时 `MANGA_UNLOCK_RULES`）追加在漫画 schema 的 description 末尾（`anima_generate.get_chat_draw_schema`，仅随漫画/解锁开关与画风设置变化，默认状态稳定）。
  - 画图行为短规则（`anima_generate.get_draw_s1_rules`：仅明确要求作画时调用/禁止编造编号/历史「在画了」需重新调用/调用前后行为规范）常驻 S1 工具段，注入条件 = 本群画图工具实际可用（`draw_mode != "off"` 或漫画模式）；漫画模式不下发"仅明确要求作画"条（主动画图由 description 内 MANGA_RULES 接管）。
  - 工具基础规则约束：先输出 tool_calls，等结果再引用编号，禁止 content 先写编号。
  - 系统提示要求像真实群聊成员自然说话，最多 3 段，不用 Markdown，双换行分段。
- **结构化历史**：
  - `prompt_messages`：核心历史，仅触发回复的 user/assistant/tool/context_only。触发 user 必须存 `user_id`（QQ 号）；图片存消息的 `images` 字段。
  - `_recent_context_buffers`：见核心架构决策「非触发消息缓冲」（旧兼容路径 `Chat._context_buffer` 已删除，唯一条链路）。
  - `chat_impressions`：用户印象字典（`ImpressionData` 含 `nickname`）。
  - `context_summary`：压缩上下文摘要，异步生成。
  - `tool_call_summary`：工具调用摘要（模式3），存对应 assistant 消息，失败 fallback 截断原文。
  - `_compress_failure_time`：摘要失败时间戳，120 秒冷却。
- **失败回复保存**：LLM 失败但有已生成回复（可能含工具结果）时也存 `prompt_messages`，避免上下文断裂。
- **上下文窗口**：轮数只计非 `context_only` 的 `role="user"`。`CONTEXT_WINDOW_SIZE` 是摘要成功后的目标窗口；请求构造和摘要前保留用缓冲窗口 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)`。滑窗/溢出/摘要裁剪均按轮删除完整旧轮次（user/assistant/tool 段），`context_only` 不再豁免、随所在区间一并删除。
- **孤立历史清理**：历史中不允许无真实 user 承接的孤立 assistant/tool（`context_only` 原样保留但不构成轮次）。`_cleanup_orphan_tool_messages()` 清无对应 assistant 的 tool；`_cleanup_orphan_history_messages()` 按状态机：非 context_only user 开轮，assistant/tool 仅在开轮内合法，普通 assistant 闭轮，assistant+tool_calls 及 tool 结果保持开轮至最终 assistant。不能用 `seen_user` 判断（中间 user 被裁会留孤立 assistant）。裁剪、摘要成功、400 清理、prompt 构造前、持久化加载/保存都用同等逻辑。`_build_openai_history_messages()` 截断点落在 assistant/tool 段内时跳到下一真实 user 或 context_only。token 预算裁剪按完整旧轮次删（最旧非触发 user 含前导印象与该轮 context_only → 下一真实 user 前的 assistant/tool/工具摘要；遇下一轮印象停止），保护头部系统消息（含 S4 当前状态块）与触发消息。
- **Token 截断**：准确 token 计算（含图片估算），智能截断优先保留工具结果/摘要/记忆。`_cal_text_tokens` 带模块级 LRU（`_text_token_cache`，容量 2048，key=(model, text)），消除裁剪 while 循环对同一批消息的 O(n²) 重编码；单张图片 token 估算为代码常量 `IMAGE_TOKEN_ESTIMATE = 1000`（OpenAI vision 高分辨率约 765~1105，取保守值让预算裁剪留余量，不再用低估的 85）。
- **图片有效期**：`MULTIMODAL_IMAGE_FRESH_MINUTES`（默认 120 分钟），图片从 `prompt_messages` 检索受门控约束；文本中标记 `[图片N]`。
- **用户印象压缩**：用户消息累积到 `chat_history`（上限 `USER_MEMORY_SUMMARY_THRESHOLD * 2`，仅备份）；印象生成依据为**本次溢出部分该用户的 user 消息**（从 `new_overflow_messages` 提取，不再用 `chat_history[-20:]`），仅对溢出中实际互动的用户生成（`_pending_overflow_user_ids` 跟踪；旧数据无 `user_id` 可用唯一匹配 `nickname` 反查）。印象 prompt 含群昵称与该用户已保存的记忆（避免重复记录），侧重个人特质（性格/爱好/习惯/倾向/互动模式），软限 `IMPRESSION_TARGET_CHARS`（默认 200）字，硬截 2 倍软限。**多用户并发**：各用户印象 prompt 串行构建（纯本地计算），LLM 请求经 `asyncio.gather` 并发（`_gen_impression` 每用户一个协程，失败仅记 error 日志不影响其他用户），总耗时从 Σt 降为 max(t)。
- **记忆管理**：超限不再自动删，仅警告，由 LLM 用 consolidate 整理。群记忆/用户记忆分别人格隔离；接近上限时注入 `[记忆提醒]`（群记忆提醒在尾部触发消息之前，用户记忆提醒在 impression system），文案直接给出工具与策略：调用 `remember`（action=consolidate）批量整理，合并同类、压缩表述，尽量少占条数但保留完整信息。`rg reset` 不清记忆/印象，由 `rg mem clear <scope>` 管理。
- **摘要压缩**：异步（`asyncio.create_task`）不阻塞响应。
  - `_compress_prompt_messages_if_needed()` 只在 bot 回复后 `require_summary=True` 路径调用。
  - 溢出轮数 = 真实 user 轮数 - `CONTEXT_WINDOW_SIZE`；`overflow_rounds > CONTEXT_WINDOW_SIZE * ratio` 才启动摘要，否则保留完整历史走缓冲窗口。
  - 溢出达阈值存 `_pending_overflow_text` 启动异步任务，**先生成摘要成功后再裁剪**。
  - 任务运行期间新消息正常入历史；`_compressing_overflow_item_ids` 与 `_pending_overflow_item_ids` 去重。成功只清自己的 compressing ids，不清运行期间新积累的 pending；失败/取消/异常恢复时与已有 pending 合并，只保留仍在 `prompt_messages` 的消息 id。
  - 摘要完成后按 `id()` 匹配删已总结旧消息段（`context_only` 不豁免、一并删除；印象随绑定轮删除），不依赖 index；`cut_index` 回溯排除保留轮的前导印象与该轮 context_only。删后必须清孤立 assistant/tool。
  - 摘要字数软目标：请求 profile 快照中显式设置的 `max_summary_tokens` 优先，否则用全局 `CONTEXT_SUMMARY_TARGET_CHARS`（默认 800，中文约 1 token≈1 字）；硬截 2 倍软限。摘要 prompt 侧重当前话题连贯性与群历史（`[当前话题]`/`[群历史]` 两节），并附上已保存的群记忆避免重复记录。
  - **失败冷却**：失败设 `_compress_failure_time`，120 秒内不再触发；溢出消息保留，冷却后再捕获。
  - **溢出恢复**：失败恢复 `_pending_overflow_text` 时合并而非覆盖 `_pending_overflow_user_ids`。
  - **异常保底**：摘要 task 必须有 `done_callback` 或外层 `finally`——任何阶段异常都清空 `_compressing_overflow_item_ids`，溢出消息仍在则恢复 pending 三件套以便重试。
  - 完成后 `save_to_file()` 持久化。
- **工具调用摘要（模式3，唯一保留路径）**：
  - 仅搜索类工具生成 LLM 摘要，`SEARCH_TOOLS = {"tavily_search", "browse_url"}`，标 `[搜索工具摘要]`；其他工具保留原始结果（截断），标 `[调用结果]`。`TOOL_CONTEXT_MODE` 配置与模式 1/2 已删除。
  - **同步一次成稿**：`generate_tool_call_summary` 为同步 await 的一次性调用——tavily 结果含 `[AI 摘要]` 时直接采用；无搜索工具结果直接用截断 fallback；其余同步调一次 mini LLM 写终稿，失败保留 fallback。旧"先存占位 + 后台 task 异步回写"链路（`_tool_summary_task`）已删除，消除历史异步回写造成的缓存前缀抖动。
  - `generate_anima_image` 的工具结果仍**不注入历史**（避免占用上下文），但调用本身转为 `[作画记录]` 轻量留痕：直接取该次调用的 `tags`/`nltags` 直出拼接（不调 LLM），并声明"该记录仅表示上述任务已完成；之后任何新的作画请求（含修改/重画/再画一张）都必须重新调用 generate_anima_image 工具"。替代原先"画图轮零痕迹"的做法——模型看不到任何作画记录时容易模仿纯文本的「说画了→说图来了」模式而不实际调用工具。混合其他工具时留痕行排在 `[调用结果]`/`[搜索工具摘要]` 之前。
  - 摘要必须显式绑定 `save_tool_messages()` 返回的本次 assistant tool-call 消息（`target_msg`），不得扫"最后一个带 tool_calls 的 assistant"（并发会挂错）。
  - 历史注入方式（`_build_openai_history_messages` 唯一路径）：历史 tool 原文一律不进 prompt；assistant(tool_calls) 有摘要→摘要 system 替代、无摘要→整条跳过（中间轮文本已并入最终回复 assistant，信息不丢失）；搜索摘要 prompt 含原始问题。

## `matcher.py`

- 主 OneBot v11 入口。`utils.gen_chat_payload()` 提取文本/图片 URL（带位置标记）；允许多模态纯图片消息；`CHAT_ENABLE_RECORD_ORTHER` 分支同样记录图片。
- 调 `stream_response()` 获取输出，按 `\n\n` 分段发送。
- **`no_img` 标记**：指令返回含 `no_img: True` 时强制纯文本发送（用于 `rg draw-XXXXXX`）。
- **画图占位符兜底**：`send_segment()`、成功/失败回复保存都调 `sanitize_draw_reply_text(..., allow_task_ids=True)`，只清历史占位符回显，不删真实编号。
- **Reply + At 修复**：OneBot `_check_reply` 删 reply 段后的 at 段导致 `to_me` 未设；handler 检查 `event.original_message` 有 @bot 则补 `to_me = True`。
- **自定义昵称优先**：`sender_name` 先查 `_custom_nicknames`，再 API 群名片。
- **非触发上下文注入**：`do_msg_response()` 锁内独立计算本条 `should_reply`；False 写 `_recent_context_buffers` 即返回；True 在节流后、生成 prompt 前 `_flush_recent_context_buffer()`。
- **漫画空闲画图**：回复完成后 `increment_manga_round()`；`should_inject_manga_idle()` 为真则 `mark_manga_drawn()` 后异步 `manga_idle_draw()`（空闲分支同样先标记，避免每个回复重复触发）。**漫画强制画图兜底**：manga 开（不限 draw_mode）且回复所应答的请求含画图关键词但该回复未实际调用画图工具，以 `pending_request` 锚定该请求触发 `manga_idle_draw()`（严格按该请求画）并 `mark_manga_drawn()`。**检查去重（`_maybe_manga_autodraw`）**：每个完成的回复在 `_on_reply_complete` 恰好检查一次（按 `reply_entries` 锚定：None=原始触发句，否则为邮箱插入批次中含画图关键词的消息）；matcher 尾部仅当"无任何回复完成"或"最近一次回复检查后又有新工具调用（最终工具轮无文本）"时才补查（`_manga_replies_checked`/`_manga_tool_dirty`），避免同一触发在多个检查点重复触发导致连画多张。
- **图片 400 重试**：图片下载相关 400（`Cannot download image`、`failed to download url data`、`` `text` is not set `` 等）先走**直传回退**：prompt 中含直传 http(s) 图片 URL 时，`_collect_passthrough_image_urls` 收集 → `mark_passthrough_failed` 标记 → `_convert_passthrough_images_to_base64` 原位转 base64 重试一次（`_passthrough_retried`，仅一次）；无直传图或仍失败则回退清历史图片无图重试，最多 2 次；非图片 400 直接失败处理。
- **空 content 400 重试**："must not be empty" 错误（`_is_empty_content_error()`）在 prompt 填 `"[无内容]"` 后重试，最多 2 次。
- **请求打断（循环邮箱）**：见核心架构决策。新消息先独立算 `should_reply`；不需要回复的消息 `return` 前清 `_chat_active_inputs`；需要回复且本群有运行中任务时，触发 user 照常落库后 `push_loop_input` 推入邮箱（锁内，记 INFO 日志），运行中的 `stream_response` 循环在轮边界批量插入继续循环；任务收尾 `_finalize_chat_task()`（finally 覆盖所有出口）drain 残留 entry 逐条重走 `do_msg_response`。旧"cancel 旧任务 + 部分回复注入"链路已删除。
- **工具图片发送**：`consume_tool_outputs(chat_key)` + 发图在 `stream_response` 返回后立即执行，位于所有 `return` 分支前（不被 `success=False` 跳过）。
- **Token 超限**：检测到自动清历史至最后 5 条并提示。
- **唤醒词**：前缀唤醒和名称提及额外检查 `chat.preset_key`（当前角色名也是唤醒词）；名称提及用 `any()`；唤醒词仅句首 `startswith` 触发，句中/句尾走 `RANDOM_CHAT_PROBABILITY`。
- **请求日志**：触发时 `触发回复 | 会话: ... | tokens: xxx + x图`；每个完成的回复由 `_on_reply_complete` 记一次 `回复完成 | ... prompt=X cached=X(xx%) completion=X total=X`（逐回复，含 token 统计）；仅在没有任何回复完成（失败路径）时由外层记一次 `请求结束（无完成回复）`，不重复记。
- **Think 标签实时拦截**：`on_text_chunk` 状态机实时提取到 `_extracted_reasoning`；`_strip_think_tags` 兜底。
- **并发控制**：`_chat_response_lock`（真 `asyncio.Lock()`）保护 `_chat_running_tasks`/`_chat_active_inputs`。
- `do_msg_response` 的 `loop_data` 默认 `None`（函数内 `or {}`），避免可变默认参数。

## `image_cache.py`

见核心架构决策「图片缓存」。补充：`resolve_urls` 批量解析去重保序过滤空结果；`resolve_url/resolve_urls` 接受 `force_base64`（默认 False，vision/anime_trace 传 True）；`_is_direct_passable` 判定公开 URL（QQ 域关键词 + `ipaddress` 私网/环回/保留地址排除）；`_passthrough_bad` 记录 provider 拉取失败的直传 URL（`purge_stale` 一并清理）。

## 其他模块

- **`singleton.py`**：`threading.Lock` + 双重检查锁定；子类各自 `cls._instance`。
- **`chat_manager.py`**：全局会话管理器；显式导入 config；`_chat_dict` 实例变量。
- **`utils.py`**：`gen_chat_payload()`（文本+唤醒标志+图片 URL）、`gen_chat_text()`、`_extract_message_text_and_images()`（`[图片N]` 标记）；`async_fetch()` 统一 `async with`；`translate()` 用 httpx；用户名解析容忍 OneBot API 失败。
- **`text_to_image.py`**：可选渲染，依赖 `nonebot_plugin_htmlrender`，导入失败自动关标志。

## `command_func.py`

- `CommandManager` 实现所有 `rg` 指令。`cmd.register()` 用 `params: Optional[list] = None`；`resolve_command()` 跳过未定义选项、`-param` 末尾越界边界检查；`execute()` 返回 `{'error': str(e)}` 保证 JSON 安全；`rg draw-XXXXXX` 格式直接路由查询函数。
- `rg` / `rg list`：重载并列人格；`rg set <persona>`：切换人格。
- `rg draw [force/on/auto/off]`：画图开关（health check → 拉 schema/knowledge → 注册/卸载工具 → 持久化 `ChatData.draw_mode`）。`force`=常驻+画图关键词时尾部注入强制画图提示（不用 tool_choice，provider 思考模式不兼容）；`on`=常驻；`auto`=常驻、调用时机由 description/S1 规则约束（默认）；`off`=关闭（仅 off 会从 tools 中过滤画图工具）。
- `rg draw <model>`：切换画图模型（可选集由上游 `/anima/workflows` 动态决定，弃用工作流不可选，见 anima_generate 一节）。持久化 `ChatData.draw_model`，默认由 `select_default_workflow()` 动态选择（首选 `fuse`）；旧内部名/简写经 `LEGACY_MODEL_MAP` 迁移，旧 `turbo_mode=True` 迁移为 `turbo_v1`。切换时按需拉 schema/knowledge。
- `rg draw <json>`：按 JSON 创建绘图任务，允许空值，检查队列与预估，存 `draw.db`；按 `draw_model` 选默认 steps/cfg 估算值。
- `rg draw-XXXXXX`：查询提示词，JSON 输出，`no_img` 强制纯文本。
- `rg turbo [on|off]`：废弃兼容别名（on→动态默认加速工作流，off→base 或动态默认）。
- `rg manga [on|off|画风]`：漫画模式开关/自定义画风；`rg manga clr` 清画风。使用动态默认工作流（首选 `fuse`），覆盖 `rg draw` 模式，持久化 `manga_mode`/`manga_style`。
- `rg nolimit [on|off]`：内容限制解锁，每群独立持久化 `ChatData.unlock_content_limit`（`None` 回退 `UNLOCK_CONTENT_LIMIT` 默认值）。
- `rg model [profile]`：列出/切换 OpenAI profile，按群生效。
- `rg nn [昵称]`：自定义昵称设置/查询/清除。
- `rg mem`：查看记忆；`rg mem clear <group|user|all>`：清除。
- `rg help`：帮助（含工具状态）；`rg stat`/`rg stat reset`：运行统计（管理员）。
- `rg reset` 只清上下文（prompt_messages/context_summary），保留记忆、印象、昵称。

## `persistent_data_manager.py`

- `ChatMessageData` 支持 `user`/`assistant`/`tool`/`system`：`tool` 含 `tool_call_id`/`tool_name`；`assistant` 可含 `tool_calls`/`tool_call_summary`；`user` 含 `user_id`；`context_only` 与 `is_impression`+`impression_user_id` 标记的 system 加载时强制 `role="system"`、不持久化（`_serializable` 过滤）。
- `ImpressionData`：`user_id`、`nickname`、`chat_history`（备份）、`chat_impression`。
- `PresetData`：`chat_memory`、`user_memories`、`chat_impressions`，与人格关联。
- `ChatData`：`active_profile`、`draw_mode`（默认 `auto`）、`draw_model`（默认空 = 读取时动态选默认；旧 `turbo_mode` bool 迁移 `True→turbo`/`False→base`，旧内部名由 `get_draw_model()` 经 `LEGACY_MODEL_MAP` 映射，删旧字段）、`manga_mode`、`manga_style`、`unlock_content_limit`（`None` 回退 `UNLOCK_CONTENT_LIMIT`）。`chat_image_history` 已弃用仅兼容。
- `PersistentDataManager._custom_nicknames`：`{user_id: nickname}`，随保存持久化。
- 默认读写 `data/naturel_gpt/naturel_gpt.json`（可配 pickle）；`save_to_file()` 节流（60 秒），`must_save=True` 跳过节流。
- **写盘线程卸载**：事件循环运行中时 `save_to_file` 把序列化+写盘卸载到执行器线程（`loop.run_in_executor`），避免大 JSON dump 阻塞 event loop；`_SAVE_LOCK`（threading.Lock）串行化并发写（共享 `.tmp` 路径）；序列化期间数据被并发修改导致失败仅告警（原子写不损坏已有文件），由下一次保存兜底。无运行中循环（启动迁移等）退化为同步写。`save_to_file_blocking()` 同步写并等待完成，仅 shutdown 钩子使用（保证进程退出前落盘）。
- **原子写入**：先写 `.tmp` 再 `os.replace`，失败回退直接写。
- `init_global_memory()` 只清当前会话记忆。
- 加载/保存过滤孤立 assistant/tool；`role="system"`（含 context_only、印象）不持久化；旧 `context_only=True` 数据加载丢弃；`请求大模型时发生错误: ...`、`RuntimeError('HTTP ... Error from provider ...')` 等内部异常在加载/保存/清理/prompt 构造前都必须过滤。

## `draw_db.py`

- SQLite：`data/naturel_gpt/draw.db`，表 `draw_prompts(task_id PK, prompt_data, created_at, updated_at)`。
- `save_prompt`（`INSERT OR REPLACE`）、`get_prompt`、`delete_prompt`、`list_prompts(limit)`（更新时间倒序）；`threading.Lock` 线程安全；启动 `init_db()`。

# Prompt 与回复规范

- 像真实群聊成员自然简短回复；普通最多 3 段，Markdown 模式最多 4 段；正常文本不用 Markdown。
- 分段用双换行 `\n\n`；发送端在双换行处拆分，段内双换行折叠为单换行；后处理兜底去 Markdown 标记。
- 工具调用过程不得显式出现在最终回复；工具结果以 system 注入，禁止模型模仿 `[调用结果]`/`[搜索工具摘要]` 格式。

# 配置字段速查

## LLM

- `OPENAI_PROFILES`：`api_keys`/`base_url`/`proxy`/`timeout`/`model`/`model_mini`/`temperature`/`top_p`/`max_tokens`/`max_summary_tokens`/`frequency_penalty`/`presence_penalty`/`multimodal`/`extra_prompt`/`thinking`/`no_think`/`keep_reasoning`/`reasoning_effort`（`no_think` 为 true 时响应规则注入 `/no_think` 指令显式关闭思考，默认 false；`keep_reasoning` 默认 false——跨轮历史 assistant 的 `reasoning_content` 在 `stream_response` 入口统一剥离以兼容不接受该字段的 provider，true 时历史思考链也注入、provider 400 拒绝时自动剥离重试一次；`reasoning_effort` 可选 `low`/`medium`/`high`，仅主对话请求透传，摘要/印象走 `model_mini` 不传）
- `OPENAI_ACTIVE_PROFILE`、`LLM_ENABLE_STREAM`、`LLM_SHOW_REASONING`、`LLM_ENABLE_TOOLS`
- `LLM_DISABLED_TOOLS`：禁用工具模块名列表；`LLM_MAX_TOOL_ROUNDS`（不限 `remember`）；`LLM_TOOL_LOOP_MAX_SECONDS`（工具循环总耗时上限秒数，默认 180，超时进终端轮收尾）

> 旧格式兼容字段（有 `OPENAI_PROFILES` 可省略）：`OPENAI_API_KEYS`、`OPENAI_BASE_URL`、`OPENAI_PROXY_SERVER`、`OPENAI_TIMEOUT`、`CHAT_MODEL`、`CHAT_MODEL_MINI`、`CHAT_TEMPERATURE`、`CHAT_TOP_P`、`CHAT_PRESENCE_PENALTY`、`CHAT_FREQUENCY_PENALTY`、`CHAT_MAX_SUMMARY_TOKENS`、`REPLY_MAX_TOKENS`

代码常量（`openai_func.py`）：`MAX_TOTAL_TOOL_CALLS=15`（实际值从配置 `LLM_MAX_TOTAL_TOOL_CALLS` 读取）、`MAX_SEARCH_TOOL_CALLS=3`、`SEARCH_TOOL_NAMES`（`tavily_search`/`bocha_search`，后者为历史遗留——博查不再注册为工具，模型不会产生该调用）、`IMAGE_TOKEN_ESTIMATE=1000`（单图 token 估算）。

## 上下文管理

- `CONTEXT_TOKEN_BUDGET`：prompt 最大 token，默认 4096
- `CONTEXT_WINDOW_SIZE`：目标窗口（真实触发轮数）；缓冲窗口 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)`
- `CONTEXT_BUFFER_SIZE`：旧配置仅兼容；非触发缓冲窗口按上式独立计算
- `CONTEXT_SUMMARY_ENABLED`：启用摘要压缩
- `CONTEXT_COMPRESS_THRESHOLD_RATIO`：触发阈值乘数（默认 0.5），同时决定缓冲窗口
- `CONTEXT_SUMMARY_TARGET_CHARS`：上下文摘要字数软目标（默认 800，硬截 2 倍）；profile `max_summary_tokens` 可覆盖
- `IMPRESSION_TARGET_CHARS`：用户印象字数软目标（默认 200，硬截 2 倍）
- `TOOL_CONTEXT_TOKEN_BUDGET`：工具+思考 token 预算，默认 16384

## 消息分段

- `NG_ENABLE_MSG_SPLIT`、`REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS`

## 多模态

- `MULTIMODAL_ENABLE`、`MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`（超限从最旧剥图）、`MULTIMODAL_IMAGE_FRESH_MINUTES`（默认 120）、profile 级 `multimodal`

## 工具

- `TAVILY_API_KEY`（多 key 选额度最多）、`BOCHA_API_KEY`、`BOCHA_API_BASE`、`BOCHA_SEARCH_COUNT`（默认 20，强制 10-20）
- `COMFYUI_BASE_URL`、`MANGA_IDLE_MINUTES`（默认 5）、`MANGA_IDLE_ROUNDS`（默认 5）
- `WEB_FETCH_TIMEOUT`、`WEB_FETCH_MAX_CHARS`、`PLAYWRIGHT_TIMEOUT`、`LLM_TOOL_LOLICON_CONFIG`
- `UNLOCK_CONTENT_LIMIT`：解锁全局默认值（默认 `False`），每群 `rg nolimit` 覆盖

## 人格

- `DEFAULT_PERSONA`；人格目录 `Path(config_path).resolve().parent / "personas"`；为空/缺失时首个加载人格为默认，无加载人格用内置 `default`。

# 开发规范

- 改动范围默认限于 `ATRI/plugins/nonebot_plugin_naturel_gpt/` 和显式配置/人格 fixture。
- 改 matcher 前梳理普通消息流和 `rg` 指令流；改 prompt 前读 `Chat.get_chat_prompt_template()`；改人格加载前读 `config.py`、`persona_loader.py`；改持久化前读 `persistent_data_manager.py`，保持 JSON 兼容。
- 改配置字段同步更新：`Config`、`CONFIG_TEMPLATE`、`_load_config_obj_from_file()` 迁移/默认值、`README.md`。
- 新增 OneBot 消息处理同步 `utils.gen_chat_payload()` 和 `matcher.do_msg_response()`。
- 新增指令用 `cmd.register(...)` 并保留权限检查；新增工具在 `llm_tool_plugins/` 加文件经 `llm_tools.py` 注册。
- 非必要不执行访问外部 API 的命令。

# 验证清单

- 语法检查所有变更 Python 文件；大范围变更编译核心文件：`__init__.py`、`chat.py`、`config.py`、`command_func.py`、`matcher.py`、`openai_func.py`、`llm_tools.py`、`persona_loader.py`、`persistent_data_manager.py`；工具变更编译所有 `llm_tool_plugins/*.py`。
- Matcher 变更覆盖：群聊/私聊/纯图片消息；`rg`、`rg list`、`rg set <persona>`；忽略前缀、禁用户/禁群；`at` 与 `at all`。
- 人格变更验证简单 `.md` 与 skill 文件夹共存。
- 配置变更验证不丢未知字段、正确补默认值。
