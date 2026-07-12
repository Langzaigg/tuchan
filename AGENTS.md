# 项目概述

这是一个基于 NoneBot2 / OneBot v11 的 QQ 机器人项目，核心功能由 `naturel_gpt` 插件提供。当前所有功能开发、问题修复和配置调整均围绕该插件展开。

## 核心架构决策

- **LLM 后端**：通过 LiteLLM 统一调用（`openai_func.py`），支持多 key 轮询、自定义 base_url、代理、流式输出。支持多组 OpenAI 配置（`OPENAI_PROFILES`），通过 `rg model` 指令运行时切换。
- **工具调用**：使用原生 OpenAI-compatible Tool Calling 替代旧的文本协议（`/#tool&args#/`）。工具定义在 `llm_tool_plugins/` 下，由 `llm_tools.py` 聚合调度。
- **先搜后答原则**：`chat_prompt.py` 的 `tool_text` 与 `rules` 各注入一条精简约束——对外部事实（人物/作品/日期/数据/新闻等）不确定时，先调 `tavily_search`（或 `bocha_search`）核实再答，禁止凭记忆编造。日常闲聊/情感/人格自我描述/上下文已给出信息不受此约束。
- **流式回复**：开启 `LLM_ENABLE_STREAM` 后，模型输出按双换行 `\n\n` 分段发送，受 `REPLY_SEGMENT_INTERVAL` 和 `REPLY_MAX_SEGMENTS` 控制。
- **多模态输入**：支持 OneBot 图片消息片段，解析为 `image_url`。用户消息中的图片以 `[图片N]` 占位符内联标记。**图片门控**：为所有含图 user 消息（触发消息 + 历史用户消息）注入图片，不限时效、不限窗口；context_only 缓冲区的图片仅当触发句包含图片关键词（图/画/上/这等）时才收集并注入触发 user 消息，否则不注入。超限策略：当含图消息数超过 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 时，清空所有非触发消息的图片，仅保留触发消息的图片，避免滚动清理导致缓存不命中。每个 profile 有独立的 `multimodal` 开关，关闭时请求层自动剥离 `image_url` 内容。`_is_supported_image_url` 支持 `http://`、`https://`、`data:image/`、`file:///` 协议。
- **图片缓存**：`image_cache.py` 提供异步图片下载 + 内存 LRU 缓存，将远程 URL 转为 `data:image/xxx;base64,...` 格式提交给 LLM API，避免 API 侧无法访问 QQ 等私有 URL。单图上限 10MB，总缓存上限 50MB。每次 `get_chat_prompt_template()` 构建完毕后自动清除不在 `prompt_messages` 中的缓存条目。下载时自动附加 `User-Agent` 和 QQ 域名 `Referer` 头提升成功率，失败时回退到原始 URL。
- **Think 标签过滤**：Grok 等模型将思考内容以 `<think>` 标签放在 `content` 中返回。流式回调中实时拦截 `<think>...</think>` 内容，提取到 `reasoning_content` 字段；兜底正则在最终响应上做二次过滤。
- **思考泄漏兜底**：部分模型开启思考后既不返回 `reasoning_content` 字段、也不输出 `<think>` 标签，而是把思考过程直接混在 `content` 里，导致思考被当成回复分段发到群里。每个 `OPENAI_PROFILES` 条目可设 `thinking: bool`（默认 `true`）标记思考模式。思考模式下，`on_reasoning_chunk` 标记 `_saw_reasoning`；`on_text_chunk` 在收到 content 时若发现「无 reasoning_content 且无 `<think>` 标签」，置位 `_skip_think_buffer_mode`，停止流式分段提前发送，全部缓冲到 `stream_buffer`。流结束后若 `content` 长度超过 `THINK_LEAK_THRESHOLD`（默认 300）且含双换行，则 `rsplit("\n\n", 1)` 取最后一段作为实际回复，前段存入 `reasoning_content`（仅用于 debug 日志，不进历史、不发群）；失败路径同样兜底 `raw_res_for_save`，避免思考随部分回复进入历史。`thinking: false` 的 profile 走原流式分段逻辑，不受影响。
- **非触发消息缓冲**：不需要回复的群消息不写入 `prompt_messages`，而是由 `matcher.py` 的 `_recent_context_buffers` 按 `chat_key` 存入入口层临时缓冲区。`[群聊上下文-非触发消息]` 有独立窗口，大小按 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)` 计算，不使用触发对话历史的裁剪点，也不依赖 `CONTEXT_BUFFER_SIZE`。并发场景下必须先基于本次新消息独立计算 `should_reply`，再决定是否合并旧 active input；非触发消息不能继承旧触发状态，必须走 `_push_recent_context_buffer()`。当下一条触发消息到达并通过节流检查后，入口层缓冲和兼容用的 `Chat._context_buffer` 一并 flush，内容作为 `context_only` system 消息注入到 `prompt_messages` 中（置于触发消息之前），带有 `[群聊上下文-非触发消息]` 前缀；flush 必须发生在生成 prompt 之前且在 `REPLY_THROTTLE_TIME` 之后，确保触发语句附近、节流窗口内收到的非触发消息也进入本轮 prompt。`context_only` 消息不计入对话轮数、不增加 token 消耗，每次追加前清除旧的 `context_only`。上下文中图片占位符用全局计数器区分（如 Marcel 的 `[图片1]` 和 严肃早睡中的 `[图片2]`）。`context_only` 消息使用 `role="system"`，不进入持久化存储。**截断保护**：`_trim_prompt_messages_without_summary()` 和 `_compress_prompt_messages_if_needed()` 在删除溢出消息时保留 `context_only` 消息，只删除真实 user/assistant/tool 轮次，确保非触发上下文在窗口滑动时不丢失。
- **Reply 消息上下文注入**：当触发消息包含 reply 段时，从 `event.reply.message` 提取被回复消息的文本和图片，以 `[回复 xxx 的消息] 文本 [图片N]` 前缀拼接到触发消息前面，图片插入到 `image_urls` 前面并重新编号已有标记。
- **自定义昵称**：用户可通过 `rg nn <昵称>` 设置在 bot 中的固定昵称，存储在 `PersistentDataManager._custom_nicknames`（全局，跨群生效），优先于 API 获取的群名片。`rg nn` 查询，`rg nn 清除` 删除。
- **个人印象注入（per-turn，重构）**：印象不再在 System 4 根据触发句中提到的所有角色集中列出，而是固定只注入触发者的印象。`update_chat_history_row` 在记录触发 user 消息前，若 `prompt_messages` 中尚无该 `user_id` 的印象 system（`is_impression=True`，按 `impression_user_id` 去重）且该用户 `chat_impression` 非空或有用户记忆，则先 append 一条印象 system（内容为印象正文+用户记忆，由 `_message_text_for_prompt` 格式化为 `[用户印象: 昵称]\n正文\n[你的记忆]\n1. key: value`），再 append user。用户记忆现在与印象绑定在同一 system 消息中，不再单独注入 System 3，提高缓存命中率。一个角色的印象在整个上下文中只注入一次，绑定到其首次触发的轮次；摘要/裁剪删除该轮次时连同其前导印象一起删除，下次该用户触发时上下文中已无其印象，自然注入更新后的新印象，既避免同一用户印象重复注入、避免旧印象残留，又保证历史轮一旦写入即固定不变（含其印象 system），从而在多人使用时历史前缀稳定、prompt 缓存可稳定命中。印象 system 不持久化（`PresetData._serializable` 过滤 `role=system`），重启后丢失，下次触发按最新 `chat_impression` 和用户记忆重新注入。`mentioned_userids` 提取与传参已移除。
- **人格系统**：运行时动态加载，来源固定为 `config/personas/` 目录。YAML 中的 `PRESETS` 仅作为运行时容器，不作为人工编辑源。
- **扩展系统**：旧版 Extension 运行时扩展系统、PresetHub 集成、`/#...#/` 文本协议均已移除，不再使用。
- **Debug 日志**：每次 LLM 请求完成后，无论 `success=True` 还是失败路径，都将最近一次请求/响应保存到 `data/naturel_gpt/logs/{chat_key}.latest.json`（图片 base64 替换为占位符）；摘要任务完成后，将摘要 LLM 的请求/响应保存到 `{chat_key}.summary.json`。reasoning 内容完整保存，不截断。`latest.json.prompt` 必须记录请求发出前的 prompt 快照，`stream_response()` 内部必须 deep copy prompt，避免工具调用追加 assistant/tool 消息污染 debug prompt。摘要日志包含上下文摘要、工具摘要和用户印象（如有）。debug 日志中 `response` 为最终回复或错误/部分回复，`intermediate_responses` 为中间轮 assistant 回复文本列表，`tool_messages` 为完整工具调用链；这些文本字段写入前也必须经过 `sanitize_internal_control_text()`，避免内部控制提示落盘后再次误导排查。`请求大模型时发生错误: ...` 这类内部模型请求异常可以保留在 latest debug 日志中用于排查，但不得保存为 assistant 历史、不得进入 prompt、不得原样发送到群聊。
- **Error 日志**：每次 `stream_response` 返回 `success=False` 时，立即保存 prompt 到 `{chat_key}.error.json`（每个群只保留最新一份），用于排查 API 兼容性问题。prompt 和 tool_messages 经过 `_sanitize_prompt_for_log` 处理，base64 图片替换为 `[base64图片，已省略]` 占位符，方便复制粘贴调试。保存时机在重试判断之前，即使后续重试成功，失败的那次请求也会被记录。摘要/印象任务失败时同样写入 error log（`source: "summary_task"`），便于排查后台 LLM 请求问题。
- **请求打断与部分回复保留**：同群新消息打断旧请求时，已接收的流式内容（剥离 `<think>` 标签后）保存到 `Chat._last_interrupted_response`，下次请求时作为 system 消息注入上下文，避免模型重复已说过的内容。`_last_interrupted_response` 为实例变量，每个 Chat 实例独立。旧请求处于工具调用阶段时不 cancel，也不得把旧 active input 与新触发输入合并或删除旧请求已记录的 user；只能把本次新触发输入原样放入 `_pending_merge_input`，待旧请求完成后作为独立下一轮处理。
- **模型专用提示词**：每个 `OPENAI_PROFILES` 条目可设置 `extra_prompt` 字段，注入到 system1 消息末尾。用于针对特定模型的行为调优（如 kimi 的工具调用积极性、减少推理等）。
- **人格热加载缓存**：`chat_preset` 属性带 5秒 TTL 缓存，避免每次访问都做磁盘 I/O。`_persona_cache` 和 `_persona_cache_time` 为实例变量。
- **运行统计（stats.py）**：`StatsManager` 单例按日期分桶记录运行数据，持久化到 `data/naturel_gpt/stats.json`，每日刷新（只看当天）。采集点：(1) 触发回复次数——`matcher.py` `do_msg_response` 成功后 `stats.inc_trigger()`；(2) 各模型 token 消耗——`openai_func.py` `_stream_once`/`_complete_once` 每次实际 API 请求后 `stats.record_model_usage(model_name, usage)`，兼容 OpenAI(`prompt_tokens_details.cached_tokens`)/Anthropic(`cache_read_input_tokens`)/DeepSeek(`prompt_cache_hit_tokens`) 三种缓存字段；(3) 工具调用次数——`_execute_tool_calls` 每个工具执行前 `stats.inc_tool_call(name)`（成功失败都计）。查询指令 `rg stat`（管理员）渲染当日：触发次数、各模型 token(prompt/completion/cached/命中率/请求数)、整体缓存命中率、各工具调用次数；`rg stat reset` 清空当日。模型名取 `OPENAI_PROFILES` 配置的 `model` 字段（即实际请求的模型名）。

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
├── llm_tool_plugins/       # 工具插件目录（含 tavily_extract.py）
├── image_cache.py          # 图片下载缓存（URL→base64 data URI）
├── persistent_data_manager.py  # 持久化数据管理
├── stats.py               # 运行统计（触发次数/模型token/缓存命中/工具调用，按日分桶）
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

- 导入时加载配置与持久化聊天状态。
- 初始化 `TextGenerator`，从当前激活 profile 读取 `extra_prompt` 并传入。
- 导入 `matcher`，通过导入副作用注册事件处理器。
- 调用 `init_tools(config)` 进行条件工具注册（`LLM_DISABLED_TOOLS` 列表中的工具在 `_discover_tools` 阶段跳过加载）。启动前先调用 `tavily_search.init(config)` 检查所有 Tavily key 的额度并选用剩余最多的 key；若配置了 `TAVILY_API_KEY` 则优先注册 `tavily_search`，`bocha_search` 仅在 Tavily 不可用时作为 fallback 注册。`tavily_extract` 共享 Tavily key，在 `tavily_search` 可用时自动随 `_discover_tools` 注册。
- Anima 画图：启动时无条件执行 health check，通过则自动开启（不再依赖 `COMFYUI_ENABLED` 持久化状态），并将 `COMFYUI_ENABLED = True` 写回配置。支持 Turbo 加速模式（`rg turbo on/off`），每群独立，默认开启。Turbo 模式使用 anima-turbo-lora 工作流（8步，约15秒），普通模式使用 35 步（约60秒）。支持漫画模式（`rg manga on/off/[画风描述]`），开启后 bot 会积极主动画图来增强角色扮演沉浸感，无视 turbo 选项固定使用 turbo 工作流，不需要任务编号和 ETA，不保存 prompt 到 DB。
- 初始化绘图提示词数据库 `draw_db.init_db()`。
- 不再检查 PresetHub 连通性，不再加载旧扩展。

## `config.py`

- 定义 `GlobalConfig`、`Config`、`PresetConfig`。
- 从 NoneBot 配置读取 `ng_config_path`，默认指向 `config/naturel_gpt_config.yml`。
- 缺失键由 `CONFIG_TEMPLATE` 补齐，加载时回写规范化 YAML。
- YAML 中的 `PRESETS` 不作为输入源，仅用于运行时动态人格存储。
- `DEFAULT_PERSONA` 指定默认加载的人格；为空或缺失时，首个加载人格设为默认。
- `get_persona_dir()` 返回配置所在目录的 `personas/` 子目录。
- `load_dynamic_persona_presets()` 仅从 `config/personas/` 加载人格。
- **多 OpenAI 配置**：`OPENAI_PROFILES` 存储多组配置，`OPENAI_ACTIVE_PROFILE` 指定当前激活配置。旧格式的扁平键（`OPENAI_API_KEYS`/`OPENAI_BASE_URL` 等）自动迁移为 `default` profile。每个群有独立的 `active_profile`（持久化在 `ChatData` 中），消息到达时自动切换到该群的 profile。每个 profile 可设置 `extra_prompt`（模型专用追加提示词）。
- **旧格式兼容**：`Config` 中的旧扁平键字段（`CHAT_MODEL`、`OPENAI_API_KEYS` 等）均有默认值，YAML 中有 `OPENAI_PROFILES` 时可省略。`save_config()` 在有 `OPENAI_PROFILES` 时自动剔除旧字段，不写回 YAML。缺失旧字段的日志在有 `OPENAI_PROFILES` 时被抑制。
- 旧 `NG_EXT_*`、`PRESETHUB_*` 配置字段已移除。
- **禁用工具列表**：`LLM_DISABLED_TOOLS` 为工具模块名列表（如 `['pixiv_search']`），`_discover_tools()` 注册阶段直接跳过；默认值为空列表，需在 YAML 中显式配置。
- **YAML 安全加载**：使用 `yaml.safe_load()` 替代 `yaml.FullLoader`，防止反序列化攻击。
- **路径处理**：`save_config()` 使用 `Path.parent` 创建目录，避免 `[:-1]` 截断在 Windows 路径上出错。
- **配置重载**：`reload_config()` 直接替换整个 config 对象，避免 `setattr` 逐字段覆盖丢失新增字段。

## `persona_loader.py`

- 从同一目录加载两种人格格式：
  - **简单 `.md` 文件**：整文件作为 prompt，人格名为文件名（不含扩展名）。
  - **Skill 风格文件夹**：包含 `SKILL.md` 时构成一个人格，人格名为文件夹名中第一个 `-` 之前的部分。
- Skill 文件夹文件注入顺序：`SKILL.md` → `soul.md` → `limit.md` → `resource/behavior_guide.md` → `resource/key_life_events.md` → `resource/relationship_dynamics.md` → `resource/speech_patterns.md`。
- `SKILL.md` 会剔除 front matter 和通用激活模板文本后再注入。

## `openai_func.py`

- 定义 `TextGenerator`（Singleton）。
- 直接调用 OpenAI-compatible API，支持 API key 轮询、base_url、代理、超时、流式输出。
- `init()` 接受 `extra_prompt` 参数，启动时即加载当前 profile 的模型专用提示词。
- 支持原生工具调用多轮交互（`llm_tools.py`）。
- 支持可选推理内容回调（`LLM_SHOW_REASONING`）。
- **Content 兜底**：API 返回 `content: None` 时强制转为 `""`，避免 Moonshot 等后端报 "assistant message must not be empty"。`_message_to_dict()` 还会修复 content 列表中 `{"type":"text"}` 缺失 `text` 字段的问题（Xiaomi/mimo 等 provider 可能返回此格式），并将纯文本列表简化为字符串以避免 provider 兼容性问题。`_normalize_prompt()` 对传入的消息列表做同等清理，并将 assistant 消息中为空的 content 填充为 `"[无内容]"` 占位符，防止 Moonshot 等 provider 拒绝空 content 的 assistant 消息。
- **Thinking 模式兼容**：构造 assistant 消息时，若存在 `reasoning_content` 会一并保留，防止 tool call 消息缺少该字段导致 400。
- **工具结果临时性**：`stream_response()` 返回四元组 `(text, success, tool_messages, reasoning_content)`，工具消息和思考内容只在当前请求内使用，不持久化。`stream_response()` 必须对传入 prompt 做深拷贝后再追加 assistant/tool 中间消息，避免污染调用方的 `prompt_template` 和 debug 日志。工具产生的附件输出（如 Pixiv 图片）必须按 `chat_key` 分桶暂存，`matcher.py` 只能通过 `consume_tool_outputs(chat_key)` 消费当前会话附件，避免并发群请求串图。
- **工具调用多轮分段**：中间轮（有 tool_calls）的 assistant 文本通过 `on_text` 回调实时输出，工具执行完毕后自动插入 `\n\n` 分隔符，与最终轮分段。`intermediate_texts` 收集中间轮文本，在最后一轮前注入 system 提示避免语义重复。
- **`LLM_MAX_TOOL_ROUNDS` 不限制记忆工具**：流式回调中每轮结束后检查工具名集合，若仅包含 `remember` 则不计入轮数，允许记忆整理不受轮数限制。
- **工具调用次数限制**：单轮总工具调用次数上限 `MAX_TOTAL_TOOL_CALLS=7`；单轮搜索工具（`tavily_search`/`bocha_search`）调用次数上限 `MAX_SEARCH_TOOL_CALLS=3`。总工具调用次数超限时只注入内部 system 提示，要求模型停止继续调工具并基于已有结果回答；搜索超限时不强制停止，仅注入系统提示提醒模型不再调用。所有“工具调用次数已达上限”“搜索工具调用次数已达上限”等内部控制文本必须由 `sanitize_internal_control_text()` 在流式发送、最终返回、失败保存和中间轮缓存前清理，不能作为 `success=True` 的 assistant 回复返回或持久化；若已注入内部控制提示，后续流式文本应先缓冲，完成后清洗再发送，避免模型照抄控制提示直出。
- **终端工具轮**：`TERMINAL_TOOLS = {"generate_anima_image", "remember"}`。当总工具调用次数超限或工具上下文 token 超预算（`TOOL_CONTEXT_TOKEN_BUDGET`）时，不立即停止，而是设置 `_allow_terminal_tools = True`，下一轮 `current_tools` 仅包含终端工具定义，非终端工具调用被过滤；终端工具轮跳过总工具次数限制和中间文本去重提示。轮次计数仍正常进行（`remember` 不计入轮数、`generate_anima_image` 计入），模型停止调用终端工具后自然结束。终端工具轮仍然遵循画图模式门控：`off` 模式下 `generate_anima_image` 不在 `TOOL_REGISTRY` 中不会被注入；`auto` 模式无画图关键词时已被从 `tool_schemas` 过滤，终端工具选择时同样不可见。
- **画图任务编号防伪**：当用户消息含画图关键词（`画`/`draw`/`改图`/`重画`/`来一张`/`整一张`）时，force 模式下预先注入 system 消息引导模型调用工具。拦截检查延迟到整轮结束后（工具仍可用但模型未调用时），用原始模型输出检查是否含伪造任务编号（`任务编号`/`单号` 后跟 6 位字母数字、`draw-XXXXXX` 格式、`generate_anima_image` 工具名回显，或历史编号占位符回显）。若 force 模式检测到伪造编号且无 `generate_anima_image` tool_calls，整条消息拦截不发送，注入系统提示强制重试（最多 1 次），且只有此重试分支记录 `[伪造任务编号]` warning。画图请求在尚未调用画图工具时会先缓冲流式文本，非 force 模式不重试但会发送清理后的文本，避免占位符或伪编号直出。历史任务号隐藏占位符常量保持为 `[请调用 generate_anima_image 画图工具获取编号]`，不要改动；`sanitize_draw_reply_text()` 负责清理新旧占位符回显，并仅在 `allow_task_ids=False` 时清除伪任务编号，避免误清除工具返回的真实编号。
- **画图工具 thinking 检测兜底**：在流式和非流式两条路径中，当模型返回无 tool_calls 时，检查 `reasoning_content` 中是否包含 `generate_anima_image`。若包含（说明模型"想画"但没实际调用工具），且当前 draw_mode 不是 `off`，则注入 system 提示要求模型通过 tool_calls 调用画图工具，设置 `_force_tools_next = True` 强制下一轮保留工具定义，触发重试（仅一次，`_thinking_check_done` 防重复）。覆盖 `auto`/`on`/`force` 三个挡位。漫画模式下不强制重试，由模型自主决定是否画图。
- **流式工具名双拼修复**：`_stream_once()` 中流式拼接 `function.name` 时，某些 provider 可能重复发送 name chunk 导致双拼（如 `generate_anima_imagegenerate_anima_image`）。拼接后检测 `name[:half] == name[half:]` 自动去重。
- **工具调用参数校验**：`_stream_once()` 和 `_complete_once()` 返回 `tool_calls` 后，逐个校验 `arguments` 是否为合法 JSON。解析失败则丢弃该 tool_call 并记 warning 日志；全部丢弃时注入系统提示要求模型重新调用并 `continue` 重试。防止流式截断导致畸形 JSON 进入下一轮请求引发 provider 502。
- **缓存命中采集**：`_stream_iter_openai()` 请求体中添加 `stream_options: {"include_usage": True}`，从流式响应的最后一个 chunk 中提取 `usage` 信息（`prompt_tokens`、`cached_tokens`、`completion_tokens`），存储到 `_last_stream_usage` 供 matcher 读取。
- **异常处理**：`stream_response()` 内部循环异常时始终 return（不再因多 key 而 continue 死循环），由 matcher 外层重试逻辑统一处理。
- **并发控制**：`_pending_merge_input` 字段存储工具调用期间收到的新触发输入。工具调用状态必须按 `chat_key` 判断（`TextGenerator.is_tool_calling(chat_key)`），不能用全局 `_tool_calling` 布尔值跨群判断；`_current_chat_key` / `_current_trigger_userid` 使用任务本地上下文，避免并发群请求互相覆盖工具运行上下文。旧请求处于工具调用中时，新触发消息不能与旧 active input 合并，也不能删除旧请求已记录的 user；pending input 必须保留本次新消息的 `trigger_userid`、`sender`、`images`、`event` 等元数据，避免递归处理时错用旧请求用户。
- **Profile 切换**：`switch_profile()` 方法运行时重新初始化连接参数（api_keys、base_url、proxy、multimodal、extra_prompt 等），无需重启。
- **请求级 Profile 快照**：`matcher.py` 调用 `stream_response()` 前必须基于 `chat.get_active_profile()` 显式传入当前群 profile 快照；`chat_summary.py` 的工具摘要、上下文摘要和用户印象后台任务也必须快照当前会话 profile 并传给 `get_response()`。`stream_response()` / `get_response()` 开始时固定本轮请求的 `model`、`base_url`、`proxy`、`api_key`、`multimodal`、`enable_stream` 等配置，后续 `_completion_kwargs()`、HTTP 请求和流式请求都使用这份快照，避免其他群在并发请求中调用 `apply_profile()` / `switch_profile()` 后污染当前请求，尤其避免工具调用返回后的下一轮续写打到错误 provider。
- **多模态剥离**：`_completion_kwargs()` 中，若当前 profile 的 `multimodal=False`，自动将消息中的 `image_url` 内容转为文本占位符，避免不支持图片的 API 报错。
- **流式超时**：`_stream_iter_openai()` 中，httpx 的 `read` timeout 在每个 chunk 到达时重置；总体响应时间硬上限 5 分钟（`MAX_TOTAL_SECONDS`），超出时中断流式输出。
- **model_mini 回退**：`_completion_kwargs()` 中，当 `type='summarize'` 或 `type='impression'` 时使用 `model_mini`；若 `model_mini` 为空则自动回退到 `model`，避免摘要生成因配置缺失而失败。`kwargs["model"]` 赋值为已计算的 `model_name` 而非直接读取 config。
- **请求参数安全**：`_request_openai_compatible()` 和 `_stream_iter_openai()` 入口处对 `kwargs` 做浅拷贝（`kwargs = dict(kwargs)`），避免 `pop` 操作污染调用方的原始字典。
- **可变默认参数**：`_normalize_prompt()`、`stream_response()`、`get_response()` 等公开/半公开方法不得使用 `{}` 或 `[]` 作为默认参数；使用 `None` 并在函数内创建新对象，避免长进程单例共享状态。

## `llm_tools.py`

- 聚合所有原生工具定义。
- 调用 `llm_tool_plugins/` 下的各工具模块。
- 工具输出（如图片 URL）暂存，供 matcher 在文本流结束后统一发送。
- `get_tool_schemas()` 根据 `chat_key` 的 `draw_model` 动态选择注入对应模型的 schema（base/turbo2/turbo/aesthetic），函数名统一为 `generate_anima_image`。漫画模式下强制使用 turbo schema。

## `llm_tool_plugins/`

每个工具独立为一个文件，当前内置工具：

- **`pixiv_search.py`**：Pixiv 图片搜索。多关键词无结果时自动取首个关键词重试；工具返回不含图片 URL，仅告知模型图片会自动发送。
- **`fetch_url.py`**：轻量 HTTP 文本抓取。
- **`browse_url.py`**：Playwright 渲染页面文本抓取。schema 描述中明确标注为 fallback：`ONLY use when fetch_url fails or the page requires JavaScript rendering`，引导 LLM 优先使用轻量抓取。
- **`tavily_search.py`**：Tavily 网页搜索（主搜索工具）。启动时通过 `GET /usage` 检查所有配置的 key 额度，选用剩余最多的 key（`TAVILY_API_KEY` 支持多 key 列表）。`include_answer` 设为 `advanced`，`search_depth` 设为 `advanced`（更深度搜索），`max_results` 固定 20。结果格式化后返回，单条 content 截断 300 字符，总长受 `WEB_FETCH_MAX_CHARS` 限制；超预算时逐条降级为 title+url，超出部分省略。调用失败（401/429/432/433 或网络异常）时在内存中标记 `_tavily_disabled` 并动态注册 `bocha_search` 作为 fallback。
- **`tavily_extract.py`**：Tavily 网页内容爬取工具。当 `fetch_url` / `browse_url` 等浏览器端工具因反爬、JS 渲染等原因无法访问目标页面时，通过 Tavily 服务端爬取页面内容，返回干净的 Markdown 或纯文本。共享 `tavily_search` 的 API key，仅在 Tavily 可用时自动加载。支持参数：`urls`（必填，最多20个）、`query`（用于内容块重排序）、`extract_depth`（默认 `advanced`）、`format`（`markdown`/`text`）、`include_images`。
- **`bocha_search.py`**：博查网页搜索（fallback）。仅在 Tavily 不可用（未配置 key 或运行时被标记禁用）时才通过 `should_load` 注册。当 LLM 对问题不确定、不了解或涉及实时信息时应主动搜索验证，不猜测不确定的事实。单次搜索结果数强制为 10-20 条，默认请求 20 条。
- **`memory.py`**：记忆工具，对用户透明。支持两种 scope：
  - `group`：群记忆，所有人共享，注入到 `[群记忆]`。
  - `user`：用户记忆，仅对该用户有效，注入到 `[你的记忆]`。
  - 记忆与人格关联，每个人格有独立记忆空间。
  - 支持 `save`、`delete` 和 `consolidate` 三种 action：
    - `save`：保存单条记忆（key + value）。
    - `delete`：删除单条记忆（key）。
    - `consolidate`：批量整理记忆，通过 `operations` 参数传入操作列表（每项含 `op: "save"|"delete"` + `key` + 可选 `value`），按顺序执行，支持一次调用完成多条记忆的增删改。
  - LLM 应积极主动保存重要信息，保存时透明返回 `已记住：「key」=「value」`。
  - 接近上限（80%）时不阻断，仅在 system2 中注入整理提醒；整理功能随时可用，不受阈值限制。
- **`anima_generate.py`**：ComfyUI Anima 画图工具。
  - 通过 `rg draw [force/on/auto/off]` 动态注册/卸载，默认 `auto`。
  - 支持四种画图模型（`rg draw <turbo|aesthetic|turbo2|base>`，可简写 `t|a|t2|b`），每群独立，默认 `turbo`（turbo_v1）：
    - `turbo`/`t` → turbo_v1（`/anima/generate_turbo_v1`，10步/CFG 1，约15秒，schema_turbo_v1，knowledge_new_models）
    - `aesthetic`/`a` → aesthetic_v1（`/anima/generate_aesthetic_v1`，35步/CFG 4，约60秒，schema_aesthetic_v1，knowledge_new_models 共享）
    - `turbo2`/`t2` → turbo0.2 原有 turbo（`/anima/generate_turbo`，8步/CFG 1，约15秒，schema_turbo，knowledge_turbo）
    - `base`/`b` → 普通工作流（`/anima/generate`，35步/CFG 5，约60秒，schema，knowledge）
  - `MODEL_CONFIG` 字典统一管理四种模型的端点、schema/knowledge 路径、默认 steps/cfg、预估耗时、是否需要 `tags↔nltags` 兼容交换（仅 turbo2 需要）。`MODEL_ALIASES` 处理简写。
  - 旧指令 `rg turbo on/off` 保留为兼容别名（on→turbo，off→base），提示用户改用 `rg draw <model>`。
  - 支持漫画模式（`rg manga on/off/[画风描述]`），每群独立，默认关闭。漫画模式下 bot 会积极主动画图增强角色扮演沉浸感，无视 draw_model 固定使用 turbo（turbo_v1）工作流，不需要任务编号和 ETA，不保存 prompt 到 DB。
  - `fetch_schema_and_knowledge_sync()` 从 ComfyUI 服务一次性拉取全部四种模型的 schema 与 knowledge（`/anima/schema` + `/anima/schema_turbo` + `/anima/schema_turbo_v1` + `/anima/schema_aesthetic_v1` + `/anima/knowledge` + `/anima/knowledge_turbo` + `/anima/knowledge_new_models`，其中 turbo 与 aesthetic 共享 knowledge_new_models）。缓存时强制将函数名统一为 `generate_anima_image`。
  - `get_schema(model)` / `get_knowledge(model)`：按 model 名（base/turbo2/turbo/aesthetic）返回对应版本。
  - `get_draw_model(chat_key)` / `set_draw_model(chat_key, model)`：管理每群画图模型，持久化在 `ChatData.draw_model`。`resolve_model_alias(name)` 将指令简写解析为内部 model 名。
  - `get_manga_mode(chat_key)` / `set_manga_mode(chat_key, enabled)`：管理每群漫画状态，持久化在 `ChatData.manga_mode`。
  - `get_manga_style(chat_key)` / `set_manga_style(chat_key, style)`：管理每群自定义画风描述，持久化在 `ChatData.manga_style`。
  - `MANGA_RULES`：漫画模式专用规则常量，开启时追加到 turbo（turbo_v1）knowledge 末尾。包含具体场景列举、频率指导和防复读规则。
  - `MANGA_UNLOCK_RULES`：漫画模式解锁内容限制规则常量，当内容限制解锁时（per-group `unlock_content_limit` 为 True，或回退到 `config.UNLOCK_CONTENT_LIMIT`）在 manga 直接画图路径和 chat_prompt 漫画模式中追加。NSFW 相关画图规则始终包含在 knowledge cache 中（由 system prompt 的解锁规则控制是否激活）；自然语言 tags 描述规则也始终包含（修复了之前被错误放在解锁条件内的 bug）。
  - `mark_manga_drawn(chat_key)`：标记指定会话刚完成一次画图，更新 `_manga_last_draw_time` 时间戳和 `_manga_rounds_since_draw` 计数（重置为0）。
  - `increment_manga_round(chat_key)`：增加指定会话的漫画模式对话轮数计数。
  - `should_inject_manga_idle(chat_key)`：检查漫画模式下是否超过配置的分钟数或轮数未画图，用于决定是否触发自动画图。
  - `manga_idle_draw(chat_key, chat, config, bot)`：超过配置时间或轮数未画图时，用 mini 模型根据上下文设计场景并调用画图工具，不输出文字到聊天。可画 bot 神态、用户请求内容或互动场景，根据上下文决定。构建精简 prompt（人设+记忆+最近对话历史，去掉图片），使用 prompt 指令引导调用画图工具（不使用 tool_choice，兼容 thinking 模型）。每次调用保存 JSON 日志到 `data/naturel_gpt/logs/{chat_key}.manga_draw.json`。prompt 顺序：漫画技能 → 画图指令 → 群记忆 → 当前时间 → 最近对话。
  - `run()` 根据 `draw_model` 决定调用哪个端点（由 `MODEL_CONFIG[model]["endpoint"]` 指定）。turbo2 模式下做 `tags ↔ nltags` 字段映射以保持数据库兼容性（`needs_tag_swap` 控制）；其余模型直接透传 args。漫画模式下强制使用 turbo（turbo_v1）端点（无视 `draw_model`），返回简化内容（无任务编号/ETA），不保存 prompt 到 DB。`_do_generate` 接收 `model` 参数选择端点。
  - 工具调用后即时返回第一人称作画描述文本，并告知用户预计时间，后台 `asyncio.create_task` 提交生成任务。漫画模式下直接返回「图片正在生成中，会自动发送」。
  - 生成结果进入 `_pending_results` 队列，由 matcher 在文本回复发送完毕后消费并发送图片。
  - `_bg_tasks: set` 保留 Task 引用防止 gc 取消；httpx timeout 300s。
  - schema 与 knowledge 处理：schema description 精简为一句话引导；base 模式 knowledge 按文件类型分别压缩（`anima_expert.md` 去掉默认参数/长宽比段落、`artist_list.md` 只保留 @artist 列表、`prompt_examples.md` 裁剪到 3 个代表性场景）+ 硬编码核心规则；turbo2/turbo/aesthetic 模式 knowledge 直接使用完整内容 + 对应调用规则。turbo 与 aesthetic 共享同一份 knowledge_new_models 内容。
  - **画图任务编号**：调用成功后返回随机 6 位字母数字任务编号（格式 `draw-XXXXXX`）和预计生成时间（精确到秒）。schema 和 knowledge 中明确禁止模型编造虚假任务编号，只有工具返回的编号才算成功调用。漫画模式下不返回任务编号。
  - **队列限制**：当 ComfyUI 队列长度大于 5 时拒绝生成并提示用户稍后重试。预计生成时间公式为：`当前图片预计生成时间 + 队列中图片数 * 90秒 - 30秒`。
  - **发图拼接**：图片生成后通过 OneBot 发图时，将任务编号拼接在图片消息前一起发送。漫画模式下不拼接任务编号。
  - **提示词持久化**：工具调用成功后自动将提示词保存到 `draw.db` 数据库，供 `rg draw-XXXXXX` 查询。漫画模式下不保存。
  - **空参数校验**：`run()` 入口校验 `args` 中至少包含一个有效字段（`character`/`appearance`/`nltags`/`artist`/`series`/`tags`/`style`/`environment`），全空时直接返回错误提示让模型重新填写，不保存 DB、不提交生成任务。
- **`nas_game_list.py`**：NAS 游戏目录查询。`get_tool_schemas()` 中根据 `NAS_GAME_WHITELIST_GROUPS` 白名单过滤 schema，非白名单群连工具定义都不可见（LLM 无法"看到"此工具），`run()` 中也保留运行时二次校验。`_check_whitelist()` 接受可选 `chat_key` 参数，优先使用传入值而非 `TextGenerator._current_chat_key`。扫描深度上限 `_BRAND_SCAN_MAX_DEPTH=5`，始终递归子目录（不受是否有文件影响），确保多层嵌套的游戏能被扫描到。

新工具遵循同一模式：定义 schema + 提供 `run(args, config)` 入口。

## `chat.py` 及其子模块

`Chat` 类通过 Mixin 模式分拆为多个子模块，便于维护：

- **`chat.py`**：核心类定义、属性、基本操作（人格切换、Profile 管理、缓冲区管理等）。
- **`chat_memory.py`**：记忆管理（`_get_chat_memory`、`_get_user_memory`、`set_memory`）。
- **`chat_history.py`**：对话历史管理（`update_chat_history_row`、`save_tool_messages`、`remove_last_prompt_user_message`、`cleanup_after_bad_request`、`_trim_prompt_messages_without_summary`、`_cleanup_orphan_tool_messages`、`_cleanup_orphan_history_messages`、`_count_rounds`、`update_chat_history_row_for_user`）。`save_tool_messages` 中校验工具名：修复 provider 重复发送 name chunk 导致的双拼函数名（如 `generate_anima_imagegenerate_anima_image` → `generate_anima_image`），剔除不在 `TOOL_REGISTRY` 中的无效工具调用。工具结果写入时必须按有效 `tool_call_id` 对齐，只保存对应已保留 assistant tool_call 的 tool result，并用规范化后的工具名写入 `tool_name`。`cleanup_after_bad_request` 只清理超过 `MULTIMODAL_IMAGE_FRESH_MINUTES` 的图片，保留新鲜图片，并清理孤立 assistant/tool。`save_tool_messages` 必须 deep copy 嵌套 tool_call 后再规范化，不能修改传入的 `msg`，并返回本次写入的 assistant tool-call 消息供工具摘要显式绑定。`update_chat_history_row_for_user` 将用户名写入 `ImpressionData.nickname`。`update_chat_history_row` 为触发 user 写入 `ChatMessageData.user_id`；当 `CONTEXT_SUMMARY_ENABLED=True` 时，`require_summary=False` 不得触发滑窗裁剪，避免摘要任务启动前丢失旧轮次。
- **`chat_prompt.py`**：prompt 构造（`get_chat_prompt_template`、`_build_openai_history_messages`、`_trim_messages_to_request_budget`、`_message_text_for_prompt`、`_message_content_for_prompt`、`_format_prompt_message_for_summary`、`_apply_image_gating`）。
  - **历史上下文单号隐藏**：`_message_text_for_prompt` 对所有 assistant 历史消息自动替换任务编号（匹配 `任务编号`/`单号` + 可选分隔符 + 可选 markdown 加粗 + 可选 `draw-` 前缀 + 6 位字母数字，以及无前缀的 `draw-XXXXXX` 格式）为 `[编号已隐藏，请调用 generate_anima_image 画图工具]`，防止模型从历史中引用伪造编号。
  - **图片门控**：`_apply_image_gating` 方法独立处理图片注入逻辑。普通图片分支只处理真实 user 消息（触发消息 + 历史用户消息），context_only 图片只能走专用分支，避免重复注入；user 图片受 `_image_is_fresh` 有效期检查，context_only 图片不受有效期限制、始终收集并注入到 context_only 消息本身，图片关键词仅控制去重行为；最后执行 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 全局数量限制。context_only 图片注入时自动去重（基于已存在的 image_url），避免同一 URL 重复出现在 content 列表中。
  - **个人印象注入（per-turn）**：印象不再在 System 4 集中列出，改为跟随触发者注入到历史消息中。`update_chat_history_row` 在触发 user 消息前按需插入印象 system（`is_impression=True`），`_message_text_for_prompt` 将其格式化为 `[用户印象: 昵称]\n印象正文\n[你的记忆]\n1. key: value`。用户记忆现在与印象绑定在同一 system 消息中，不再单独注入 System 3。同一用户印象在整个上下文中只注入一次（按 `impression_user_id` 去重），绑定到首次触发轮次。`_build_openai_history_messages` 的 `source_messages` 过滤、`msg_role` 判定（印象→system）、轮次选取 `start_idx`（不跳过保留轮的印象 system）、token 裁剪 `_oldest_removable_round_indices`（回溯纳入 user 前导印象、遇到下一轮印象则停止收集）、`_cleanup_orphan_history_messages`（印象绑定紧跟的 user，孤立印象丢弃）均已适配。System 4 仅保留压缩上下文摘要。最终上下文结构为 `[历史轮: system印象? user assistant system工具摘要?]...[system 触发者印象?][system context_only非触发缓冲][user 触发句]`。
- **`chat_summary.py`**：摘要/印象生成（`generate_tool_call_summary`、`_compress_prompt_messages_if_needed`、`_save_summary_log`、`_save_error_log`）。

核心功能说明：

- 定义 `Chat` 领域对象，围绕持久化 `ChatData` 运作。
- 负责人格切换、记忆、聊天历史、摘要、用户印象、prompt 构造、发送与生成时间戳。
- **实例变量**：`_compress_task`、`_tool_summary_task`、`_pending_overflow_text`、`_pending_overflow_user_ids`、`_pending_overflow_item_ids`、`_compressing_overflow_item_ids`、`_compress_failure_time`、`_last_interrupted_response`、`_persona_cache`、`_persona_cache_time` 均为实例变量，每个 Chat 实例独立，避免类变量共享状态。
- **Profile 管理**：`get_active_profile()` / `set_active_profile()` 管理每群的 OpenAI profile；`apply_profile()` 在消息到达时自动切换 TextGenerator。
- **`get_chat_prompt_template()`**：返回 OpenAI 风格的对话消息列表。
  - 系统消息拆分为 4 条：
    - system 1 = 角色设定 + 响应规则 + 工具基础规则（**极稳定前缀，完全不变，最大化缓存命中**）
    - system 2 = 画图知识（仅 force/on/auto+关键词时注入）+ 模型专用提示词（`extra_prompt`，仅非空时追加）—— 条件追加，变化不影响 system 1 缓存
    - system 3 = 记忆（群+用户）+ 记忆提醒 + 日期
    - system 4 = 压缩上下文摘要（会话级变化，仅在摘要更新时变动；个人印象不再放在此处，改为 per-turn 注入到历史消息中）
  - 若当前会话启用了 Anima 画图，在 system 2 中注入 `[你的绘画技能]` knowledge（从 ComfyUI 拉取，经压缩处理：精简提示词规范、保留画师列表、裁剪示例到 3 个代表性场景、压缩调用规则）。**auto 模式惯性注入**：当前消息有画图关键词，或过去 `CONTEXT_WINDOW_SIZE` 轮中有画图活动（`generate_anima_image` 工具调用或画图关键词触发）时保持注入；无近期活动时移除以节省 token。Turbo 模式下注入 turbo 版本的 knowledge（标签为 `[你的 Turbo 绘画技能]`），使用英文自然语言描述 tags。漫画模式下注入 turbo knowledge + `MANGA_RULES`（标签为 `[你的漫画技能]`），若设置了自定义画风则追加到 knowledge 前面（标签为 `## 自定义画风（必须遵循）`）。
  - 工具基础规则新增约束：调用工具时先输出 tool_calls，等系统返回结果后再在回复中引用编号，禁止在 tool_calls 之前就在 content 中写任务编号。
  - 系统提示要求模型像真实群聊成员一样自然说话，最多 3 段，不用 Markdown，可用双换行分段。
- **结构化历史管理**：
  - `prompt_messages`：核心对话历史，仅记录触发了回复的用户、Bot、`context_only` 上下文和工具消息。触发 user 消息必须保存 `user_id`（QQ 号）供印象生成定位用户；图片作为消息的 `images` 字段存储，不再有独立的图片历史表。
  - `_recent_context_buffers`：`matcher.py` 入口层非触发消息临时缓冲区，按 `chat_key` 保存最近非触发文本和图片；窗口大小独立按 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)` 计算。触发时作为独立的 `context_only` system 消息注入 `prompt_messages`（置于历史之后、触发消息之前）。`Chat._context_buffer` 仅作为兼容旧路径的本轮 flush 来源，不作为新的主写入路径。`context_only` 消息使用 `role="system"`，带有 `[群聊上下文-非触发消息]` 前缀，不进入持久化存储，不计入对话轮数。
  - `chat_impressions`：用户印象字典，按用户ID存储对话印象，用于个性化回复。`ImpressionData` 包含 `nickname` 字段存储群名片。
  - `context_summary`：压缩上下文摘要，当历史过长时异步生成，用于维持长对话连贯性。
  - `tool_call_summary`：工具调用摘要（模式3），存储在对应的 assistant 消息中，每次工具调用后异步生成，失败时 fallback 为截断原文。
  - `_last_interrupted_response`：被中断的流式回复内容，打断时保存，下次请求时注入上下文。
  - `_compress_failure_time`：摘要压缩失败的时间戳，用于 120 秒冷却期内跳过压缩触发。
- **失败回复保存**：当 LLM 请求失败（`success=False`）时，如果有已生成的回复（可能包含工具调用结果），也会保存到 `prompt_messages`，避免 bot 回复丢失导致上下文断裂。
- **上下文窗口**：`CONTEXT_WINDOW_SIZE` 按实际对话轮数计算（只统计非 `context_only` 的 `role="user"`，即真实触发回复的用户消息）。`CONTEXT_WINDOW_SIZE` 是摘要成功后的目标窗口；请求构造和摘要前保留使用独立对话缓冲窗口 `CONTEXT_WINDOW_SIZE * (1 + CONTEXT_COMPRESS_THRESHOLD_RATIO)`（实现上为 `target + int(target * ratio)`）。滑窗截断、溢出检测、摘要裁剪均按轮数判断，并删除完整旧轮次对应的 user/assistant/tool 消息段。`context_only` 不计入轮数且裁剪时保留。
- **孤立历史清理与工具消息截断保护**：
  - 历史中不允许存在没有真实 user 轮次承接的孤立 assistant/tool；`context_only` 可以保留，但不会让后面的 assistant 变成合法轮次。
  - `_cleanup_orphan_tool_messages()` 清理孤立 tool 消息（无对应 assistant 的 tool_calls）。
  - `_cleanup_orphan_history_messages()` 先清理孤立 tool，再按真实轮次状态机清理：非 `context_only` user 开启一轮；assistant/tool 只有处在打开轮次内才合法；普通 assistant 关闭该轮；assistant+tool_calls 和对应 tool 结果保持该轮打开直到最终 assistant。不能只用“历史中是否曾出现过 user”的 `seen_user` 判断，否则中间 user 被裁掉后会留下孤立 assistant。裁剪、摘要成功、400 清理、prompt 构造前、持久化加载/保存时都应使用同等逻辑。
  - `_build_openai_history_messages()` 按轮选取最近窗口时，如果截断点落在 user 后的 assistant/tool 段内，必须跳到下一个真实 user 或 `context_only`，避免构造 prompt 时出现孤立 assistant。
  - `_trim_messages_to_request_budget()` 和 `_build_openai_history_messages()` 的 token 预算裁剪必须按完整旧轮次删除：从最旧的非触发 user（含其前导印象 system）到下一条真实 user 前的 assistant/tool/工具摘要 system 一起删除，遇到下一轮的印象 system 则停止收集（印象属于下一轮），保护系统消息、`context_only` 和触发消息（最后一条 user 消息），避免逐条删除制造孤立 assistant 或孤立印象。
- **Token 截断**：使用准确的token计算方法（包括图片token估算），智能截断时优先保留包含工具结果、摘要、记忆等重要内容。
- **图片有效期**：通过 `MULTIMODAL_IMAGE_FRESH_MINUTES` 配置项控制图片上下文的有效时间（默认120分钟）。图片从 `prompt_messages` 检索，受图片门控约束。
- **图片位置标记**：图片在文本中自动标记为 `[图片1]`、`[图片2]`，保持图片与文本的对应关系。
- **用户印象压缩**：用户消息仍累积到 `chat_history`（上限 `USER_MEMORY_SUMMARY_THRESHOLD * 2`，保留用于备份），但印象生成依据已改为**本次溢出部分中该用户的 user 消息**（从 `new_overflow_messages` 提取该 `user_id` 的对话行，不再用 `chat_history[-20:]`）。由摘要任务统一生成印象（异步），**仅对本次溢出中实际产生互动的用户生成印象**（通过 `ChatMessageData.user_id` 和 `_pending_overflow_user_ids` 跟踪），不更新所有有历史的用户。旧数据若没有 `user_id`，可用唯一匹配的 `ImpressionData.nickname` 反查。印象 prompt 中包含用户的群昵称（`ImpressionData.nickname`），软限制 300 字，硬截断 600 字。摘要裁剪删除溢出整轮（含其前导印象 system）后，下次该用户触发时上下文已无其印象，自然注入更新后的新印象。
- **记忆管理**：超出最大长度时不再自动删除，仅记录警告，由 LLM 通过记忆工具的整理模式主动精简。
  - 群记忆 (`chat_memory`)：所有人共享，注入到 `[群记忆]`。
  - 用户记忆 (`user_memories`)：按用户ID存储，注入到 `[你的记忆]`，与用户印象绑定在同一 system 消息中。
  - 记忆与人格关联，每个人格有独立记忆空间。
  - 记忆接近上限时在 impression system 中注入 `[记忆提醒]`，建议 LLM 调用 consolidate 整理。
  - `rg reset` 不清除记忆和印象，记忆由 `rg mem clear <scope>` 专门管理。
- **摘要压缩**：异步执行（`asyncio.create_task`），不阻塞用户消息响应。
  - `_compress_prompt_messages_if_needed()` 只在 bot 回复后的 `require_summary=True` 路径调用；触发 user 入历史、`context_only` 注入、失败回复保存等 `require_summary=False` 路径不得在摘要开启时提前裁剪。
  - 独立对话缓冲窗口 = `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO)`。`_build_openai_history_messages()` 构造请求时按此缓冲窗口取最近真实轮次，而不是只取 `CONTEXT_WINDOW_SIZE`，避免“摘要前保留了但请求里看不到”的上下文断层。
  - 溢出轮数 = 当前真实 user 轮数 - `CONTEXT_WINDOW_SIZE`。只有 `overflow_rounds > CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO` 时才启动摘要；否则继续保留完整 `prompt_messages` 并允许缓冲窗口内历史进入请求。
  - 溢出达阈值时保存溢出文本到 `_pending_overflow_text`，启动异步摘要任务，**先生成摘要，成功后再实际裁剪**（摘要完成前保留完整上下文供模型对话）。
  - 摘要任务运行期间，新消息正常加入 `prompt_messages`；通过 `_compressing_overflow_item_ids` 和 `_pending_overflow_item_ids` 去重，避免同一批溢出消息被重复累积或重复总结。当前摘要任务成功时只能清除自己的 `_compressing_overflow_item_ids`，不得清空运行期间新积累的 `_pending_overflow_text` / `_pending_overflow_item_ids`；失败、取消或异常恢复时也要与已有 pending 合并，并只保留仍存在于 `prompt_messages` 的消息 id。
  - 摘要完成后通过 `id()` 匹配删除已总结的完整旧消息段（保留 `context_only`；印象 system 非 context_only，随其绑定轮次一并删除），不依赖 index 偏移；`cut_index` 回溯排除保留轮 user 的前导印象 system，避免把保留轮印象误纳入溢出。删除后必须清理孤立 assistant/tool。
  - 摘要触发受 `CONTEXT_COMPRESS_THRESHOLD_RATIO` 控制（默认 0.5，即溢出超过窗口的 50% 才触发；当前配置可设为 2.0 表示额外允许 2 倍窗口溢出）。
  - 摘要字数限制从 `max_summary_tokens` 配置动态读取（中文约 1 token ≈ 1 字），不在 prompt 中硬编码。硬截断在 2 倍软限制处。
  - **失败冷却**：摘要生成失败后设置 `_compress_failure_time`，120 秒内不再触发压缩，避免持续发送无效请求。溢出消息保留在 `prompt_messages` 中，冷却期过后下次触发时重新捕获。
  - **溢出文本恢复**：失败时将 `_pending_overflow_text` 恢复，下次触发时重试相同文本。失败恢复时合并而非覆盖 `_pending_overflow_user_ids`，避免丢失新用户 ID。
  - **异常保底**：摘要 task 必须有 `done_callback` 或等价外层 `finally`，无论摘要、日志、印象生成或持久化在哪个阶段异常退出，都必须清空 `_compressing_overflow_item_ids`；如果对应溢出消息仍在 `prompt_messages` 中，则恢复 `_pending_overflow_text`、`_pending_overflow_item_ids` 和 `_pending_overflow_user_ids` 以便后续重试。
  - 摘要和印象生成完毕后调用 `save_to_file()` 持久化。摘要日志（`.summary.json`）包含上下文摘要、工具摘要和用户印象。
- **工具调用摘要（模式3）**：
  - 仅对搜索类工具（`tavily_search`、`bocha_search`、`fetch_url`、`browse_url`）生成 LLM 摘要，标记为 `[搜索工具摘要]`。
  - 其他工具（`pixiv_search`、`remember` 等）保留原始结果，标记为 `[调用结果]`。
  - **绘画工具不显式记录**：`generate_anima_image` 的工具调用结果不注入历史上下文，避免 LLM 看到历史中的调用记录后产生"已经画过了"的错觉。
  - 工具摘要必须显式绑定 `save_tool_messages()` 返回的本次 assistant tool-call 消息，异步任务不得扫描“最后一个带 tool_calls 的 assistant”，否则并发请求会把旧摘要挂到新工具调用上。
  - 摘要以 system 消息**紧跟在对应 assistant 消息后面**注入（不附加到 assistant 的 content 中），保证摘要不漂移，避免模型模仿工具结果格式。
  - 搜索摘要的 LLM prompt 中包含触发搜索的原始问题。
  - 构建 `normal_messages` 后，检查开头是否有孤立的工具摘要 system 消息（工具调用已超出窗口但摘要残留），有则丢弃。

## `matcher.py`

- 主 OneBot v11 集成入口。
- 通过 `utils.gen_chat_payload()` 从消息片段提取文本、图片 URL（带位置标记）。
- 允许纯图片消息（多模态开启时）。
- 更新历史记录时补充图片元数据；`CHAT_ENABLE_RECORD_ORTHER` 分支同样记录图片，防止非 @ 消息图片丢失。
- 调用 `TextGenerator.stream_response()` 获取模型输出和工具消息，按 `\n\n` 分段发送。
- **指令返回的 `no_img` 标记**：当指令返回包含 `no_img: True` 时，即使开启了 `ENABLE_COMMAND_TO_IMG`，也强制以纯文本形式发送（用于 `rg draw-XXXXXX` 查询结果）。
- **画图占位符发送兜底**：`send_segment()`、成功响应保存、失败回复保存和中断部分回复保存都会调用 `sanitize_draw_reply_text(..., allow_task_ids=True)`，只移除历史编号占位符回显，不删除工具调用后返回的真实 `draw-XXXXXX` 任务编号。
- **Reply + At 修复**：OneBot v11 的 `_check_reply` 会删除 reply 段后的 at 段，导致 `event.to_me` 未被设置。handler 中检查 `event.original_message` 中是否有 @bot 的 at 段，有则补设 `event.to_me = True`。
- **Reply 上下文注入**：触发消息包含 reply 段时，从 `event.reply.message` 提取被回复消息的文本和图片，以 `[回复 xxx 的消息]` 前缀拼接到触发消息前面。图片插入到 `image_urls` 前面，原有 `[图片N]` 标记重新编号偏移。
- **自定义昵称优先**：`sender_name` 优先使用 `PersistentDataManager.get_custom_nickname()`，其次才是 API 获取的群名片。
- **个人印象注入（已重构）**：印象不再根据触发句中提到的所有角色列出，固定只注入触发者印象，由 `update_chat_history_row` 在触发消息前按需注入（见核心架构决策「个人印象注入」）。用户记忆现在与印象绑定在同一 system 消息中，不再单独注入 System 3。`mentioned_userids` 的 @段提取、昵称匹配与 `get_chat_prompt_template` 传参均已移除。
- **非触发上下文注入**：`do_msg_response()` 在锁内先独立计算本条消息的 `should_reply`。`should_reply=False` 时写入 `_recent_context_buffers` 并立即返回；`should_reply=True` 时在节流后、生成 prompt 前调用 `_flush_recent_context_buffer()`，把最近非触发文本和图片合并为 `[群聊上下文-非触发消息]` 的 `context_only` system 消息。
- **漫画模式无画图提醒**：漫画模式下，若超过 `MANGA_IDLE_MINUTES` 分钟或 `MANGA_IDLE_ROUNDS` 轮对话未画图（通过 `should_inject_manga_idle(chat_key)` 检查），在回复完成后异步触发 `manga_idle_draw()`，使用 mini 模型根据上下文设计场景并调用画图工具，不输出文字到聊天。每次回复后调用 `increment_manga_round(chat_key)` 递增轮数计数。**漫画+force 强制画图**：当 manga 模式开启且 draw_mode 为 `force` 时，若触发句含画图关键词但本轮未实际调用画图工具，直接触发 `manga_idle_draw()` 并调用 `mark_manga_drawn()` 重置空闲计数。
- **Profile 切换**：消息到达时，`chat.apply_profile()` 自动将 TextGenerator 切换到该群的 profile，确保每群使用各自的模型配置。
- **工具结果持久化**：`stream_response()` 返回的 `tool_messages` 通过 `chat.save_tool_messages()` 保存到历史。
- **图片 400 重试**：LLM 请求返回图片下载相关 400 错误（`Cannot download image`、`failed to download url data`、`` `text` is not set `` 等）时，自动清理历史图片并以无图模式重试，最多 2 次。非图片 400 错误不重试，直接进入失败处理。
- **空 assistant content 400 重试**：LLM 请求返回 "must not be empty" 错误（Moonshot 等 provider 拒绝空 content 的 assistant 消息）时，在 prompt 上直接填充 `"[无内容]"` 占位符后重试，最多 2 次。`_is_empty_content_error()` 检测此错误类型。
- **旧请求打断**：
  - 收到新消息时，若同群存在旧请求，先判断新消息 `should_reply`（唤醒词、禁用词、违禁词检查）。
  - `should_reply` 必须基于本次新消息独立计算，不能先与 `_chat_active_inputs` 旧触发输入合并；否则非触发群消息会被旧触发状态污染并丢失到 context buffer 之外。
  - 只有确定需要回复的消息才打断旧请求；不需要回复的消息在 `return` 前清理 `_chat_active_inputs`。
  - 若旧请求正处于工具调用阶段（`TextGenerator.instance.is_tool_calling(chat_key) == True`），不 cancel，不合并旧 active input，只将本次新触发输入原样放入 `_pending_merge_input`，并保留最新 `trigger_userid`、`sender`、`images`、`event`。
  - 旧请求完成后，检查 `_pending_merge_input`，如果有待合并的输入则递归调用 `do_msg_response()` 处理。
  - 打断时捕获 `CancelledError`，从 `raw_parts` 中取出已接收内容（剥离 `<think>` 标签），保存到 `chat.set_interrupted_response()`。
  - 下次请求时，通过 `chat.pop_interrupted_response()` 取出中断回复，与 context buffer 合并为一条 `context_only` system 消息注入。
- **工具图片发送**：`consume_tool_outputs(chat_key)` + 图片发送在 `stream_response` 返回后立即执行，位于所有 `return` 分支之前，确保工具图片不因 `success=False` 等错误被跳过，且只能消费当前会话的工具附件。
- **失败回复保存**：当 LLM 请求失败（`success=False`）时，如果有已生成的回复（可能包含工具调用结果），也会保存到 `prompt_messages`，避免 bot 回复丢失导致上下文断裂。
- **Token 超限处理**：检测到 token 超限错误时，自动清理历史至最后 5 条并提示用户。
- **唤醒词机制**：
  - 前缀唤醒和名称提及额外检查 `chat.preset_key`，当前激活角色名也作为唤醒词。
  - 名称提及检测使用 `any()` 替代旧 `random.choice()`，避免漏检。
  - 唤醒词仅在句首触发（`startswith`），句中或句尾出现时不无条件唤醒，走 `RANDOM_CHAT_PROBABILITY`。
- **请求日志**：触发时输出 `触发回复 | 会话: xxx | 预设: xxx | 原因: xxx | tokens: xxx + x图`；回复完成后输出 `回复完成 | 会话: xxx | prompt=X cached=X(xx%) completion=X total=X`（缓存数据来自 API 响应的 `usage.prompt_tokens_details.cached_tokens`）。
- **Think 标签实时拦截**：流式回调 `on_text_chunk` 中维护状态机，实时检测 `<think>` 和 `</think>` 标签，将思考内容提取到 `_extracted_reasoning` 而非输出到聊天。兜底 `_strip_think_tags` 在最终响应上做正则二次过滤。
- **并发控制**：`_chat_response_lock` 使用真正的 `asyncio.Lock()` 保护 `_chat_running_tasks` 和 `_chat_active_inputs` 的关键段，防止竞态条件。
- **do_msg_response 签名**：`event` 参数已显式传入，`loop_data` 默认值改为 `None`（函数内 `loop_data = loop_data or {}`），避免可变默认参数陷阱。
- 不再解析 `/#...#/` 旧工具调用格式。

## `image_cache.py`

- 异步图片下载 + 内存 LRU 缓存，将远程 URL 转为 `data:image/xxx;base64,...` 格式。
- 单图上限 10MB，总缓存上限 50MB。
- **MIME 检测**：先检查 `content-type` 是否以 `image/` 开头，非图片类型默认为 `image/jpeg`。
- **下载头**：请求时附加 `User-Agent` 浏览器标识；QQ 图片域名（`qpic.cn`、`qq.com`）额外附加 `Referer: https://im.qq.com/` 头，减少 403 拒绝。
- **下载失败处理**：`resolve_url` 下载失败时返回空字符串（不再回退到原始 URL），避免将不可访问的 URL 发送给 API 导致 400。`resolve_urls` 批量解析时自动去重（保持顺序）并过滤空结果。已知失败的 URL（`_known_bad_urls`）直接跳过，随 `purge_stale()` 清理。
- **QQ 多媒体域名**：`multimedia.nt.qq.com.cn` 等 URL 带有时效性 `rkey`，过期后 400 为预期行为，降级为 DEBUG 日志，不刷屏。

## `singleton.py`

- 单例模式实现，使用 `threading.Lock` + 双重检查锁定消除 TOCTOU 竞态。
- 每个子类通过 `cls._instance` 独立维护自己的单例实例。

## `chat_manager.py`

- 全局会话管理器，管理所有 Chat 实例的创建和查找。
- 使用显式导入（`from .config import config`），避免通配符导入污染命名空间。
- `_chat_dict` 为实例变量，在 `__init__` 中初始化，避免类变量共享状态。

## `utils.py`

- `gen_chat_payload()` 返回文本（带图片位置标记）、唤醒标志、图片 URL。
- `gen_chat_text()` 保持纯文本兼容。
- `_extract_message_text_and_images()` 提取消息文本和图片，图片在文本中自动标记为 `[图片1]`、`[图片2]`。
- `async_fetch()` 重构为条件创建 session，统一用 `async with` 管理，消除资源泄漏。
- `translate()` 改用 `httpx`，保留原始异常信息。
- 用户名解析可能调用 OneBot API，需容忍失败。

## `command_func.py`

- `CommandManager` 实现所有 `rg` 指令。
- `cmd.register()` 使用 `params: Optional[list] = None`，避免可变默认参数陷阱。
- `resolve_command()` 对未定义的选项（`-unknown`）跳过而非 KeyError；对 `-param` 末尾越界做边界检查。
- `execute()` 返回 `{'error': str(e)}` 而非原始异常对象，确保 JSON 序列化安全。
- `execute()` 特殊处理 `rg draw-XXXXXX` 格式，直接路由到查询函数。
- `rg` / `rg list`：重载动态人格并列出可用人格。
- `rg set <persona>`：将运行时 `config.PRESETS` 中的指定人格加入当前会话并切换。
- `rg draw [force/on/auto/off]`：Anima 画图开关。执行 health check → 拉取 schema/knowledge → 注册/卸载工具 → 维护内存级会话开关。
  - `force`：常驻工具 + 画图关键词时拦截虚假回复（缓冲模式，检测到伪造编号整条不发送并重试 1 次）
  - `on`：常驻工具，不拦截
  - `auto`：仅在用户消息含画图关键词时注入工具到请求中（默认）
  - `off`：关闭
  - 模式持久化存储在 `ChatData.draw_mode` 中，重启后保持
- `rg draw <turbo|aesthetic|turbo2|base>`（可简写 `t|a|t2|b`）：切换画图模型。`turbo`=turbo_v1 新加速模型，`aesthetic`=aesthetic_v1 新高质量模型，`turbo2`=turbo0.2 原 turbo 加速，`base`=普通工作流。无参数时显示当前模式与模型。模型持久化在 `ChatData.draw_model`，每群独立，默认 `turbo`（turbo_v1，兼容旧 `turbo_mode=True`）。切换时按需拉取对应 schema/knowledge。
- `rg draw <json字符串>`：根据 JSON 创建绘图任务，解析 JSON 到绘图提示词模板，允许空值，自动检查队列状态和生成时间预估。提示词保存到 `draw.db` 数据库。按当前 `draw_model` 选择端点和默认 steps/cfg；turbo2 模式做 tags↔nltags 兼容映射。
- `rg draw-XXXXXX`：查询绘图编号对应的提示词，以 JSON 格式输出。设置 `no_img` 标记强制以纯文本发送。
- `rg turbo [on|off]`：**已废弃兼容别名**。`on` → 切换到 turbo（turbo_v1）模型，`off` → 切换到 base 模型。提示用户改用 `rg draw <model>`。
- `rg manga [on|off|画风描述]`：切换漫画模式。无参数时显示当前状态和画风；`on` 开启漫画模式；`off` 关闭；其他参数作为自定义画风描述开启漫画模式。`rg manga clr` 清除自定义画风。漫画模式下 bot 会积极主动画图来增强角色扮演沉浸感，无视 draw_model 固定使用 turbo（turbo_v1）工作流，不需要任务编号和 ETA，不保存 prompt 到 DB。开启时覆盖原有的 `rg draw` 模式。状态每群独立，默认关闭，持久化在 `ChatData.manga_mode` 和 `ChatData.manga_style`。
- `rg nolimit [on|off]`：内容限制解锁开关。无参数时显示当前群解锁状态与全局默认值；`on` 解锁内容限制（LLM 配合处理 NSFW 内容请求）；`off` 锁定。状态每群独立，持久化在 `ChatData.unlock_content_limit`（`None` 时回退到配置文件 `UNLOCK_CONTENT_LIMIT` 默认值）。
- `rg model [profile_name]`：列出或切换 OpenAI 配置。无参数时列出所有 profile 及当前激活状态；有参数时切换到指定 profile 并持久化。切换为按群生效，每个群有独立的模型配置。
- `rg nn [昵称]`：设置/查看/清除自定义昵称。无参数时显示当前昵称；`rg nn 清除` 删除自定义昵称；否则设置为指定昵称（最长30字符）。昵称全局生效，优先于 API 获取的群名片。持久化在 `PersistentDataManager._custom_nicknames`。
- `rg mem`：查看当前人格的群记忆和用户记忆。
- `rg mem clear <scope>`：清除当前人格的记忆。`group`=群记忆，`user`=当前用户记忆，`all`=全部。
- `rg help`：显示指令帮助，包含当前会话的工具状态和已注册工具列表。
- `rg reset` 只清除上下文（prompt_messages/context_summary），保留群记忆、用户记忆和用户印象。印象数据和自定义昵称不受 `rg reset` 影响。
- PresetHub 命令与旧扩展管理命令已移除。

## `persistent_data_manager.py`

- 定义持久化数据类；`ChatData` 包含 `chat_image_history` 用于多模态上下文（已弃用，保留字段兼容旧数据）。
- `ChatMessageData` 支持 `user`、`assistant`、`tool`、`system` 角色，其中 `tool` 角色包含 `tool_call_id` 和 `tool_name` 字段，`assistant` 角色可包含 `tool_calls` 和 `tool_call_summary` 字段。`user` 消息包含 `user_id` 字段用于印象生成定位 QQ 号；`context_only` 字段标记非触发上下文，加载时必须强制为 `role="system"`，不计入对话轮数。`is_impression`+`impression_user_id` 字段标记个人印象 system（由 `update_chat_history_row` 注入到触发 user 前，按 `impression_user_id` 去重，整个上下文只注入一次，加载时强制为 `role="system"`；不持久化，`PresetData._serializable` 过滤 `role=system`，重启后丢失由下次触发重新注入）。
- `ImpressionData` 包含 `user_id`、`nickname`（群名片）、`chat_history`（对话历史行，保留用于备份）、`chat_impression`（印象文本）。印象生成依据为溢出部分该用户的 user 消息，不再使用 `chat_history[-20:]`。
- `PresetData` 包含 `chat_memory`（群记忆）、`user_memories`（用户个人记忆）和 `chat_impressions`（用户印象字典），记忆与人格关联。
- `ChatData` 包含 `active_profile` 字段，存储每个群/私聊的 OpenAI profile 名，支持每群独立模型配置。
- `ChatData` 包含 `draw_mode` 字段，存储每个群的 Anima 画图模式（`force`/`on`/`auto`/`off`），默认 `auto`。
- `ChatData` 包含 `draw_model` 字段，存储每个群的画图模型（`turbo`/`aesthetic`/`turbo2`/`base`），默认 `turbo`（turbo_v1）。旧 `turbo_mode: bool` 字段在 `_init_from_dict` 时自动迁移：`True`→`turbo`、`False`→`base`，并删除旧字段。
- `ChatData` 包含 `manga_mode` 字段，存储每个群的漫画模式（`on`/`off`），默认 `off`。开启后覆盖 draw_model，固定使用 turbo（turbo_v1）工作流。
- `ChatData` 包含 `manga_style` 字段，存储每个群的漫画自定义画风描述，默认空。
- `ChatData` 包含 `unlock_content_limit` 字段（`Optional[bool]`），存储每个群的内容限制解锁开关。`None`（默认）时回退到配置文件 `UNLOCK_CONTENT_LIMIT` 全局默认值；`True` 解锁，`False` 锁定。通过 `rg nolimit on/off` 设置，通过 `chat.get_unlock_content_limit()` 读取。
- `PersistentDataManager` 包含 `_custom_nicknames` 全局字典（`{user_id: nickname}`），提供 `get_custom_nickname()`/`set_custom_nickname()` 方法，随 `save_to_file()` 持久化。
- 默认读写 `data/naturel_gpt/naturel_gpt.json`，可配置为 pickle。
- `save_to_file()` 对普通保存做节流；仅在必要时使用 `must_save=True`。
- **原子写入**：`_save_to_file_json` 和 `_save_to_file_pickle` 先写入 `.tmp` 临时文件，再 `os.replace` 原子覆盖；若 `os.replace` 失败（如文件锁定），回退到直接写入。
- **global 记忆**：`init_global_memory()` 只清除当前会话的记忆，不跨群清除其他会话的记忆。
- 持久化加载和保存时都要过滤孤立 assistant/tool：历史开头没有真实 user 承接的 assistant 不允许保留；`role="system"` 的消息（包括 `context_only` 的非触发上下文）不进入持久化存储。兼容旧数据时如果读到 `context_only=True` 的消息，加载阶段也要丢弃，避免重启后旧的非触发上下文污染新请求。`请求大模型时发生错误: ...`、`RuntimeError('HTTP ... Error from provider ...')` 等内部模型请求异常不属于 assistant 回复，加载、保存、清理和 prompt 构造前都必须过滤。

## `draw_db.py`

- 绘图提示词 SQLite 数据库存储模块。
- 数据库文件路径：`data/naturel_gpt/draw.db`。
- 表结构：`draw_prompts (task_id TEXT PRIMARY KEY, prompt_data TEXT, created_at, updated_at)`。
- 提供 `save_prompt(task_id, prompt_data)` 保存提示词，重复编号时覆盖（`INSERT OR REPLACE`）。
- 提供 `get_prompt(task_id)` 查询提示词，返回字典或 `None`。
- 提供 `delete_prompt(task_id)` 删除提示词。
- 提供 `list_prompts(limit)` 列出所有提示词（按更新时间倒序）。
- 线程安全：使用 `threading.Lock` 保护数据库操作。
- 在 `__init__.py` 启动时调用 `init_db()` 初始化表结构。

## `text_to_image.py`

- 可选 markdown/文本渲染，依赖 `nonebot_plugin_htmlrender`。
- 导入失败时自动关闭渲染标志。

# Prompt 与回复规范

- 回复应像真实群聊成员一样自然、简短，不写文章；普通回复最多 3 段，Markdown 模式最多 4 段。
- 正常文本回复不使用 Markdown 语法。
- 不过度拆分消息；如需分段，模型使用双换行 `\n\n`。
- 发送端在双换行处拆分，发送段内将双换行折叠为单换行。
- 后处理作为兜底，去除常见 Markdown 标记。
- 工具调用过程不得显式出现在最终回复中。
- 工具调用结果以 system 消息注入（非 assistant content），禁止模型模仿 `[调用结果]` 或 `[搜索工具摘要]` 格式。

# 配置字段速查

## LLM

- `OPENAI_PROFILES`：多组配置，每组含 `api_keys`/`base_url`/`proxy`/`timeout`/`model`/`model_mini`/`temperature`/`top_p`/`max_tokens`/`max_summary_tokens`/`frequency_penalty`/`presence_penalty`/`multimodal`/`extra_prompt`
- `OPENAI_ACTIVE_PROFILE`
- `LLM_ENABLE_STREAM`
- `LLM_SHOW_REASONING`
- `LLM_ENABLE_TOOLS`
- `LLM_DISABLED_TOOLS`：禁用的工具模块名列表，在 `_discover_tools()` 阶段直接跳过加载
- `LLM_MAX_TOOL_ROUNDS`：单轮回复最多工具调用轮数，**不限制 `remember` 记忆工具**

> 旧格式兼容字段（有 `OPENAI_PROFILES` 时可省略）：`OPENAI_API_KEYS`、`OPENAI_BASE_URL`、`OPENAI_PROXY_SERVER`、`OPENAI_TIMEOUT`、`CHAT_MODEL`、`CHAT_MODEL_MINI`、`CHAT_TEMPERATURE`、`CHAT_TOP_P`、`CHAT_PRESENCE_PENALTY`、`CHAT_FREQUENCY_PENALTY`、`CHAT_MAX_SUMMARY_TOKENS`、`REPLY_MAX_TOKENS`

### 工具调用限制常量（`openai_func.py` 代码常量，非配置文件）

- `MAX_TOTAL_TOOL_CALLS`：单轮总工具调用次数上限，默认 7
- `MAX_SEARCH_TOOL_CALLS`：单轮搜索工具调用次数上限，默认 3
- `SEARCH_TOOL_NAMES`：搜索工具名称集合（`tavily_search`、`bocha_search`），用于计数过滤

## 上下文管理

- `CONTEXT_TOKEN_BUDGET`：上下文窗口token预算，控制prompt最大token数，默认4096
- `CONTEXT_WINDOW_SIZE`：上下文目标窗口大小（实际触发轮数），摘要成功后裁剪回此窗口；请求构造和未摘要硬裁剪使用 `CONTEXT_WINDOW_SIZE * (1 + CONTEXT_COMPRESS_THRESHOLD_RATIO)` 的独立缓冲窗口。轮数只按非 `context_only` 的 `role="user"` 出现次数计算，assistant/tool 作为该触发轮的后续内容一起裁剪或摘要
- `CONTEXT_BUFFER_SIZE`：旧版非触发消息缓冲大小配置，仅兼容旧数据含义；`Chat._context_buffer` 兼容路径和主路径 `_recent_context_buffers` 的 `[群聊上下文-非触发消息]` 窗口均按 `CONTEXT_WINDOW_SIZE * (1 + CONTEXT_COMPRESS_THRESHOLD_RATIO)` 独立计算
- `CONTEXT_SUMMARY_ENABLED`：是否启用上下文摘要压缩，启用后超窗口的历史会被异步压缩为摘要
- `CONTEXT_COMPRESS_THRESHOLD_RATIO`：压缩触发阈值乘数，`overflow_rounds > CONTEXT_WINDOW_SIZE * ratio` 时才触发摘要生成；同时决定请求侧独立缓冲窗口大小 `CONTEXT_WINDOW_SIZE * (1 + ratio)`。摘要开启时先生成摘要，成功后再裁剪
- `TOOL_CONTEXT_TOKEN_BUDGET`：工具调用和思考内容的共享token预算，超出时从旧到新逐组去除，默认16384
- `TOOL_CONTEXT_MODE`：工具上下文模式，1=完整工具+思考上下文，2=仅保留思考上下文，3=仅保留工具调用摘要，默认3

## 消息分段

- `NG_ENABLE_MSG_SPLIT`
- `REPLY_SEGMENT_INTERVAL`
- `REPLY_MAX_SEGMENTS`

## 多模态

- `MULTIMODAL_ENABLE`
- `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`：全局限制，所有来源（prompt_messages + 当前消息）的含图消息总数受此约束，超出时从最旧的开始剥离图片
- `MULTIMODAL_IMAGE_FRESH_MINUTES`：图片有效期（分钟），默认120
- Profile 级 `multimodal`：每个 `OPENAI_PROFILES` 条目可设置 `multimodal: true/false`，关闭时请求层自动剥离 `image_url` 内容

## 工具

- `TAVILY_API_KEY`：Tavily 搜索 API Key 列表，启动时自动选用额度剩余最多的 key
- `BOCHA_API_KEY`
- `BOCHA_API_BASE`
- `BOCHA_SEARCH_COUNT`：博查搜索默认结果数，默认20；工具层强制单次至少10条、最多20条
- `COMFYUI_BASE_URL`
- `MANGA_IDLE_MINUTES`：漫画模式下多少分钟未画图触发自动画图，默认5
- `MANGA_IDLE_ROUNDS`：漫画模式下多少轮对话未画图触发自动画图，默认5
- `WEB_FETCH_TIMEOUT`
- `WEB_FETCH_MAX_CHARS`
- `PLAYWRIGHT_TIMEOUT`
- `LLM_TOOL_LOLICON_CONFIG`
- `UNLOCK_CONTENT_LIMIT`：内容限制解锁全局默认值（`True`/`False`），默认 `False`。新群未通过 `rg nolimit` 设置过时使用此值；每群可通过 `rg nolimit on/off` 独立覆盖，持久化在 `ChatData.unlock_content_limit`。

## 人格

- `DEFAULT_PERSONA`
- 人格目录固定为 `config/personas/`，实际路径为 `Path(config_path).resolve().parent / "personas"`。
- 若 `DEFAULT_PERSONA` 为空或不在已加载人格中，首个加载人格设为默认；无加载人格时使用内置 `default`。

# 开发规范

- 改动范围默认限于 `ATRI/plugins/nonebot_plugin_naturel_gpt/` 和显式的配置/人格 fixture，除非用户另有要求。
- 修改 matcher 行为前，需同时梳理普通消息流和 `rg` 指令流。
- 修改 prompt 生成前，阅读 `Chat.get_chat_prompt_template()`。
- 修改人格加载前，阅读 `config.py` 和 `persona_loader.py`。
- 修改持久化前，阅读 `persistent_data_manager.py`，保持 JSON 兼容性。
- 修改配置字段时，同步更新：
  - `Config`
  - `CONFIG_TEMPLATE`
  - `_load_config_obj_from_file()` 中的 YAML 迁移/默认值处理
  - `README.md`
- 新增 OneBot 消息处理时，同步更新 `utils.gen_chat_payload()` 和 `matcher.do_msg_response()` 的发送逻辑。
- 新增指令时，在 `command_func.py` 用 `cmd.register(...)` 注册，并保留权限检查。
- 新增工具时，在 `llm_tool_plugins/` 新增文件，并通过 `llm_tools.py` 注册。
- 非必要时不要执行会访问外部 API 的命令。

# 验证清单

- 语法检查所有变更的 Python 文件。
- 大范围插件变更时，编译以下核心文件：
  - `__init__.py`
  - `chat.py`
  - `config.py`
  - `command_func.py`
  - `matcher.py`
  - `openai_func.py`
  - `llm_tools.py`
  - `persona_loader.py`
  - `persistent_data_manager.py`
- 工具变更时，编译所有 `llm_tool_plugins/*.py`。
- Matcher 变更时，覆盖以下场景：
  - 群聊消息、私聊消息、纯图片消息
  - `rg`、`rg list`、`rg set <persona>`
  - 忽略前缀、禁用户/禁群
  - `at` 片段与 `at all`
- 人格变更时，验证简单 `.md` 文件与 skill 文件夹可在 `config/personas/` 共存。
- 配置变更时，验证配置加载不会意外丢弃未知字段，且能正确补齐缺失默认值。
