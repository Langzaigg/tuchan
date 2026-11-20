# 兔酱

基于 NoneBot2 + OneBot v11 的 QQ 群聊机器人。

核心功能由 `nonebot_plugin_naturel_gpt` 插件提供：以 LLM 驱动的人格化群聊 + 一整套原生 Tool Calling 工具集。

> 本项目早期基于 [Kyomotoi/ATRI](https://github.com/Kyomotoi/ATRI) 二次开发，遵循 GPLv3 协议开源。

## 核心：Naturel GPT 插件

- **人格化群聊**：人格从 `config/personas/` 热加载（`.md` 单文件 / skill 文件夹两种格式），运行中可按群切换；像真实群友一样自然分段回复
- **原生工具调用**（OpenAI-compatible Tool Calling）：
  - 联网搜索：Tavily（主）/ 博查（fallback）、Tavily Extract 网页正文提取
  - 网页抓取：短链还原、SSR、Playwright 渲染多策略链
  - 搜图识图：Pixiv、Danbooru、Bangumi 番组、AnimeTrace 以图识角色
  - AI 画图：ComfyUI Anima 工作流（动态发现、按群开关、漫画模式）
  - 长期记忆：群记忆 / 用户记忆，由模型自主维护
  - NAS 游戏目录查询（白名单群限定）
  - 视觉理解：纯文本模型可借助独立视觉模型"看"图
- **多模态输入**：群图片自动解析进上下文，内置异步图片缓存与门控策略
- **长期陪伴**：per-turn 用户印象、非触发消息缓冲、上下文压缩摘要，长对话不断片
- **多模型配置**：`OPENAI_PROFILES` 多组配置，按群运行时切换；流式分段回复

详细文档见 [ATRI/plugins/nonebot_plugin_naturel_gpt/README.md](ATRI/plugins/nonebot_plugin_naturel_gpt/README.md)。

## 其他内置插件

| 插件 | 功能 |
|------|------|
| `nonebot_plugin_repeater` | 复读 + 撤回还原提示 |
| `nonebot_plugin_fortune` | 每日运势 |
| `nonebot_plugin_analysis_bilibili` | B 站链接 / 小程序解析 |
| `nonebot_plugin_bilibilibot` | B 站动态、直播订阅推送 |
| `kalive` | 私有直播服务集成（开播通知、今日老婆、服务器状态）；地址、群号等敏感项全部走 `config.yml`，不入库 |
| `applet` / `essential` / `help` / `manage` / `status` / `util` / `broadcast` / `repo` | 小程序处理、基础部件、帮助、管理、状态、小工具等 |

## 运行

环境：Python 3.10+，以及一个 OneBot v11 协议端（go-cqhttp / NapCat / Lagrange 等）。

```bash
pip install -r requirements.txt
python main.py
```

首次运行前需要准备本地配置（均被 .gitignore 排除，不会提交）：

- `config.yml`：机器人基础配置（账号、超级用户、KaLive 等）
- `config/naturel_gpt_config.yml`：LLM 插件配置（API key、模型、工具开关等），缺失项会自动补默认值
- `config/personas/`：人格文件目录

### 多接入点与 QQ 官方机器人

在 `config.yml` 的 `BotEndpoints` 中配置 OneBot 和 QQ 官机，各自用 `enabled` 控制开关。运行 `python main.py` 会为每个启用的接入点启动独立进程。修改配置后重启生效。

```yaml
BotEndpoints:
  onebot:
    adapter: onebot
    enabled: true
    host: "0.0.0.0"
    port: 15701
    nickname: ["兔酱"]
  qq_official:
    adapter: qq
    enabled: false
    qq_app_id: "123456789"
    qq_app_secret_env: "ATRI_QQ_APP_SECRET"
    nickname: ["空酱"]
```

- `onebot` 使用 OneBot v11，按 `host` 和 `port` 监听 `/onebot/v11/ws`，并可运行内置 go-cqhttp。反向 WebSocket 令牌字段为 `onebot_access_token`。其他接入点的插件在 `data/endpoints/<接入点>/runtime/` 保存相对路径上的本地状态。
- `adapter: qq` 默认由 `nonebot-adapter-qq` 主动连接 QQ 官方 WebSocket 网关，无需配置 `host` 或 `port`。进程内接入层给现有插件分发 OneBot 事件。将 AppSecret 放入 `qq_app_secret_env` 指定的环境变量，或在本地 `config.yml` 填 `qq_app_secret`。若要使用 Webhook，设置 `qq_use_websocket: false`、`host` 和 `port`，并在 QQ 开放平台填写 HTTPS 回调 `/qq/webhook`。有群成员事件权限时可设置 `qq_group_members: true`。 [NoneBot QQ 适配器](https://github.com/nonebot/adapter-qq) · [QQ 官方事件文档](https://bot.q.qq.com/wiki/develop/api-v2/dev-prepare/interface-framework/event-emit.html)
- 需要监听的接入点可用 `ATRI_ENDPOINT` 为单个 `main:app` 进程选择配置。旧版 `BotSelfConfig` 会在缺少 `BotEndpoints.onebot` 时载入为 `onebot`。
- 官机群聊和私聊使用平台的 OpenID，接入层为旧插件生成稳定的数字 ID。官机的 `superusers` 支持填写 `qq:<用户 OpenID>`。旧插件的群号白名单、订阅目标等字段应填官机接入层的数字 ID。主动群发和好友审批仍受 QQ 官方权限及接口约束。
- QQ 推送的聊天记录正文和图片可在 Naturel GPT 中展开，包括直接发送和通过回复引用的合并转发。QQ 只推送引用索引时，接入层可复用同一会话仍在缓存中的原转发。 [QQ 官方群消息事件](https://bot.qq.com/wiki/develop/api-v2/autogen/event/group_message_create.html)

#### Naturel GPT 配置和存档

- 插件配置各接入点共用 `config/naturel_gpt_config.yml`；`config/bots/<首个昵称>/<接入点>.yml` 只是覆盖层，只写与主配置不同的键（如 `ADMIN_USERID`、独立的 `nickname`），未写的键启动时自动并自主配置。启动时会自动剔除覆盖层里与主配置同值的键和 NAS 共用字段；非默认接入点的 `ADMIN_USERID`、`NAS_GAME_WHITELIST_GROUPS` 始终显式保留。新会话按昵称顺序找同名人格，随后回退到 `DEFAULT_PERSONA` 和第一个可用人格。
- 会话存档和统计默认放在 `NG_DATA_PATH/archives/<首个昵称>/`。主接入点沿用现有 `NG_DATA_PATH` 作为根，其他接入点的根在 `data/endpoints/<接入点>/naturel_gpt/`。获取不到昵称时，使用各自的根目录作为默认存档。
- 新昵称存档首次创建时，可从默认旧档导入。匹配昵称的人格、对话、摘要和个人印象保留，其他人格按当前定义重新初始化。从同一接入点的默认旧档导入时，也保留对应会话的共享群记忆和已在该人格下出现过的用户的共享个人记忆。跨接入点导入时，记忆须能归到匹配人格。
- 在 Naturel GPT 自己的 YAML 写入 `nickname: ["兔酱"]`，重启后会优先使用这组昵称，并将会话和统计放回当前接入点的默认路径。移除该键可恢复按接入点昵称选存档。
- 各接入点共用画图记录库、NAS 资料路径和 NAS 同步记录。可在 `config.yml` 的 `NaturelGPTShared` 设置 `DRAW_DB_PATH`、`NAS_GAME_ROOT_PATH`、`NAS_GAME_UPLOAD_PATH`、`NAS_GAME_BASE_URL`、`NAS_GAME_SYNC_RECORDS_PATH`。默认沿用旧主配置的路径。NAS 群白名单仍由各机器人配置。

#### 超级用户

- `superusers` 授予当前接入点的 NoneBot 超级用户权限，例如 Naturel GPT 的 `rg chats` 和 `-global` 指令。管理、广播等命令也使用这份名单。Naturel GPT 配置内的 `ADMIN_USERID` 也能独立授予部分管理权限。
- `essential` 可就好友申请、入群邀请、机器人自身的群管理员变动、禁言与解禁私聊超级用户。在对应接入点设置 `notify_superusers: false` 可关闭这些通知并保留权限。
- Hikari 会在每次协议端连上时私聊超级用户，并在每天 12 点推送版本检查结果。在对应接入点设置 `hikari_enabled: false` 可关闭 Hikari 插件及其通知。Naturel GPT 的常规聊天和模型报错不会私聊超级用户。

## 目录结构

```text
ATRI/                   # 机器人框架与插件
└── plugins/
    └── nonebot_plugin_naturel_gpt/   # 核心 LLM 插件（含 llm_tool_plugins/ 工具集）
config/                 # 本地配置与人格（不提交）
data/                   # 运行数据（不提交）
main.py                 # 入口
```

## 声明

一切开发旨在学习，请勿用于非法用途。运行期间因行为违反当地法律法规而被处理的，本项目概不承担任何责任。

## 协议

[GPLv3](https://www.gnu.org/licenses/gpl-3.0.html)
