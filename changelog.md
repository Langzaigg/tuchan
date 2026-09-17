# 更新日志

此处仅记录重大更新，细节请关注 commit 记录。

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
