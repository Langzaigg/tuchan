<div align="center">

# 🐰 Naturel GPT

**基于 NoneBot2 + OneBot v11 的群聊人格聊天插件**

流式响应 · 多模态图片 · 原生工具调用 · 动态人格 · 智能上下文

</div>

---

## 🗺️ 快速导航

- [✨ 核心亮点](#-核心亮点)
- [🚀 快速开始](#-快速开始)
- [📖 详细说明](#-详细说明)
- [🗃️ 数据与迁移](#-数据与迁移)

---

## ✨ 核心亮点

| 特性 | 说明 |
|------|------|
| 🖼️ 多模态群聊 | 支持 OneBot v11 图片消息，图片作为 `image_url` 传给模型，内置异步图片缓存与上下文图片门控。 |
| 🛠️ 原生工具调用 | 使用 OpenAI-compatible Tool Calling，内置搜索、网页抓取、Pixiv、Bangumi 等工具，可通过 `LLM_DISABLED_TOOLS` 按需禁用。 |
| 🎭 动态人格系统 | 人格从 `config/personas/` 热加载，支持 `.md` 单文件和 skill 文件夹两种格式，运行时可切换。 |
| 🧠 智能上下文管理 | per-turn 触发者印象、非触发消息缓冲、上下文压缩摘要、按完整轮次裁剪、孤立消息清理，兼顾长对话连贯性与 prompt 稳定性。 |
| ⚙️ 多 OpenAI 配置 | 支持 `OPENAI_PROFILES` 多 profile，每个会话可独立切换 active_profile，单 profile 可设置模型专用 `extra_prompt` 调优。 |
| ⚡ 流式分段发送 | 边生成边按双换行分段发送，自动过滤 Markdown，更像真实群聊。 |
| 📝 调试与可观测性 | 每次 LLM 请求、摘要任务、错误路径都会保存结构化日志到 `data/naturel_gpt/logs/`，方便排查。 |

> 💡 设计目标：在维持长对话连贯性的同时，让 prompt 前缀尽量稳定、避免重复注入和旧信息残留，从而提升缓存命中率并降低 token 浪费。

---

## 🚀 快速开始

### 环境要求

- Python 3.10+
- 已部署 NoneBot2 框架
- 已配置 OneBot v11 适配器

### 安装依赖

```bash
pip install httpx playwright tiktoken
```

若使用 `browse_url` 浏览器抓取工具，还需要安装 Chromium：

```bash
playwright install chromium
```

### 部署方式

- 直接运行本仓库作为 NoneBot 项目（`main.py` 为入口）。
- 或将 `nonebot_plugin_naturel_gpt/` 目录复制到你的 NoneBot 项目的 `plugins/` 目录下，确保依赖和配置路径正确。

### 配置文件

1. 在 NoneBot 全局配置中指定插件配置文件路径：

```yaml
ng_config_path: config/naturel_gpt_config.yml
ng_dev_mode: false
```

2. 创建 `config/naturel_gpt_config.yml`，至少包含：

```yaml
OPENAI_API_KEYS:
  - sk-xxx
OPENAI_BASE_URL: https://api.openai.com/v1
OPENAI_PROXY_SERVER: ''
OPENAI_TIMEOUT: 60
CHAT_MODEL: gpt-4o
CHAT_MODEL_MINI: gpt-4o-mini
CHAT_TEMPERATURE: 0.4
REPLY_MAX_TOKENS: 1024
CHAT_MAX_SUMMARY_TOKENS: 800
```

- `CHAT_MODEL`：正常聊天模型。
- `CHAT_MODEL_MINI`：摘要和印象总结模型。
- `OPENAI_BASE_URL`：支持任意 OpenAI-compatible API。
- 更多配置项见下文“基础配置”。

3. 把人格文件放到 `config/personas/`。

4. 启动 NoneBot，群里 @ 机器人或私聊发送任意消息即可开始对话。

---

## 📖 详细说明

### 📂 目录结构

插件代码：

```text
ATRI/plugins/nonebot_plugin_naturel_gpt/
```

运行配置：

```text
config/naturel_gpt_config.yml
config/personas/
```

运行数据：

```text
data/naturel_gpt/
```

工具目录：

```text
ATRI/plugins/nonebot_plugin_naturel_gpt/llm_tool_plugins/
```

每个工具单独封装在一个 Python 文件中，由 `llm_tools.py` 统一注册和调度。启动时可通过 `LLM_DISABLED_TOOLS` 列表跳过指定工具。

### ⚙️ 基础配置

大模型基础配置示例：

```yaml
OPENAI_API_KEYS:
  - sk-xxx
OPENAI_BASE_URL: https://api.openai.com/v1
OPENAI_PROXY_SERVER: ''
OPENAI_TIMEOUT: 60
CHAT_MODEL: gpt-4o
CHAT_MODEL_MINI: gpt-4o-mini
CHAT_TEMPERATURE: 0.4
REPLY_MAX_TOKENS: 1024
CHAT_MAX_SUMMARY_TOKENS: 800
```

说明：

- `CHAT_MODEL`：用于正常聊天。
- `CHAT_MODEL_MINI`：用于摘要和用户印象总结。
- `OPENAI_BASE_URL`：支持任意 OpenAI-compatible API。

### 🔀 多 OpenAI 配置（可选）

插件支持多组配置，通过 `OPENAI_PROFILES` 管理：

```yaml
OPENAI_ACTIVE_PROFILE: default
OPENAI_PROFILES:
  default:
    OPENAI_API_KEYS:
      - sk-xxx
    OPENAI_BASE_URL: https://api.openai.com/v1
    CHAT_MODEL: gpt-4o
    CHAT_MODEL_MINI: gpt-4o-mini
    extra_prompt: ''
  kimi:
    OPENAI_API_KEYS:
      - sk-yyy
    OPENAI_BASE_URL: https://api.moonshot.cn/v1
    CHAT_MODEL: kimi-k2
    extra_prompt: '你是 Kimi，请保持简洁。'
```

- `OPENAI_ACTIVE_PROFILE`：默认激活的 profile。
- 每个会话可独立设置 active_profile，运行时自动切换。
- `extra_prompt`：模型专用追加提示词，会注入到 System 2 末尾，用于特定模型调优。
- `no_think`：`true` 时在响应规则中注入 `/no_think` 指令，用于显式关闭模型思考（默认 `false`）。
- `keep_reasoning`：`true` 时跨轮持久化历史中的 `reasoning_content` 随请求发送（默认 `false`，provider 400 拒绝时自动剥离重试一次）；同一轮工具循环内的思考链始终保留。
- 旧版扁平键（如 `OPENAI_API_KEYS`、`CHAT_MODEL`）会自动迁移为 `default` profile。

### ⚡ 流式响应

```yaml
LLM_ENABLE_STREAM: true
LLM_SHOW_REASONING: false
```

- `LLM_ENABLE_STREAM`：控制是否边生成边处理回复。
- `LLM_SHOW_REASONING`：控制是否把模型返回的 `reasoning_content` 发送到聊天中。
- 群聊环境通常建议保持 `LLM_SHOW_REASONING: false`。

### ✂️ 分段发送

```yaml
NG_ENABLE_MSG_SPLIT: true
REPLY_SEGMENT_INTERVAL: 1.0
REPLY_MAX_SEGMENTS: 5
```

当前分段规则：

- 不再使用旧版 `*;` 特殊符号分段。
- 检测到双换行 `\n\n` 时自动切成一段发送。
- 每段之间至少等待 `REPLY_SEGMENT_INTERVAL` 秒。
- 最多发送 `REPLY_MAX_SEGMENTS` 段。
- 流式过程中如果超过分段上限，会继续接收完剩余内容，然后作为最后一段发送。

回复后处理：

- 同一段内多余双换行会压缩为单换行。
- 会过滤常见 Markdown 语法，包括代码块、标题、列表标记、粗体、链接等。
- 系统提示词要求模型像真实群聊一样说话，不写文章，不频繁分段，不使用 Markdown。

### 🖼️ 多模态图片输入

```yaml
MULTIMODAL_ENABLE: true
MULTIMODAL_HISTORY_LENGTH: 4
MULTIMODAL_MAX_MESSAGES_WITH_IMAGES: 2
```

- 插件会读取 OneBot v11 `image` 消息段中的图片 URL。
- 图片会作为 OpenAI-compatible 的 `image_url` 内容传给模型。
- `MULTIMODAL_HISTORY_LENGTH` 控制图片可进入上下文的聊天记录视野长度。
- `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 控制最多保留几条带图片的消息，并且始终从最近输入开始保留。
- 如果 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES` 设置为 `0`，不会保留历史图片消息。

注意：

- 模型本身必须支持视觉输入。
- 图片 URL 必须能被模型服务访问；插件会自动把 QQ 等私有 URL 下载缓存为 `data:image/...` base64 格式。

### 🛠️ 工具调用

```yaml
LLM_ENABLE_TOOLS: true
LLM_MAX_TOOL_ROUNDS: 3
LLM_TOOL_LOOP_MAX_SECONDS: 180  # 工具调用循环总耗时上限（秒），超时进入终端收尾轮
LLM_DISABLED_TOOLS: []  # 按模块名禁用指定工具，如 pixiv_search
```

插件使用原生工具调用，不再支持旧版 `/#tool&args#/` 文本协议，也不再加载旧扩展系统。

内置工具（`llm_tool_plugins/` 目录，每个工具一个文件）：

| 工具 | 用途 |
|------|------|
| `tavily_search` | Tavily 联网搜索（唯一暴露的搜索工具；Tavily 失败时内部回落博查） |
| `browse_url` | 网页抓取（短链还原 → SSR → Playwright 渲染 → trafilatura → Tavily 服务端代抓兜底） |
| `pixiv_search` | Lolicon API 搜索 Pixiv 图片 |
| `danbooru_search` | Danbooru 标签检索（画图提示词辅助） |
| `bangumi` | Bangumi 番组数据库（单工具 action 分发：search_subject / get_subject / search_character / search_person / calendar） |
| `anime_trace` | AnimeTrace 以图识角色 |
| `generate_anima_image` | ComfyUI Anima AI 画图（`anima_generate.py`） |
| `memory` | 长期记忆（群 / 用户 scope，模型自主维护） |
| `nas_game_list` | NAS 游戏目录查询（白名单群限定） |
| `vision` | 视觉理解：纯文本模型借助独立视觉模型看图 |

新增工具时，建议新增独立 Python 文件，并在 `llm_tools.py` 的注册表中挂载。

#### tavily_search

用途：调用 Tavily API 联网搜索，主搜索工具。

```yaml
TAVILY_API_KEY: []  # 支持多 key，启动时自动选用剩余额度最多的 key
```

配置 key 后自动注册；Tavily 调用失败时自动经内部 fallback 调用博查搜索（不注册独立工具）；仅配置 `BOCHA_API_KEY` 时也会注册本工具并直通博查。

#### bocha_search（内部 fallback）

用途：博查搜索 API，作为 Tavily 的服务端内部 fallback，不注册独立工具 schema。

```yaml
BOCHA_API_KEY: ''
BOCHA_API_BASE: https://api.bochaai.com/v1/web-search
BOCHA_SEARCH_COUNT: 20
```

单次搜索结果数强制为 10-20 条。

#### browse_url

用途：多策略网页抓取：短链还原 → 已知社交平台 SSR → Playwright 渲染 → trafilatura 正文提取 → Tavily Extract 服务端代抓（最终兜底，反爬 / JS 渲染失败场景，需配置 Tavily key）。

```yaml
WEB_FETCH_TIMEOUT: 20
WEB_FETCH_MAX_CHARS: 6000
PLAYWRIGHT_TIMEOUT: 20
```

使用 Playwright 策略前需安装 Chromium（`playwright install chromium`）；trafilatura 为软依赖，未安装自动跳过。

#### pixiv_search

用途：通过 Lolicon API 搜索 Pixiv 图片。

```yaml
LLM_TOOL_LOLICON_CONFIG:
  proxy: null
  r18: 0
  pic_proxy: null
  exclude_ai: true
```

#### danbooru_search

用途：把角色名 / 视觉概念落实成准确的 Danbooru 标签，供画图任务使用。优先国内直连魔搭创空间，失败回落 HuggingFace Space（走 `TOOL_PROXY`）。

```yaml
TOOL_PROXY: ''
```

#### bangumi_search

用途：调用 Bangumi API 搜索动画、书籍、游戏等条目，以及角色和人物信息。

```yaml
BANGUMI_ACCESS_TOKEN: ''
```

如果 `BANGUMI_ACCESS_TOKEN` 为空，工具不会加载。

#### anime_trace

用途：调用 AnimeTrace 开放 API 以图识角色，`image_index` 引用当前对话中的图片。依赖 `MULTIMODAL_ENABLE: true`，无需额外配置。

#### generate_anima_image（anima_generate.py）

用途：调用 ComfyUI Anima 服务生成图片。可选工作流在启动时从服务端动态发现；`rg draw` 按群控制开关与模型，`rg manga` 控制漫画模式。

```yaml
COMFYUI_BASE_URL: http://127.0.0.1:8188
COMFYUI_ENABLED: false
MANGA_IDLE_MINUTES: 5
MANGA_IDLE_ROUNDS: 5
```

#### memory

用途：长期事实记忆。按群（`group` scope）或用户（`user` scope）保存 / 删除 / 批量整理重要事实，与人格关联隔离。无需额外配置。

#### nas_game_list

用途：查询 NAS 上的游戏合集目录并生成下载链接，仅在白名单群暴露。

```yaml
NAS_GAME_ROOT_PATH: ''
NAS_GAME_UPLOAD_PATH: ''
NAS_GAME_BASE_URL: ''
NAS_GAME_WHITELIST_GROUPS: []
NAS_GAME_SYNC_RECORDS_PATH: ''  # 同步服务记录文件，为空则不读取
```

#### vision

用途：主模型为纯文本模型（profile `multimodal: false`）时，注入 `vision` 工具，让主模型调用独立视觉模型理解对话中的图片。

```yaml
# 在 OPENAI_PROFILES 的对应 profile 中配置：
model_vision: mimo            # 视觉模型名
model_vision_base_url: ''     # 可选，默认复用 profile 的 base_url
model_vision_api_keys: []     # 可选，默认复用 profile 的 api_keys
model_vision_max_tokens: 0    # 可选
```

### 🎭 人格加载

人格不再从配置文件 `PRESETS` 手写加载。`PRESETS` 在配置文件中会保持为空，仅作为运行时动态人格承载字段。

当前支持两类人格来源：

- 单个 Markdown 人格文件。
- 固定格式 skill 人格文件夹。

人格加载目录固定为 `naturel_gpt_config.yml` 所在目录下的 `personas` 子文件夹。默认配置下就是：

```text
config/personas/
```

同一个目录中可以混放 `.md` 单文件人格和 skill 形式的人格文件夹，不需要额外配置路径。

默认人格通过配置文件中的 `DEFAULT_PERSONA` 指定：

```yaml
DEFAULT_PERSONA: SOUL
```

如果 `DEFAULT_PERSONA` 为空或名称不存在，会使用扫描到的第一个人格；如果没有扫描到任何人格，会使用内置 `default` 人格。

#### 单个 Markdown 人格

规则：

- `.md` 文件会直接全文作为人格提示词。
- 人格名称取文件名，不含扩展名。

示例：

```text
config/personas/SOUL.md
```

加载后人格名为：

```text
SOUL
```

#### Skill 文件夹人格

规则：

- 整个文件夹作为一个人格输入。
- 人格名称取文件夹名中第一个 `-` 之前的部分。

示例：

```text
小春-skill-main
```

加载后人格名为：

```text
小春
```

固定读取顺序：

```text
SKILL.md
soul.md
limit.md
resource/behavior_guide.md
resource/key_life_events.md
resource/relationship_dynamics.md
resource/speech_patterns.md
```

`SKILL.md` 会过滤顶部 YAML front matter 和通用激活模板，例如 `Roleplay Rules`、语言规则、退出角色扮演、默认激活、激活方式等。其它文件按固定顺序完整注入系统提示词。

### 🎮 rg 指令

人格会在以下场景动态刷新：

- 插件加载配置时。
- 执行 `rg`。
- 执行 `rg list`。
- 执行 `rg set <人格名>`。
- 执行 `rg query <人格名>`。

常用指令：

```text
rg                      # 人格列表与状态
rg list                 # 列出可用人格
rg set <人格名>          # 切换人格
rg query <人格名>        # 查看人格详情
rg reload_config        # 重载配置
rg reset                # 清空当前会话上下文（保留记忆/印象/昵称）
rg model [profile]      # 查看/切换本群 OpenAI profile
rg nn [昵称]            # 自定义昵称（查询/设置/清除）
rg mem                  # 查看记忆；rg mem clear <group|user|all> 清除
rg draw [force|on|auto|off]  # AI 画图开关
rg draw <模型名>         # 切换画图工作流
rg draw-XXXXXX          # 查询画图任务提示词
rg manga [on|off|画风]   # 漫画模式
rg nolimit [on|off]     # 内容限制解锁（每群独立）
rg stat                 # 当日运行统计；rg stat reset 清空（管理员）
rg help                 # 帮助
```

`rg` 和 `rg list` 会展示当前可用人格列表。新增或修改人格文件后，通常不需要重启 Bot，直接执行 `rg` 或 `rg set <人格名>` 即可触发动态加载。

### 🧠 上下文管理设计

本插件的上下文管理核心目标是：**在维持长对话连贯性的同时，让 prompt 前缀尽量稳定、避免重复注入和旧信息残留，从而提升缓存命中率并降低 token 浪费**。

#### 四层系统消息结构

最终发送给模型的对话上下文由 4 条头部系统消息 + 结构化历史消息 + 尾部记忆提醒（可选）组成：

1. **System 1 — 角色与响应规则**  
   人格设定、基础响应规则、工具基础规则、画图行为短规则。这条消息最稳定，用于最大化 prompt 缓存命中。

2. **System 2 — 模型专用追加提示词**  
   仅当前 profile 配置了 `extra_prompt` 时注入；画图知识常驻 `generate_anima_image` 的 schema description，不再占用系统消息。

3. **System 3 — 压缩上下文摘要**  
   会话级变化的摘要，侧重话题连贯性与群历史，格式固定为：
   - `[当前话题]`：当前讨论焦点、进展和未决问题，体现话题如何演变。
   - `[群历史]`：按日期（到天）记录的高信号群事件、共同约定和关键决策。

4. **System 4 — 当前状态**  
   `[当前状态]`：群记忆 + 当前日期。低频变化内容放头部：平时整段历史都能命中前缀缓存，仅记忆变更或跨天时失效一次。

尾部 **记忆提醒**（`[记忆提醒]`，仅群记忆接近上限时出现）单独注入在触发消息之前（本轮 flush 的 context_only 之后）：其出现与否取决于记忆条数阈值，放头部会让阈值两侧各打穿一次前缀缓存，故放尾部；且行动指令离触发消息更近，模型更容易照做。

#### 记忆链路三层分工

- **上下文摘要**（System 3）：会话/群层面。侧重当前话题的连贯性与群历史（事件、约定、决策）。
- **用户印象**（per-turn system）：用户个人层面。侧重性格、爱好、习惯、偏好倾向与互动模式，随触发轮注入。
- **remember 记忆工具**（头部 System 4 `[群记忆]` / 印象内 `[你的记忆]`）：长期事实层面。只记重要且长期有效的客观事实（称呼、生日、规则约定）和用户主动要求记住的内容。

三层的生成/使用提示词各自声明了职责边界，并且摘要与印象生成时会把已保存的记忆内容一并交给模型参考，尽量避免同一信息在多层重复记录。

#### 历史消息结构

一条真实的历史对话轮按顺序组织为：

```text
[system 触发者印象（仅该用户首次出现时）]
[user 触发句]
[assistant 回复]
[system 工具摘要（可选）]
```

- **用户印象只注入触发者**：固定只注入触发该轮对话的用户的印象，不额外注入被提到的其他用户。
- **同一用户印象只注入一次**：同一 `user_id` 的印象在整个当前上下文中只注入一次，绑定到该用户首次触发的轮次，避免重复和缓存抖动。
- **印象与轮次同步**：印象 system 消息跟随其所属 user 轮次一起进入上下文，并在该轮次被裁剪时一起移除；触发者下一次触发时，会按 `chat_impressions` 中的最新印象重新注入，避免旧印象残留。
- **非触发消息缓冲**：不需要回复的群消息不写入 `prompt_messages`，而是先进入 `_recent_context_buffers` 临时缓冲区；当下一条触发消息到达时，以 `context_only` system 消息 flush 到触发句之前。`context_only` 为 append-only：每轮 flush 追加一条、不清旧，随所在区间被裁剪/摘要一并淘汰。

#### 上下文压缩与摘要

- 当真实对话轮数超过 `CONTEXT_WINDOW_SIZE + CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO` 时，触发异步摘要。
- 摘要只针对**溢出的完整轮次**生成；生成成功后删除这些完整轮次（`context_only` 消息不豁免，随溢出区间一并删除）。
- 摘要 prompt 按职责分层，与其它记忆层互不重复：
  - 摘要只记会话/群层面内容：当前话题进展与演变、群事件、约定和决策。
  - 性格、兴趣、说话风格等个人特质属于 `[用户印象]` 的职责，不重复记录。
  - 称呼、生日等长期事实属于 remember 记忆工具的职责；已保存的群记忆会一并交给摘要模型，避免重复。
  - 新信息覆盖旧摘要中重复或过时的部分，避免同一件事反复累积。
- 摘要字数软目标由 `CONTEXT_SUMMARY_TARGET_CHARS`（默认 800）控制，profile 的 `max_summary_tokens` 可覆盖；硬截断为软目标的 2 倍，防止摘要无限膨胀。

#### 用户印象更新

- 用户印象随上下文压缩任务一并更新。
- 印象侧重用户个人层面：性格与说话风格、爱好与习惯、偏好倾向、与 Bot 的关系和互动模式；不记群话题事件（摘要职责），也不重复已保存的用户记忆（生成时会附上该用户已保存的记忆供参考）。
- 印象字数软目标由 `IMPRESSION_TARGET_CHARS`（默认 200）控制，硬截断为软目标的 2 倍。
- 只使用**本次溢出轮次中**该用户的对话内容 + 旧印象进行总结，不更新所有有历史的用户。
- 摘要裁剪删除溢出轮次（含其前导印象 system）后，下次该用户触发时上下文中已无其旧印象，自然注入更新后的新印象，避免重复和残留。

#### 裁剪与孤立清理

- 所有上下文裁剪都按**完整轮次**进行：从最旧的非触发 user 开始，连同其 assistant、tool 消息、工具摘要 system 一起删除；遇到下一轮 user 的前导印象则停止收集，确保印象 system 跟随其所属轮次。
- `context_only` 消息不参与轮数统计；append-only 追加，裁剪/摘要时不豁免、随所在区间一并删除（当轮新 flush 的 context_only 位于最新触发轮之前，随该轮存续）。
- 清理孤立 assistant/tool 消息，避免历史中出现没有真实 user 承接的 assistant 或没有对应 assistant 调用的 tool 结果。

---

## 🗃️ 数据与迁移

### 数据文件

运行时聊天数据默认保存到：

```text
data/naturel_gpt/naturel_gpt.json
```

日志默认保存到：

```text
data/naturel_gpt/logs/
```

- `.latest.json`：每次 LLM 请求的请求与响应快照。
- `.summary.json`：每次摘要任务的请求与响应。
- `.error.json`：请求失败时的 prompt 快照。

不要手动编辑运行时聊天数据，除非已经停止 Bot 并确认数据结构兼容。

### 迁移说明

- 旧版扩展系统已移除，不再使用 `NG_EXT_PATH`、`NG_ENABLE_EXT`、`NG_EXT_LOAD_LIST`。
- 不再依赖 `data/naturel_gpt/extensions/` 作为人格或扩展默认目录。
- 不再支持模型输出 `/#tool&args#/` 调用工具。
- 工具统一迁移到 `llm_tool_plugins/`，并通过原生工具调用执行。
- 人格统一从 `naturel_gpt_config.yml` 同级的 `personas` 子目录加载，可混放 `.md` 文件和 skill 文件夹。
