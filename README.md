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
