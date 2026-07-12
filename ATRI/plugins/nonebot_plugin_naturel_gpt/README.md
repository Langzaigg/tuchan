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
```

插件使用原生工具调用，不再支持旧版 `/#tool&args#/` 文本协议，也不再加载旧扩展系统。

内置工具：

```text
pixiv_search
fetch_url
browse_url
bocha_search
bangumi_search
```

工具文件：

```text
llm_tool_plugins/pixiv_search.py
llm_tool_plugins/fetch_url.py
llm_tool_plugins/browse_url.py
llm_tool_plugins/bocha_search.py
llm_tool_plugins/bangumi_search.py
```

新增工具时，建议新增独立 Python 文件，并在 `llm_tools.py` 的注册表中挂载。

#### pixiv_search

用途：通过 Lolicon API 搜索 Pixiv 图片。

```yaml
LLM_TOOL_LOLICON_CONFIG:
  proxy: null
  r18: 0
  pic_proxy: null
  exclude_ai: true
```

#### fetch_url

用途：使用普通 HTTP 客户端抓取网页文本。

```yaml
WEB_FETCH_TIMEOUT: 20
WEB_FETCH_MAX_CHARS: 6000
```

适合静态网页、API 文本和简单 HTML 页面。

#### browse_url

用途：使用 Playwright 打开网页，等待浏览器渲染后读取页面可见文本。

```yaml
PLAYWRIGHT_TIMEOUT: 20
WEB_FETCH_MAX_CHARS: 6000
```

适合需要 JS 渲染的网页。使用前需要确保 Chromium 已安装。

#### bocha_search

用途：调用博查搜索 API 联网搜索。

```yaml
BOCHA_API_KEY: ''
BOCHA_API_BASE: https://api.bochaai.com/v1/web-search
BOCHA_SEARCH_COUNT: 20
```

如果 `BOCHA_API_KEY` 为空，工具会返回未配置提示。单次搜索结果数强制为 10-20 条。

#### bangumi_search

用途：调用 Bangumi API 搜索动画、书籍、游戏等条目，以及角色和人物信息。

```yaml
BANGUMI_ACCESS_TOKEN: ''
```

如果 `BANGUMI_ACCESS_TOKEN` 为空，工具不会加载。

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
rg
rg list
rg set <人格名>
rg query <人格名>
rg reload_config
```

`rg` 和 `rg list` 会展示当前可用人格列表。新增或修改人格文件后，通常不需要重启 Bot，直接执行 `rg` 或 `rg set <人格名>` 即可触发动态加载。

### 🧠 上下文管理设计

本插件的上下文管理核心目标是：**在维持长对话连贯性的同时，让 prompt 前缀尽量稳定、避免重复注入和旧信息残留，从而提升缓存命中率并降低 token 浪费**。

#### 四层系统消息结构

最终发送给模型的对话上下文由 4 条系统消息 + 结构化历史消息组成：

1. **System 1 — 角色与响应规则**  
   人格设定、基础响应规则、工具基础规则。这条消息最稳定，用于最大化 prompt 缓存命中。

2. **System 2 — 条件知识**  
   根据当前状态注入：Anima 绘画技能（`force`/`on`/`auto` 或漫画模式）、模型专用追加提示词（`extra_prompt`）。只在需要时追加，不影响 System 1 的稳定性。

3. **System 3 — 记忆与日期**  
   群记忆、用户记忆、记忆提醒、当前日期。

4. **System 4 — 压缩上下文摘要**  
   会话级变化的摘要，格式固定为：
   - `[上下文摘要]`：会话级事实、共识、未完成的讨论和需要后续记住的信息。
   - `[时间线]`：按日期（到天）记录的高信号事件、话题转折、共同约定和关键决策。

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
- **非触发消息缓冲**：不需要回复的群消息不写入 `prompt_messages`，而是先进入 `_recent_context_buffers` 临时缓冲区；当下一条触发消息到达时，以 `context_only` system 消息 flush 到触发句之前。

#### 上下文压缩与摘要

- 当真实对话轮数超过 `CONTEXT_WINDOW_SIZE + CONTEXT_WINDOW_SIZE * CONTEXT_COMPRESS_THRESHOLD_RATIO` 时，触发异步摘要。
- 摘要只针对**溢出的完整轮次**生成；生成成功后删除这些完整轮次（`context_only` 消息保留）。
- 摘要 prompt 要求：
  - 与 `[用户印象]` 互补：性格、兴趣、说话风格等个人层面信息不重复。
  - 保留会话/群层面的事实、事件、共识、待办。
  - 时间线按日期到天记录，只保留高信号事件，不记录每一句闲聊。
  - 新信息覆盖旧摘要中重复或过时的部分，避免同一件事反复累积。
- 摘要软限制由 `CHAT_MAX_SUMMARY_TOKENS` 控制，硬限制为 `max(1000, 软限制 * 2)`，防止摘要无限膨胀。

#### 用户印象更新

- 用户印象随上下文压缩任务一并更新。
- 只使用**本次溢出轮次中**该用户的对话内容 + 旧印象进行总结，不更新所有有历史的用户。
- 摘要裁剪删除溢出轮次（含其前导印象 system）后，下次该用户触发时上下文中已无其旧印象，自然注入更新后的新印象，避免重复和残留。

#### 裁剪与孤立清理

- 所有上下文裁剪都按**完整轮次**进行：从最旧的非触发 user 开始，连同其 assistant、tool 消息、工具摘要 system 一起删除；遇到下一轮 user 的前导印象则停止收集，确保印象 system 跟随其所属轮次。
- `context_only` 消息不参与轮数统计，裁剪时保留，确保非触发上下文在窗口滑动时不丢失。
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
