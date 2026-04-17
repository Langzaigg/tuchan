# Naturel GPT 插件使用说明

这是一个基于 NoneBot2 + OneBot v11 的群聊人格聊天插件。当前版本的大模型交互层直接调用 OpenAI-compatible API，支持流式响应、多模态图片输入、原生工具调用、动态人格加载和 `rg` 指令切换人格。

## 目录结构

插件代码目录：

```text
ATRI/plugins/nonebot_plugin_naturel_gpt/
```

运行数据和配置：

```text
config/naturel_gpt_config.yml
data/naturel_gpt/
```

内置 LLM 工具目录：

```text
ATRI/plugins/nonebot_plugin_naturel_gpt/llm_tool_plugins/
```

每个工具单独封装在一个 Python 文件中，由 `llm_tools.py` 统一注册和调度。

## 依赖

核心依赖：

```text
httpx
playwright
tiktoken
```

如果要使用 `browse_url` 浏览器抓取工具，还需要安装 Chromium：

```powershell
playwright install chromium
```

## 基础配置

主配置文件：

```text
config/naturel_gpt_config.yml
```

NoneBot 全局配置项：

```yaml
ng_config_path: config/naturel_gpt_config.yml
ng_dev_mode: false
```

大模型配置示例：

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
CHAT_MAX_SUMMARY_TOKENS: 512
```

说明：

- `CHAT_MODEL` 用于正常聊天。
- `CHAT_MODEL_MINI` 用于摘要和用户印象总结。
- `OPENAI_BASE_URL` 支持 OpenAI-compatible API。
- 插件内部直接调用 OpenAI-compatible API。

## 流式响应

```yaml
LLM_ENABLE_STREAM: true
LLM_SHOW_REASONING: false
```

- `LLM_ENABLE_STREAM` 控制是否边生成边处理回复。
- `LLM_SHOW_REASONING` 控制是否把模型返回的 `reasoning_content` 发送到聊天中。
- 群聊环境通常建议保持 `LLM_SHOW_REASONING: false`。

## 分段发送

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

## 多模态图片输入

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
- 图片 URL 必须能被模型服务访问。

## 工具调用

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
```

工具文件：

```text
llm_tool_plugins/pixiv_search.py
llm_tool_plugins/fetch_url.py
llm_tool_plugins/browse_url.py
llm_tool_plugins/bocha_search.py
```

新增工具时，建议新增独立 Python 文件，并在 `llm_tools.py` 的注册表中挂载。

### pixiv_search

用途：通过 Lolicon API 搜索 Pixiv 图片。

```yaml
LLM_TOOL_LOLICON_CONFIG:
  proxy: null
  r18: 0
  pic_proxy: null
  exclude_ai: true
```

### fetch_url

用途：使用普通 HTTP 客户端抓取网页文本。

```yaml
WEB_FETCH_TIMEOUT: 20
WEB_FETCH_MAX_CHARS: 6000
```

适合静态网页、API 文本和简单 HTML 页面。

### browse_url

用途：使用 Playwright 打开网页，等待浏览器渲染后读取页面可见文本。

```yaml
PLAYWRIGHT_TIMEOUT: 20
WEB_FETCH_MAX_CHARS: 6000
```

适合需要 JS 渲染的网页。使用前需要确保 Chromium 已安装。

### bocha_search

用途：调用博查搜索 API 联网搜索。

```yaml
BOCHA_API_KEY: ''
BOCHA_API_BASE: https://api.bochaai.com/v1/web-search
BOCHA_SEARCH_COUNT: 5
```

如果 `BOCHA_API_KEY` 为空，工具会返回未配置提示。

## 人格加载

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

### 单个 Markdown 人格

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

### Skill 文件夹人格

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

## rg 指令

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

## 数据文件

运行时聊天数据默认保存到：

```text
data/naturel_gpt/naturel_gpt.json
```

日志默认保存到：

```text
data/naturel_gpt/logs/
```

不要手动编辑运行时聊天数据，除非已经停止 Bot 并确认数据结构兼容。

## 迁移说明

- 旧版扩展系统已移除，不再使用 `NG_EXT_PATH`、`NG_ENABLE_EXT`、`NG_EXT_LOAD_LIST`。
- 不再依赖 `data/naturel_gpt/extensions/` 作为人格或扩展默认目录。
- 不再支持模型输出 `/#tool&args#/` 调用工具。
- 工具统一迁移到 `llm_tool_plugins/`，并通过原生工具调用执行。
- 人格统一从 `naturel_gpt_config.yml` 同级的 `personas` 子目录加载，可混放 `.md` 文件和 skill 文件夹。
