# 更新日志

此处仅记录重大更新，细节请关注 commit 记录。

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
