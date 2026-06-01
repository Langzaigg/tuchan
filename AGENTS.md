# 项目概述

这是一个基于 NoneBot2 / OneBot v11 的 QQ 机器人项目，核心功能由 `naturel_gpt` 插件提供。当前所有功能开发、问题修复和配置调整均围绕该插件展开。

## 核心架构决策

- **LLM 后端**：通过 LiteLLM 统一调用（`openai_func.py`），支持多 key 轮询、自定义 base_url、代理、流式输出。支持多组 OpenAI 配置（`OPENAI_PROFILES`），通过 `rg model` 指令运行时切换。
- **工具调用**：使用原生 OpenAI-compatible Tool Calling 替代旧的文本协议（`/#tool&args#/`）。工具定义在 `llm_tool_plugins/` 下，由 `llm_tools.py` 聚合调度。
- **流式回复**：开启 `LLM_ENABLE_STREAM` 后，模型输出按双换行 `\n\n` 分段发送，受 `REPLY_SEGMENT_INTERVAL` 和 `REPLY_MAX_SEGMENTS` 控制。
- **多模态输入**：支持 OneBot 图片消息片段，解析为 `image_url`。用户消息中的图片以 `[图片N]` 占位符内联标记。**图片门控**：触发消息的图片始终保留；若触发消息文本含图片关键词（`图`/`画`/`看`/`照片`/`截图`/`image`/`pic`/`photo`/`前`/`上`/`这`），从 context_only 缓冲区收集额外图片注入触发消息；历史用户消息不注入图片，只保留文本。`MULTIMODAL_IMAGE_FRESH_MINUTES` 控制图片有效期。每个 profile 有独立的 `multimodal` 开关，关闭时请求层自动剥离 `image_url` 内容。`_is_supported_image_url` 支持 `http://`、`https://`、`data:image/`、`file:///` 协议。
- **图片缓存**：`image_cache.py` 提供异步图片下载 + 内存 LRU 缓存，将远程 URL 转为 `data:image/xxx;base64,...` 格式提交给 LLM API，避免 API 侧无法访问 QQ 等私有 URL。单图上限 10MB，总缓存上限 50MB。每次 `get_chat_prompt_template()` 构建完毕后自动清除不在 `prompt_messages` 中的缓存条目。下载失败时回退到原始 URL。
- **Think 标签过滤**：Grok 等模型将思考内容以 `<think>` 标签放在 `content` 中返回。流式回调中实时拦截 `<think>...</think>` 内容，提取到 `reasoning_content` 字段；兜底正则在最终响应上做二次过滤。
- **非触发消息缓冲**：不需要回复的群消息不写入 `prompt_messages`，而是存入临时缓冲区（长度上限 `CONTEXT_BUFFER_SIZE`）。当下一条触发消息到达时，缓冲区内容作为 `context_only` system 消息注入到 `prompt_messages` 中（置于触发消息之前），带有 `[群聊上下文-非触发消息]` 前缀，图片 URL 追加到触发消息的图片列表。`context_only` 消息不计入对话轮数、不增加 token 消耗，每次追加前清除旧的 `context_only`。上下文中图片占位符用全局计数器区分（如 Marcel 的 `[图片1]` 和 严肃早睡中的 `[图片2]`）。`context_only` 消息使用 `role="system"`，不进入持久化存储。
- **人格系统**：运行时动态加载，来源固定为 `config/personas/` 目录。YAML 中的 `PRESETS` 仅作为运行时容器，不作为人工编辑源。
- **扩展系统**：旧版 Extension 运行时扩展系统、PresetHub 集成、`/#...#/` 文本协议均已移除，不再使用。
- **Debug 日志**：每次 LLM 请求完成后，将最近一次请求/响应保存到 `data/naturel_gpt/logs/{chat_key}.latest.json`（图片 base64 替换为占位符）；摘要任务完成后，将摘要 LLM 的请求/响应保存到 `{chat_key}.summary.json`。reasoning 内容完整保存，不截断。摘要日志包含上下文摘要、工具摘要和用户印象（如有）。debug 日志中 `response` 为最终回复，`intermediate_responses` 为中间轮 assistant 回复文本列表，`tool_messages` 为完整工具调用链。
- **请求打断与部分回复保留**：同群新消息打断旧请求时，已接收的流式内容（剥离 `<think>` 标签后）保存到 `Chat._last_interrupted_response`，下次请求时作为 system 消息注入上下文，避免模型重复已说过的内容。
- **模型专用提示词**：每个 `OPENAI_PROFILES` 条目可设置 `extra_prompt` 字段，注入到 system1 消息末尾。用于针对特定模型的行为调优（如 kimi 的工具调用积极性、减少推理等）。

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
├── command_func.py         # 命令管理
├── persona_loader.py       # 人格加载
├── utils.py                # 工具函数
├── chat_manager.py         # 会话管理器
├── text_to_image.py        # 文本转图片
├── store.py                # 序列化工具
├── singleton.py            # 单例模式
└── logger.py               # 日志配置
config/naturel_gpt_config.yml          # 主配置
config/personas/                       # 人格加载目录（.md 单文件 / skill 文件夹）
data/naturel_gpt/                      # 持久化状态与日志（避免直接修改）
```

# 模块说明

## `__init__.py`

- 导入时加载配置与持久化聊天状态。
- 初始化 `TextGenerator`，从当前激活 profile 读取 `extra_prompt` 并传入。
- 导入 `matcher`，通过导入副作用注册事件处理器。
- 调用 `init_tools(config)` 进行条件工具注册（博查搜索仅在 `BOCHA_API_KEY` 非空时注册；`LLM_DISABLED_TOOLS` 列表中的工具在 `_discover_tools` 阶段跳过加载）。
- Anima 画图：启动时无条件执行 health check，通过则自动开启（不再依赖 `COMFYUI_ENABLED` 持久化状态），并将 `COMFYUI_ENABLED = True` 写回配置。
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
- **Content 兜底**：API 返回 `content: None` 时强制转为 `""`，避免 Moonshot 等后端报 "assistant message must not be empty"。
- **Thinking 模式兼容**：构造 assistant 消息时，若存在 `reasoning_content` 会一并保留，防止 tool call 消息缺少该字段导致 400。
- **工具结果临时性**：`stream_response()` 返回四元组 `(text, success, tool_messages, reasoning_content)`，工具消息和思考内容只在当前请求内使用，不持久化。
- **工具调用多轮分段**：中间轮（有 tool_calls）的 assistant 文本通过 `on_text` 回调实时输出，工具执行完毕后自动插入 `\n\n` 分隔符，与最终轮分段。`intermediate_texts` 收集中间轮文本，在最后一轮前注入 system 提示避免语义重复。
- **`LLM_MAX_TOOL_ROUNDS` 不限制记忆工具**：流式回调中每轮结束后检查工具名集合，若仅包含 `remember` 则不计入轮数，允许记忆整理不受轮数限制。
- **工具调用次数限制**：单轮总工具调用次数上限 `MAX_TOTAL_TOOL_CALLS=7`；单轮博查搜索（`bocha_search`）调用次数上限 `MAX_SEARCH_TOOL_CALLS=3`。博查搜索超限时不强制停止，仅注入系统提示提醒模型不再调用。
- **画图任务编号防伪**：当用户消息含画图关键词（`画`/`draw`/`改图`/`重画`/`来一张`/`整一张`）时，force 模式下预先注入 system 消息引导模型调用工具。拦截检查延迟到整轮结束后（工具仍可用但模型未调用时），缓冲流式内容检查是否含伪造任务编号（`任务编号`/`单号` 后跟 6 位字母数字、`draw-XXXXXX` 格式或占位符回显 `[编号已隐藏，请调用 generate_anima_image 画图工具]`）。若检测到伪造编号且无 `generate_anima_image` tool_calls，整条消息拦截不发送，注入系统提示强制重试（最多 1 次）。重试轮不进入拦截逻辑。若用户消息不含画图关键词或已重试过，仅做被动清理（从返回文本中剥离伪造编号）。占位符回显（`[请调用 generate_anima_image 画图工具获取编号]`）由 `_clean_fake_task_ids` 静默移除，不触发告警。
- **流式工具名双拼修复**：`_stream_once()` 中流式拼接 `function.name` 时，某些 provider 可能重复发送 name chunk 导致双拼（如 `generate_anima_imagegenerate_anima_image`）。拼接后检测 `name[:half] == name[half:]` 自动去重。
- **异常处理**：`stream_response()` 内部循环异常时始终 return（不再因多 key 而 continue 死循环），由 matcher 外层重试逻辑统一处理。
- **并发控制**：`_pending_merge_input` 字段存储待合并的输入，工具调用中收到的新消息会合并到当前输入，而非分别处理。
- **Profile 切换**：`switch_profile()` 方法运行时重新初始化连接参数（api_keys、base_url、proxy、multimodal、extra_prompt 等），无需重启。
- **多模态剥离**：`_completion_kwargs()` 中，若当前 profile 的 `multimodal=False`，自动将消息中的 `image_url` 内容转为文本占位符，避免不支持图片的 API 报错。
- **流式超时**：`_stream_iter_openai()` 中，httpx 的 `read` timeout 在每个 chunk 到达时重置；总体响应时间硬上限 5 分钟（`MAX_TOTAL_SECONDS`），超出时中断流式输出。
- **model_mini 回退**：`_completion_kwargs()` 中，当 `type='summarize'` 或 `type='impression'` 时使用 `model_mini`；若 `model_mini` 为空则自动回退到 `model`，避免摘要生成因配置缺失而失败。

## `llm_tools.py`

- 聚合所有原生工具定义。
- 调用 `llm_tool_plugins/` 下的各工具模块。
- 工具输出（如图片 URL）暂存，供 matcher 在文本流结束后统一发送。

## `llm_tool_plugins/`

每个工具独立为一个文件，当前内置工具：

- **`pixiv_search.py`**：Pixiv 图片搜索。多关键词无结果时自动取首个关键词重试；工具返回不含图片 URL，仅告知模型图片会自动发送。
- **`fetch_url.py`**：轻量 HTTP 文本抓取。
- **`browse_url.py`**：Playwright 渲染页面文本抓取。schema 描述中明确标注为 fallback：`ONLY use when fetch_url fails or the page requires JavaScript rendering`，引导 LLM 优先使用轻量抓取。
- **`bocha_search.py`**：博查网页搜索。当 LLM 对问题不确定、不了解或涉及实时信息时应主动搜索验证，不猜测不确定的事实。启动时若 `BOCHA_API_KEY` 为空则不注册。
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
  - `fetch_schema_and_knowledge_sync()` 从 ComfyUI 服务拉取 schema 与 knowledge。
  - 工具调用后即时返回第一人称作画描述文本，并告知用户大约1分钟后画完、届时系统自动发送，后台 `asyncio.create_task` 提交生成任务。
  - 生成结果进入 `_pending_results` 队列，由 matcher 在文本回复发送完毕后消费并发送图片。
  - `_bg_tasks: set` 保留 Task 引用防止 gc 取消；httpx timeout 300s。
  - schema 与 knowledge 均经过压缩处理：schema description 精简为一句话引导；knowledge 按文件类型分别压缩（`anima_expert.md` 去掉默认参数/长宽比段落、`artist_list.md` 只保留 @artist 列表、`prompt_examples.md` 裁剪到 3 个代表性场景），硬编码核心规则压缩为 5 条 bullet。
  - **画图任务编号**：调用成功后返回随机 6 位字母数字任务编号（格式 `draw-XXXXXX`）和预计生成时间（精确到秒）。schema 和 knowledge 中明确禁止模型编造虚假任务编号，只有工具返回的编号才算成功调用。
  - **队列限制**：当 ComfyUI 队列长度大于 5 时拒绝生成并提示用户稍后重试。预计生成时间公式为：`当前图片预计生成时间 + 队列中图片数 * 90秒 - 30秒`。
  - **发图拼接**：图片生成后通过 OneBot 发图时，将任务编号拼接在图片消息前一起发送。
- **`nas_game_list.py`**：NAS 游戏目录查询。`get_tool_schemas()` 中根据 `NAS_GAME_WHITELIST_GROUPS` 白名单过滤 schema，非白名单群连工具定义都不可见（LLM 无法"看到"此工具），`run()` 中也保留运行时二次校验。`_check_whitelist()` 接受可选 `chat_key` 参数，优先使用传入值而非 `TextGenerator._current_chat_key`。扫描深度上限 `_BRAND_SCAN_MAX_DEPTH=5`，始终递归子目录（不受是否有文件影响），确保多层嵌套的游戏能被扫描到。

新工具遵循同一模式：定义 schema + 提供 `run(args, config)` 入口。

## `chat.py` 及其子模块

`Chat` 类通过 Mixin 模式分拆为多个子模块，便于维护：

- **`chat.py`**：核心类定义、属性、基本操作（人格切换、Profile 管理、缓冲区管理等）。
- **`chat_memory.py`**：记忆管理（`_get_chat_memory`、`_get_user_memory`、`set_memory`）。
- **`chat_history.py`**：对话历史管理（`update_chat_history_row`、`save_tool_messages`、`remove_last_prompt_user_message`、`cleanup_after_bad_request`、`_trim_prompt_messages_without_summary`、`_cleanup_orphan_tool_messages`、`_count_rounds`）。`save_tool_messages` 中校验工具名：修复 provider 重复发送 name chunk 导致的双拼函数名（如 `generate_anima_imagegenerate_anima_image` → `generate_anima_image`），剔除不在 `TOOL_REGISTRY` 中的无效工具调用。
- **`chat_prompt.py`**：prompt 构造（`get_chat_prompt_template`、`_build_openai_history_messages`、`_trim_messages_to_request_budget`、`_message_text_for_prompt`、`_message_content_for_prompt`、`_format_prompt_message_for_summary`）。
  - **历史上下文单号隐藏**：`_message_text_for_prompt` 对所有 assistant 历史消息自动替换任务编号（匹配 `任务编号`/`单号` + 可选分隔符 + 可选 markdown 加粗 + 可选 `draw-` 前缀 + 6 位字母数字，以及无前缀的 `draw-XXXXXX` 格式）为 `[编号已隐藏，请调用 generate_anima_image 画图工具]`，防止模型从历史中引用伪造编号。
- **`chat_summary.py`**：摘要/印象生成（`generate_tool_call_summary`、`_compress_prompt_messages_if_needed`、`_save_summary_log`）。

核心功能说明：

- 定义 `Chat` 领域对象，围绕持久化 `ChatData` 运作。
- 负责人格切换、记忆、聊天历史、摘要、用户印象、prompt 构造、发送与生成时间戳。
- **Profile 管理**：`get_active_profile()` / `set_active_profile()` 管理每群的 OpenAI profile；`apply_profile()` 在消息到达时自动切换 TextGenerator。
- **`get_chat_prompt_template()`**：返回 OpenAI 风格的对话消息列表。
  - 系统消息拆分为 4 条：
    - system 1 = 角色设定 + 响应规则 + 工具基础规则（**极稳定前缀，完全不变，最大化缓存命中**）
    - system 2 = 画图知识（仅 force/on/auto+关键词时注入）+ 模型专用提示词（`extra_prompt`，仅非空时追加）—— 条件追加，变化不影响 system 1 缓存
    - system 3 = 记忆（群+用户）+ 记忆提醒 + 日期
    - system 4 = 摘要 + 印象（会话级变化，**印象为空时不显示 `[impression]` 标签**）
  - 若当前会话启用了 Anima 画图，在 system 2 中注入 `[你的绘画技能]` knowledge（从 ComfyUI 拉取，经压缩处理：精简提示词规范、保留画师列表、裁剪示例到 3 个代表性场景、压缩调用规则）。**auto 模式下仅在有画图关键词时注入**，避免无画图意图时白白占用 token。
  - 工具基础规则新增约束：调用工具时先输出 tool_calls，等系统返回结果后再在回复中引用编号，禁止在 tool_calls 之前就在 content 中写任务编号。
  - 系统提示要求模型像真实群聊成员一样自然说话，最多 3 段，不用 Markdown，可用双换行分段。
- **结构化历史管理**：
  - `prompt_messages`：核心对话历史，仅记录触发了回复的用户、Bot、`context_only` 上下文和工具消息。图片作为消息的 `images` 字段存储，不再有独立的图片历史表。
  - `_context_buffer`：非触发消息临时缓冲区（长度上限 `CONTEXT_BUFFER_SIZE`），触发时作为独立的 `context_only` system 消息注入 `prompt_messages`（置于历史之后、触发消息之前）。`context_only` 消息使用 `role="system"`，带有 `[群聊上下文-非触发消息]` 前缀，不进入持久化存储，不计入对话轮数。
  - `chat_impressions`：用户印象字典，按用户ID存储对话印象，用于个性化回复。
  - `context_summary`：压缩上下文摘要，当历史过长时异步生成，用于维持长对话连贯性。
  - `tool_call_summary`：工具调用摘要（模式3），存储在对应的 assistant 消息中，每次工具调用后异步生成，失败时 fallback 为截断原文。
  - `_last_interrupted_response`：被中断的流式回复内容，打断时保存，下次请求时注入上下文。
  - `_compress_failure_time`：摘要压缩失败的时间戳，用于 120 秒冷却期内跳过压缩触发。
- **失败回复保存**：当 LLM 请求失败（`success=False`）时，如果有已生成的回复（可能包含工具调用结果），也会保存到 `prompt_messages`，避免 bot 回复丢失导致上下文断裂。
- **上下文窗口**：`CONTEXT_WINDOW_SIZE` 按对话轮数计算（1轮=1条用户消息+1条回复），滑窗截断和溢出检测均按轮数判断。`_trim_prompt_messages_without_summary()` 和 `_build_openai_history_messages()` 的截断点已修正 `+1`（off-by-one），确保最旧的溢出轮本身也被正确截断。
- **工具消息截断保护**：
  - `_cleanup_orphan_tool_messages()` 清理孤立的 tool 消息（无对应 assistant 的 tool_calls）。
  - `_trim_prompt_messages_without_summary()` 截断前**和截断后**均调用 `_cleanup_orphan_tool_messages()`，因为截断可能从 assistant+tool_calls 和 tool 结果之间切断产生新的孤立消息。
  - `_build_openai_history_messages()` 工具组完整性检查要求同时具备 `has_tool_result` 和 `has_tool_calls`，仅有 tool 结果而无 assistant+tool_calls 的孤立组被丢弃。
  - `_trim_messages_to_request_budget()` 从末尾向前遍历删除最旧历史，保护系统消息（`role="system"`）和触发消息（最后一条 user 消息），避免误删 context_only 或触发消息。
- **Token 截断**：使用准确的token计算方法（包括图片token估算），智能截断时优先保留包含工具结果、摘要、记忆等重要内容。
- **图片有效期**：通过 `MULTIMODAL_IMAGE_FRESH_MINUTES` 配置项控制图片上下文的有效时间（默认120分钟）。图片从 `prompt_messages` 检索，受图片门控约束。
- **图片位置标记**：图片在文本中自动标记为 `[图片1]`、`[图片2]`，保持图片与文本的对应关系。
- **用户印象压缩**：用户消息累积到 `chat_history`（上限 `USER_MEMORY_SUMMARY_THRESHOLD * 2`），由摘要任务统一生成印象（异步），不再单独调用 LLM。使用智能淘汰策略，保留最近对话和包含关键词的重要历史。**仅对本次溢出中实际产生互动的用户生成印象**（通过 `_pending_overflow_user_ids` 跟踪），不更新所有有历史的用户。
- **记忆管理**：超出最大长度时不再自动删除，仅记录警告，由 LLM 通过记忆工具的整理模式主动精简。
  - 群记忆 (`chat_memory`)：所有人共享，注入到 `[群记忆]`。
  - 用户记忆 (`user_memories`)：按用户ID存储，注入到 `[你的记忆]`。
  - 记忆与人格关联，每个人格有独立记忆空间。
  - 记忆接近上限时在 system2 中注入 `[记忆提醒]`，建议 LLM 调用 consolidate 整理。
  - `rg reset` 不清除记忆，记忆由 `rg mem clear <scope>` 专门管理。
- **摘要压缩**：异步执行（`asyncio.create_task`），不阻塞用户消息响应。
  - `_trim_prompt_messages_without_summary()` 截断时保存溢出消息到 `_pending_overflow_text`，供后续摘要任务使用（不再直接丢弃）。
  - `_compress_prompt_messages_if_needed()` 触发条件：当前有溢出 **或** 之前有截断时累积的 `_pending_overflow_text`。
  - 摘要触发受 `CONTEXT_COMPRESS_THRESHOLD_RATIO` 控制（默认 0.5，即溢出超过窗口的 50% 才触发）。
  - 摘要字数限制从 `max_summary_tokens` 配置动态读取（中文约 1 token ≈ 1 字），不在 prompt 中硬编码。
  - **失败冷却**：摘要生成失败后设置 `_compress_failure_time`，120 秒内不再触发压缩，避免持续发送无效请求。
  - **溢出文本恢复**：失败时将 `_pending_overflow_text` 恢复，下次触发时重试相同文本。
  - 摘要和印象生成完毕后调用 `save_to_file()` 持久化。摘要日志（`.summary.json`）包含上下文摘要、工具摘要和用户印象。
- **工具调用摘要（模式3）**：
  - 仅对搜索类工具（`bocha_search`、`fetch_url`、`browse_url`）生成 LLM 摘要，标记为 `[搜索工具摘要]`。
  - 其他工具（`pixiv_search`、`remember` 等）保留原始结果，标记为 `[调用结果]`。
  - **绘画工具不显式记录**：`generate_anima_image` 的工具调用结果不注入历史上下文，避免 LLM 看到历史中的调用记录后产生"已经画过了"的错觉。
  - 摘要以 system 消息**紧跟在对应 assistant 消息后面**注入（不附加到 assistant 的 content 中），保证摘要不漂移，避免模型模仿工具结果格式。
  - 搜索摘要的 LLM prompt 中包含触发搜索的原始问题。
  - 构建 `normal_messages` 后，检查开头是否有孤立的工具摘要 system 消息（工具调用已超出窗口但摘要残留），有则丢弃。

## `matcher.py`

- 主 OneBot v11 集成入口。
- 通过 `utils.gen_chat_payload()` 从消息片段提取文本、图片 URL（带位置标记）。
- 允许纯图片消息（多模态开启时）。
- 更新历史记录时补充图片元数据；`CHAT_ENABLE_RECORD_ORTHER` 分支同样记录图片，防止非 @ 消息图片丢失。
- 调用 `TextGenerator.stream_response()` 获取模型输出和工具消息，按 `\n\n` 分段发送。
- **Profile 切换**：消息到达时，`chat.apply_profile()` 自动将 TextGenerator 切换到该群的 profile，确保每群使用各自的模型配置。
- **工具结果持久化**：`stream_response()` 返回的 `tool_messages` 通过 `chat.save_tool_messages()` 保存到历史。
- **图片 400 重试**：LLM 请求返回图片下载相关 400 错误（`Cannot download image`、`failed to download url data` 等）时，自动清理历史图片并以无图模式重试，最多 2 次。非图片 400 错误不重试，直接进入失败处理。
- **旧请求打断**：
  - 收到新消息时，若同群存在旧请求，先判断新消息 `should_reply`（唤醒词、禁用词、违禁词检查）。
  - 只有确定需要回复的消息才打断旧请求；不需要回复的消息在 `return` 前清理 `_chat_active_inputs`。
  - 若旧请求正处于工具调用阶段（`tg._tool_calling == True`），不 cancel，将新输入合并到 `_pending_merge_input`。
  - 旧请求完成后，检查 `_pending_merge_input`，如果有待合并的输入则递归调用 `do_msg_response()` 处理。
  - 打断时捕获 `CancelledError`，从 `raw_parts` 中取出已接收内容（剥离 `<think>` 标签），保存到 `chat.set_interrupted_response()`。
  - 下次请求时，通过 `chat.pop_interrupted_response()` 取出中断回复，与 context buffer 合并为一条 `context_only` system 消息注入。
- **工具图片发送**：`consume_tool_outputs()` + 图片发送在 `stream_response` 返回后立即执行，位于所有 `return` 分支之前，确保工具图片不因 `success=False` 等错误被跳过。
- **失败回复保存**：当 LLM 请求失败（`success=False`）时，如果有已生成的回复（可能包含工具调用结果），也会保存到 `prompt_messages`，避免 bot 回复丢失导致上下文断裂。
- **Token 超限处理**：检测到 token 超限错误时，自动清理历史至最后 5 条并提示用户。
- **唤醒词机制**：
  - 前缀唤醒和名称提及额外检查 `chat.preset_key`，当前激活角色名也作为唤醒词。
  - 名称提及检测使用 `any()` 替代旧 `random.choice()`，避免漏检。
  - 唤醒词仅在句首触发（`startswith`），句中或句尾出现时不无条件唤醒，走 `RANDOM_CHAT_PROBABILITY`。
- **请求日志**：生成 prompt 后打印请求 token 数和缓存命中 token 数预测值。
- **Think 标签实时拦截**：流式回调 `on_text_chunk` 中维护状态机，实时检测 `<think>` 和 `</think>` 标签，将思考内容提取到 `_extracted_reasoning` 而非输出到聊天。兜底 `_strip_think_tags` 在最终响应上做正则二次过滤。
- 不再解析 `/#...#/` 旧工具调用格式。

## `utils.py`

- `gen_chat_payload()` 返回文本（带图片位置标记）、唤醒标志、图片 URL。
- `gen_chat_text()` 保持纯文本兼容。
- `_extract_message_text_and_images()` 提取消息文本和图片，图片在文本中自动标记为 `[图片1]`、`[图片2]`。
- 用户名解析可能调用 OneBot API，需容忍失败。

## `command_func.py`

- `CommandManager` 实现所有 `rg` 指令。
- `rg` / `rg list`：重载动态人格并列出可用人格。
- `rg set <persona>`：将运行时 `config.PRESETS` 中的指定人格加入当前会话并切换。
- `rg query <persona>`：查看已加载人格内容。
- `rg draw [force/on/auto/off]`：Anima 画图开关。执行 health check → 拉取 schema/knowledge → 注册/卸载工具 → 维护内存级会话开关。
  - `force`：常驻工具 + 画图关键词时拦截虚假回复（缓冲模式，检测到伪造编号整条不发送并重试 1 次）
  - `on`：常驻工具，不拦截
  - `auto`：仅在用户消息含画图关键词时注入工具到请求中（默认）
  - `off`：关闭
  - 模式持久化存储在 `ChatData.draw_mode` 中，重启后保持
- `rg model [profile_name]`：列出或切换 OpenAI 配置。无参数时列出所有 profile 及当前激活状态；有参数时切换到指定 profile 并持久化。切换为按群生效，每个群有独立的模型配置。
- `rg mem`：查看当前人格的群记忆和用户记忆。
- `rg mem clear <scope>`：清除当前人格的记忆。`group`=群记忆，`user`=当前用户记忆，`all`=全部。
- `rg reset` 只清除上下文（prompt_messages/context_summary/impressions），保留群记忆和用户记忆。
- PresetHub 命令与旧扩展管理命令已移除。

## `persistent_data_manager.py`

- 定义持久化数据类；`ChatData` 包含 `chat_image_history` 用于多模态上下文（已弃用，保留字段兼容旧数据）。
- `ChatMessageData` 支持三种角色：`user`、`assistant`、`tool`，其中 `tool` 角色包含 `tool_call_id` 和 `tool_name` 字段，`assistant` 角色可包含 `tool_calls` 和 `tool_call_summary` 字段。`user` 消息新增 `context_only` 字段标记非触发上下文，不计入对话轮数。
- `PresetData` 包含 `chat_memory`（群记忆）和 `user_memories`（用户个人记忆），记忆与人格关联。
- `ChatData` 包含 `active_profile` 字段，存储每个群/私聊的 OpenAI profile 名，支持每群独立模型配置。
- `ChatData` 包含 `draw_mode` 字段，存储每个群的 Anima 画图模式（`force`/`on`/`auto`/`off`），默认 `auto`。
- 默认读写 `data/naturel_gpt/naturel_gpt.json`，可配置为 pickle。
- `save_to_file()` 对普通保存做节流；仅在必要时使用 `must_save=True`。
- 序列化时过滤掉 `role="system"` 的消息（包括 `context_only` 的非触发上下文），不进入持久化存储。

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
- `MAX_SEARCH_TOOL_CALLS`：单轮博查搜索（`bocha_search`）调用次数上限，默认 3
- `SEARCH_TOOL_NAMES`：博查搜索工具名称集合，用于计数过滤

## 上下文管理

- `CONTEXT_TOKEN_BUDGET`：上下文窗口token预算，控制prompt最大token数，默认4096
- `CONTEXT_WINDOW_SIZE`：上下文窗口大小（对话轮数），每轮=1条用户消息+1条回复，控制保留多少轮最近对话
- `CONTEXT_BUFFER_SIZE`：非触发消息缓冲区大小（消息条数），同时控制图片视野窗口，默认10
- `CONTEXT_SUMMARY_ENABLED`：是否启用上下文摘要压缩，启用后超窗口的历史会被异步压缩为摘要
- `CONTEXT_COMPRESS_THRESHOLD_RATIO`：压缩触发阈值乘数，溢出超过窗口*此比例才触发摘要生成，默认0.5
- `TOOL_CONTEXT_TOKEN_BUDGET`：工具调用和思考内容的共享token预算，超出时从旧到新逐组去除，默认8192
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

- `BOCHA_API_KEY`
- `BOCHA_API_BASE`
- `BOCHA_SEARCH_COUNT`
- `COMFYUI_BASE_URL`
- `WEB_FETCH_TIMEOUT`
- `WEB_FETCH_MAX_CHARS`
- `PLAYWRIGHT_TIMEOUT`
- `LLM_TOOL_LOLICON_CONFIG`

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
