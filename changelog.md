# 更新日志

此处仅记录重大更新，细节请关注 commit 记录。

## Oct 24, 2026

- MOSS 改图定案：VAE 路径 + 固定参数 + 「成品描述」式提示词
  - **路径**：三组同底图对照（换角色、评论区改名+换头像、黑白漫画换角色+改三格）——VAE 保住构图/包装文字/UI 版式/分镜；TE 会把原文当图案重新编造（用户名碎片混进正文、点赞数错乱）。改图默认 `edit_image`，TE 只用于「照着这张画一张新的」
  - **参数不暴露**：`edit_denoise` 固定 0.70、改图步数下限 6，两者都不进主模型 schema，也不让摘要模型输出（多输出也忽略）；画布只做 16 对齐，**不放大分辨率**——用手写固定提示词对照后确认原生尺寸就能把用户名/正文/点赞数渲染对，之前「必须放大 1.5×」是摘要模型每次重写提示词造成的假象
  - **提示词**：摘要模型的框架是「那张图只是参照物，输出目标成图本身的描述」，禁止「保留/不变/替换/改成/原本/原图」这类改动口吻；主模型 `idea` 同样要求直接描述成品。另加 `[构思是唯一准绳]`（构思优先于参照图、点名项不许漏/换/弱化、不许新增主体或整条内容）与硬性框位规则（一文本块一框、框高≈字号 1.5 倍、徽章并进相邻文本框）
  - **稳定性**：摘要模型与主对话共享 `reasoning_effort`（`openai_func` 不再对 summarize/impression 屏蔽思考预算）；改图首版没给 elements 时带一句提醒重试一次
  - **知识库只留硬规则**：`moss_expert.md` 删掉调参经验与实测叙述，只保留字段规范、Layout 坐标、分格写法（含「阅读顺序从右往左」）、参考图/改图两条路径与文字框规则

## Oct 22, 2026

- 新增 MOSS 画图通道（上游独立接口 `/moss/*`，Layout JSON + 画面内文字渲染）：作为独立工具 `generate_moss_image` 接入，主模型只给画面构思，提示词由摘要模型在 MOSS 画图知识下独立撰写后调后端出图，MOSS 的排版/tag 知识不进主模型上下文
- 触发：工具常驻暴露（描述里约束时机：提到 moss，或需要画面内文字/分格/海报排版）；`rg moss <画面描述>` 指令直接作画，可带参考图；`rg moss on|off` 按群关闭聊天触发（关闭后只能用指令）；`rg draw force` 的群在「moss + 画/p/图/换」时注入临时 system 提示强制调用 MOSS
- 参考图走 MOSS 文本编码器视觉分支（本机落盘后传绝对路径，最多 4 张）；bot 人格 + 人设库外观一并注入摘要模型；本群解锁内容限制时把「内容尺度」规则（`MOSS_UNLOCK_RULES`）也注入摘要模型，与 anima 漫画兜底同口径
- 新增 VAE 改图路径（`edit_image` / `edit_denoise`）：p 图/换装/微调走 `LoadImage → ImageScale → VAEEncode` 初始 latent，按原图比例取 16 的倍数；实测同图换角色能原样保留构图与包装文字，明显优于纯 TE 参考；工具增加 `edit_image` 参数，`rg moss` 单图 + 改图意图时自动走该路径
- 采样默认步数改为从 `/moss/schema` 读取（上游已改 4 步 `res_multistep`），摘要模型输出切到新版混合写法（tag 字段 + `nltags`）
- 参考图落盘改为每任务独立目录 + 来源日志（编号/URL/md5），修复跨群偶发串图
- 与 anima 一致的交互：返回 `moss-XXXXXX` 任务编号（不提预计耗时），画完自动发图并带编号；调用过 moss 即视为已画，不会再触发漫画/强制画图兜底；工具结果与调用记录在历史中抹除（`[作画记录]` + 编号占位符），避免污染上下文
- 新增配置 `MOSS_ENABLE`（默认开启）；上游 `/moss/*` 不可用时自动跳过注册

## Sep 4, 2026

- 修复循环邮箱批次回复丢失：历史轮次不变量改为计数（`user(A) user(B) assistant(答A) assistant(答B)` 合法），批次回复的用户维度历史归属到批次内各用户
- 图片方案重做：就地保留（不再注入触发消息、不再关键词门控）、统一 1 小时有效期按 30 分钟量化批量过期、容量滞后回收（`MULTIMODAL_MAX_IMAGES`）、渲染层全局编号，工具按同一编号查可见图片表
- 群聊上下文块改为 user 角色就地携带图片，flush 时按时间衰减（`CONTEXT_BUFFER_MAX_AGE_MINUTES` / `CONTEXT_BUFFER_MIN_LINES`）
- 提示词：背景块与触发消息定位规则、尾部 `[当前触发]` 标记、邮箱批次合并为一条消息并要求逐条分段回应、段数与 `REPLY_MAX_SEGMENTS` 对齐
- 配置：移除 `MULTIMODAL_MAX_MESSAGES_WITH_IMAGES`，新增 `MULTIMODAL_MAX_IMAGES`、`CONTEXT_BUFFER_MAX_AGE_MINUTES`、`CONTEXT_BUFFER_MIN_LINES`
- 分段发送修复：思考泄漏缓冲在收到 reasoning 后立即退出并正常分段；收尾残余缓冲按 `\n\n` 拆发，分段预算按工具轮放宽，不再把多轮回复压成一条长消息
- 图片注意力：触发标记按触发消息是否带图追加指令，vision / anime_trace 描述声明不主动识别上下文块里的图；摘要/印象输出清理 `<think>` 泄漏

## Sep 2, 2026

- 运行环境升级：nonebot2 2.3.3 → 2.5.0、nonebot-adapter-onebot 2.4.3 → 2.4.6、websockets 11.0.3 → 16.1.1（pydantic 保持 v1，hikari-bot 钉版所限）
- 清理 conda bot 环境无关包约 60 个（openai/tavily SDK、未加载插件及其依赖等），删除残留重复 dist-info
- `GlobalConfig` 的 `extra` 改为 `allow`（适配 nonebot2 ≥2.4 的 BaseSettings 写入 `_env_file`）

## Aug 19, 2026

- 项目更名「兔酱」，README 重写
- 核心功能收束到 `nonebot_plugin_naturel_gpt` 插件及其工具集
- 新增工具：anime_trace（以图识角色）、danbooru_search、vision（视觉理解）
- 移除旧插件：gptalk、bangumi-search、itnews
- 敏感配置（kalive、NAS 游戏工具等）全部迁移至本地配置文件，不再硬编码
