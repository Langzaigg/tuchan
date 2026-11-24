# 项目概述

基于 NoneBot2 的 QQ 机器人。默认使用 OneBot v11，也可通过 QQ 官方适配器接入；核心聊天功能在 `nonebot_plugin_naturel_gpt`，另有 Dice! 指令插件。

本文只记录跨模块约定和容易破坏的边界。具体参数、工具知识和实现细节以代码及 README 为准，避免重复维护。

## 开发约定

- 默认改动范围为 `ATRI/plugins/nonebot_plugin_naturel_gpt/` 及明确指定的配置、人格或接入层。
- 改 `matcher.py` 前梳理消息流与 `rg` 指令流；改 prompt 前读 `get_chat_prompt_template()`；改存档前读 `persistent_data_manager.py`，保持旧 JSON/Pickle 兼容。
- 配置变更同步 `Config`、`CONFIG_TEMPLATE`、迁移逻辑、README 和本文；新增工具放入 `llm_tool_plugins/`，新增指令通过 `cmd.register()` 保留权限检查。
- 新增消息类型同步 `utils.gen_chat_payload()` 与 `matcher.do_msg_response()`。
- 非必要不访问外部 API，不把真实密钥、账号、聊天数据或本地配置提交到仓库。
- **缓存不变式**：第 k 轮请求前缀应是第 k-1 轮的严格前缀，只有尾部触发标记、记忆提醒等每轮必变内容例外；历史轮写入后不得改写。

## 关键路径

```text
main.py / ATRI/__init__.py / ATRI/config.py    # 多接入点启动
ATRI/naturel_gpt_storage.py                   # 配置覆盖层、共享存储、旧档迁移
ATRI/qq_official.py                           # QQ 官机事件/API → OneBot v11
ATRI/plugins/nonebot_plugin_naturel_gpt/
├── matcher.py                                # 消息入口、回复、重试、指令衔接
├── openai_func.py                            # 请求、工具循环、重试、协议调度
├── anthropic_func.py / gemini_func.py        # 原生协议翻译
├── chat.py / chat_history.py / chat_prompt.py / chat_summary.py / chat_memory.py
├── config.py / persona_loader.py             # 配置与人格
├── llm_tools.py / llm_tool_plugins/          # 原生工具调用
├── image_cache.py / error_gif.py             # 图片缓存与失败兜底
├── persistent_data_manager.py / draw_db.py / stats.py
└── utils.py / command_func.py / chat_manager.py
config/naturel_gpt_config.yml                 # Naturel GPT 主配置（本地忽略）
config/personas/                              # .md 人格或 skill 文件夹
data/                                         # 运行数据，勿直接编辑或提交
```

## 启动、多接入与存储

- `main.py` 按 `config.yml` 的 `BotEndpoints.*.enabled` 启动接入点；多个接入点使用独立进程。`ATRI_ENDPOINT` 可显式选择接入点，旧 `BotSelfConfig` 兼容为 `onebot`。
- OneBot 使用反向 WebSocket；QQ 官机默认主动连接官方 WebSocket。`qq_official.py` 将 OpenID 映射为稳定数字 ID，并转换消息、通知、引用、合并转发和既有插件所需 API。主动推送、群管理等仍受平台权限限制。
- 非默认接入点在 `data/endpoints/<接入点>/runtime/` 运行，避免相对路径状态互相污染。
- Naturel GPT 共用主配置；`config/bots/<首个昵称>/<接入点>.yml` 只保存覆盖键。启动时合并主配置并精简同值项，保存时只回写覆盖键；非默认接入点始终显式保留 `ADMIN_USERID` 和 `NAS_GAME_WHITELIST_GROUPS`。
- 非默认接入点的数据根为 `data/endpoints/<接入点>/naturel_gpt/`。按接入点昵称选择人格和 `archives/<昵称>/` 存档；插件 YAML 显式配置 `nickname` 时改用默认数据根。新昵称首次可从旧档导入匹配人格及可归属记忆，旧档保留。
- 画图数据库和 NAS 共用项由 `NaturelGPTShared` 管理。`notify_superusers` 只控制通知，不影响权限；`hikari_enabled` 控制 Hikari 加载。

## Naturel GPT 配置与人格

- `Config` 实例由 `from .config import config` 获取；重载后需要最新对象时使用 `get_config()`。profile 辅助功能必须保持为 `Config` 方法，不要误写成模块函数。
- `OPENAI_PROFILES.default` 是真实 profile 名的**字符串指针**。解析顺序：会话选择 → default 指针 → 旧 `OPENAI_ACTIVE_PROFILE` → 首个真实 profile。
- 所有 profile 读取走 `config.get_profile(name)`；不要直接 `.get(OPENAI_PROFILES)`。`rg model` 只改会话选择，不改全局默认。
- 请求级 profile 快照固定本轮模型、URL、代理、key、多模态和流式设置，避免并发切换污染工具后续轮。`reasoning_effort` 仅主对话，`reasoning_effort_mini` 仅摘要/印象；未配置就不发送。
- 采样参数和 `max_tokens` 均为“配了才发”，无代码兜底值。`max_summary_tokens` 只是摘要字数软目标，不是 API 参数。
- `postprocess` 支持 `strip_thought_prefix`、`think_leak_retry`、`no_think`、`keep_reasoning`、`no_stream`；旧布尔字段仍兼容。
- `rg model [profile] [effort]` 支持 `low/medium/high/max/default/auto`；`default` 表示省略参数，`auto` 清除会话覆盖。
- 人格只从主配置旁 `personas/` 加载，YAML `PRESETS` 仅为运行时容器。支持单文件 `.md` 和含 `SKILL.md` 的文件夹；技能文件注入顺序见 `persona_loader.py`。昵称优先匹配同名人格，再回退 `DEFAULT_PERSONA`。

## 请求、工具循环与重试

### 请求与工具

- `TextGenerator.stream_response()` 返回 `(text, success, tool_messages, reasoning_content)`；入口浅拷贝 prompt，循环内用 dict 替换，调用方保留请求前快照。
- 工具使用原生 Tool Calling；旧文本协议、Extension/PresetHub 已移除。每个工具文件提供 schema 与 `run(args, config)`。
- 工具轮受 `LLM_MAX_TOOL_ROUNDS`、`LLM_MAX_TOTAL_TOOL_CALLS`、`TOOL_CONTEXT_TOKEN_BUDGET`、`LLM_TOOL_LOOP_MAX_SECONDS` 限制；remember-only 轮不占普通轮数。
- 超限进入终端阶段：只允许 `generate_anima_image`、`generate_moss_image`、`remember` 的单次终端调用，然后无工具收尾。保留完整 tools 数组以稳定缓存。
- **assistant(tool_calls) 与其全部 tool 响应之间不得插入其他消息**。被过滤的调用也必须补“未执行”占位 tool 响应；搜索超限提示放在整批 tool 响应之后。
- 工具附图只认 `into_context: True`，在全部 tool 响应后合并为一条 user 消息；多模态关闭时跳过，并登记到 `_visible_images`。
- `force` 画图提示只在 `type="chat"` 注入；摘要/印象不得继承会话的画图状态。

### 多 key 与错误分类

- key 每次从索引 0 开始，仅跳过冷却或本请求已试过的 key；禁止粘性轮询。视觉 key 使用独立 scope。
- 仅鉴权、额度、限流等 key 级错误（通常 401/402/403/429）顺延；参数错误、超时、上下文或图片问题不得换 key。
- 瞬时网络/网关故障在当前轮内原样重试，已有文字或工具副作用时不得重试。不要移到 matcher 外层重放整个工具循环。
- 异常分类文本必须包含 `e.__class__.__name__`，不能依赖可能为空的 `str(e)`；相关函数有形参 `type`，禁止误写 `type(e)`。
- 图片 400：公开 URL 直传失败后原位转 base64 一次，再剥图重试；拉图超时可在当前轮静默剥图。网络类图片失败不清理历史。
- 内容审查依次尝试：同模型关流式 → 换 `model_mini`（保留图）→ 恢复原模型并剥图 → 失败兜底。审查失败不得清上下文。
- matcher 另处理空响应、空 assistant content、工具标记泄漏等有限重试。HTTP 200 返回的供应商拒绝文案也按失败处理，不能发群。

### 输出处理

- 流式按 `\n\n` 分段；工具轮增加分段预算，收尾残余缓冲也必须拆发，不得压成一条长消息。
- 发送和落库前统一清理 `<think>` 成对块及孤立标签。`think_leak_retry` 只在无 reasoning 且正文超阈值时原样重发一次；`strip_thought_prefix` 只压候选英文思考块，未确认时回放，避免吞正文。
- 发送/落库收口还负责 Markdown 降级、说话人前缀、首尾逗号、整行分隔线归一和括号噪音过滤。`ENABLE_MSG_TO_IMG` 时不剥 Markdown。
- 内部异常文本不得作为 assistant 历史、进入 prompt 或发群。

## Prompt、上下文与记忆

### 结构和触发

- 顺序：S1 角色/规则/工具与画图短规则 → 可选 extra_prompt → 可选摘要 → 当前状态 → 历史轮 → 本轮非触发上下文 → 尾部提醒/触发标记 → 触发 user。
- 响应规则集中在 `build_response_rules()` / `format_response_rules()`。公共规则只写跨模型共性，模型特有语癖放 profile `extra_prompt`。
- 只回应当前触发消息；`[群聊上下文-非触发消息]` 是背景。正常回复口语化、简短、不用 Markdown，不模仿工具摘要格式。
- `[当前触发]` 和 `[记忆提醒]` 只进请求、不落库；邮箱插入前移除旧触发标记，保留同条其他提醒。
- reply 文本以 `[回复 xxx 的消息]` 注入，reply 图片排在转发图片之前。合并转发由 `extract_forward_content()` 展开，遵守条数、字数、图片和 `image_offset` 限制；嵌套只留占位，失败不影响原消息。

### 历史、摘要和邮箱

- 轮数只计非 context_only 的 user。基础窗口 = `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE × CONTEXT_COMPRESS_THRESHOLD_RATIO)`。
- `CONTEXT_SUMMARY.pressure` 控制接近硬轮数/token 预算时的提前摘要；`idle` 控制会话收尾后的静默压缩。新触发取消空闲等待，进行中的空闲摘要不得覆盖已更新历史。
- 摘要只在完整回复边界切分，成功后按消息 `id()` 删除旧段；失败保留原文并冷却。软目标为 `max_summary_tokens` 或 `CONTEXT_SUMMARY.target_chars`，硬截 2 倍。
- 孤立历史清理使用计数而非布尔：user 开轮、普通 assistant 关轮，tool_calls 链保持开轮。裁剪、摘要、持久化必须共用该语义，以兼容邮箱的连续 user/assistant。
- 非触发消息按 chat_key 缓冲，下次触发时 flush 为 context_only；有过期时间和最少行数，图片保留并记录逐张元数据。context_only 存储为 system、渲染为 user、不持久化、不计轮数。
- 循环邮箱不取消旧任务：新触发落库后进入 `_loop_mailbox`，每个文本轮完成回复后合并插入批次 user 和临时提醒；终端阶段不插入，残留由收尾重新处理。
- `_on_reply_complete` 每个完成回复恰好一次，负责收尾、工具图、摘要、历史、统计和漫画兜底；MOSS 与 Anima 都视为已画图。

### 记忆、印象与人设

- 群记忆属于 `ChatData.chat_memory`，本群所有人格共享；个人记忆属于全局用户表，跨群跨人格共享。旧人格级记忆加载时合并，不再写回旧字段。
- `remember` 只记长期客观事实；性格由印象负责，话题由摘要负责，人物外观归 Anima 人设库。`rg reset` 只清上下文。
- 只注入当前触发者印象，同一用户同一上下文一次；印象 system 不持久化，随绑定轮删除后重建。
- 人设库只读，仅画图实际启用时按锚点触发句匹配；排除 bot 自己，限制条数，按上游版本去重。人设与印象共用前导 system，随绑定轮删除，不改写既有历史。

## 图片与视觉

- 图片留在原消息，不搬到触发句；文本使用 `[图片N]`，可见编号表经 `chat._visible_images` → `tg._visible_images` 供 vision、AnimeTrace 和 MOSS 使用（anima_reverse_tag 反推已于 2026-11 暂时停用）。
- 触发消息图片始终可见。历史图片有过期时间，非触发来源只取最近两条消息；超过 `MULTIMODAL_MAX_IMAGES` 时优先保留触发图，无触发图则保留最新历史图。
- `multimodal: false` 只剥请求中的 image_url，不破坏编号表；纯文本模型可通过 `vision` 获取文字描述。
- 公开 HTTP 图片优先直传；QQ 私有域、内网及视觉/识图工具使用 base64。下载失败不回退坏 URL。
- 图片按真实格式规范化，不受支持或超大时重编码/缩放；损坏图跳过，缩放失败继续使用原图。10MB 上限在缩放后判断。
- provider 失败图黑名单只在“单图且剥图后成功”时登记，避免多图误伤；命中后仅剥请求副本，不改文本编号和可见表。
- 第三方字段有体积限制时使用 `shrink_data_uri()`；不要把大图 base64 无压缩塞入请求。

## 协议适配

- 默认 OpenAI 兼容 `/chat/completions`。请求头保留自定义 User-Agent 和既有 provider 专用头逻辑。
- **Anthropic**：`anthropic_func.py` 保持对上层 OpenAI 同构。开头系统指令提升顶层，轮次资料转为原位 `[系统补充]` user 块；tool_result 位于合并 user 开头。无签名的 reasoning 不回传，adaptive thinking 签名必须置于 assistant content 最前。缓存断点逐块设置；usage 合并 fresh/cache creation/cache read。
- **Gemini**：`gemini_func.py` 使用原生 generateContent/streamGenerateContent。system 合并到 `systemInstruction`，tool 响应带函数名，图片强制 inline base64，schema 清洗不支持字段。`thought: true` 只转 reasoning，不进正文；`thoughtSignature` 必须在工具轮回传。maxOutputTokens 为思考留余量。
- 不要仅凭供应商历史横评修改协议；以当前代码、配置和可复现测试为准。

## 工具与画图

- `tavily_search` 是唯一搜索 schema，失败时内部回退博查。外部事实不确定时先搜索再回答。
- `browse_url` 按短链、SSR、Playwright、正文抽取等既有链路降级。小红书桌面/移动页面使用各自 UA，保留 `xsec_token`；配图经 `into_context` 注入，视频字幕失败不影响正文。
- AnimeTrace、vision 都按可见编号取图。`anima_reverse_tag` 反推工具已暂时停用（`llm_tool_plugins/__init__.py` 注释掉注册，恢复时同步还原 `get_draw_s1_rules` 文案）；参考图作画直接综合原图与用户需求调 generate_anima_image。
- **Anima**：启动动态拉取工作流和知识，`rg draw` 管理模式；任务编号必须告知，预计时间只顺带提及。漫画模式可使用 mini 模型空闲补画。
- **MOSS**：主模型只提供成品 `idea` 和参考/改图编号，`model_mini` 独立生成 Layout/tag 并提交。工具常驻暴露，群内开关和 `rg moss` 指令见 matcher。
- MOSS 参考图在任何 await 前快照编号，按任务独立落盘。改图走 VAE，固定 `edit_denoise=0.70`、步数至少 6；不要用 1.0，也不要靠放大分辨率解决文字问题。
- MOSS 提示词描述成品而非改动方案；只承诺足够大的关键文字，小字不转写为 elements。漫画分格和气泡按右到左、上到下。画幅由系统固定，改图只做 16 对齐。
- 画图工具结果不进原始历史，使用 `[作画记录]`；历史中的任务编号替换为对应工具占位符。

## 持久化、日志与指令

- 存档加载/保存过滤孤儿消息、工具原文、思考及内部异常；保存使用节流、串行锁和原子替换，shutdown 才用阻塞保存。
- 请求日志位于各数据根 `logs/{chat_key}.latest.json`，排查工具循环看 `loop_messages`；失败和摘要分别写 error/summary 日志。
- 请求最终失败通过 `error_gif.py` 发随机 gif；已被重试救回、已有半截回复或功能性指令错误不走该兜底。
- 常用指令包括 `rg list/set/draw/manga/model/nn/mem/nolimit/stat/reset/help` 和 `rg moss`。具体语法以 `command_func.py` 与 README 为准。
- `stats.py` 按日记录触发、token、缓存和工具计数，存档位置与当前昵称归档一致。

## 验证清单

- 对所有变更 Python 文件做语法检查；大范围变更编译核心模块与 `llm_tool_plugins/*.py`，运行相关 unittest。
- matcher 变更覆盖群聊/私聊、纯图片、reply/at/at all、忽略前缀、禁用会话和 `rg` 指令。
- 配置变更验证默认值、旧字段迁移、覆盖层回写和未知字段兼容；人格变更验证 `.md` 与 skill 文件夹。
- 上下文/图片变更用 latest 日志回放 `loop_messages`，检查图片就地编号、触发标记、完整工具链和缓存命中。
- 转发变更覆盖内联内容、API 获取、限制、编号、CQ 码和嵌套；失败兜底验证本地 gif 与 base64 路径。
- 多接入点变更至少验证单/多进程选择、工作目录、覆盖层和存档隔离；不在无凭据时假装完成真实平台联调。

# Dice! 插件

`ATRI/plugins/nonebot_plugin_dice/` 是 Dice! `2.7.1beta1(668)` 指令层移植。差异清单见 `docs/dice-migration.md`。

- `_msgdata.py`、`_tables.py` 是从原 C++ 源码提取的生成物，勿手改；文案覆盖放 `HELP_OVERRIDE` / `MSG_OVERRIDE`，未复刻指令从帮助剔除。
- 核心模块：`rd.py` 掷骰/COC，`card.py` 角色卡，`session.py` 游戏桌状态，`orders.py` 调度，`event.py` 消息游标，`formatter.py` 占位符，`decks.py` 牌堆，`store.py` 持久化。
- 单个 `on_message(priority=1, block=False)` 仅在实际处理带前缀指令时阻断传播；未命中不得吞消息。`.bot off` 后只保留 `.bot` / `.help`。
- 配置为 `data/dice/{config,messages,decks}.yml`，加载时读一次；`.system load` 重载，`.system save` 落盘，不在消息热路径探测 mtime。
- 自称和 master 按配置、环境变量、NoneBot 配置顺序解析，不写死。
- 群管、权限、mod、云黑、敏感词等不复刻；群内开关要求主人或群管理。未复刻功能不得出现在帮助索引。
- 保留 `.bot/.help/.jrrp/.me` 开关。真实服务端点 `api.kokona.tech` / `logpainter.kokona.tech` 不得修改。
- 避免 `store → card → formatter → names` 回环依赖；不要在 `constants` / `formatter` 顶层 import `store`。
