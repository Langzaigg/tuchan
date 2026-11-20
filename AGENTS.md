# 项目概述

基于 NoneBot2 / OneBot v11 的 QQ 机器人，核心功能由 `naturel_gpt` 插件提供。所有功能开发、问题修复和配置调整均围绕该插件展开。

## 核心架构决策

- **LLM 后端**：`openai_func.py` 直调 OpenAI-compatible API（多 key 顺延、自定义 base_url、代理、流式）。多组配置存 `OPENAI_PROFILES`，`rg model` 按群切换。**请求级 profile 快照**：matcher / chat_summary 调用前显式传入 `chat.get_active_profile()`，本轮 `model`/`base_url`/`proxy`/`api_key`/`multimodal`/`enable_stream` 固定，避免并发群切换污染（尤其工具后续轮）。
- **多 API Key 策略（第一顺位优先，禁止粘性轮询）**：`_key_plan(scope, keys)` 恒定从**索引 0** 开始选 key，只跳过「冷却中的 key」和「本次请求内已试过的 key」；`_mark_key_failed` 把失败的 key 打进 `_key_cooldowns`（scope → {索引: 截止时间}，`KEY_FAILURE_COOLDOWN_SECONDS=300`），冷却到期自动回到第一顺位。**只有 `_is_key_level_error`（HTTP 401/402/403/429 或鉴权/额度/限流关键词）才允许顺延**——400 参数错误、超时、上下文超限、图片被拒都与 key 无关，换 key 只会把流量白白漂到备用 key。key 级失败时同一请求内顺延重试（`_tried_key_indices` 记录已试索引，全试过才返回失败）；`rg model` 显式切换会清空该 profile 的冷却。历史教训：旧实现是「任何异常都推进索引且成功不回退」的粘性索引，一次偶发失败就把该 profile 永久钉在第二个 key 上。视觉模型 key 用独立 scope（`<profile>#vision`）。
- **工具调用**：原生 OpenAI Tool Calling，定义在 `llm_tool_plugins/`，`llm_tools.py` 聚合调度。旧文本协议、Extension/PresetHub 已移除。
- **先搜后答**：对外部事实不确定时先 `tavily_search`（Tavily 不可用时工具内部直调博查 fallback）再答，禁止凭记忆编造；闲聊/情感/人格自述/上下文已给信息不受限。
- **流式回复**：`LLM_ENABLE_STREAM` 开启后按 `\n\n` 分段发送，受 `REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS` 控制；响应规则的段数上限读同一配置。**分段预算按工具轮放宽**：`_on_tool_call` 时 `sent_segments` 归零并 `_tool_rounds +1`（中间轮文本各算一轮）；收尾残余缓冲经 `_send_buffer_segmented` 按 `\n\n` 拆发，预算 = `REPLY_MAX_SEGMENTS + _tool_rounds - sent_segments`，超出部分合并为最后一段——任何路径都不得把整段回复压成一条长消息。
- **多模态输入 / 图片策略**（`chat_prompt._apply_image_policy`，所有 profile 唯一路径）：OneBot 图片解析为 `image_url`，文本以 `[图片N]` 占位（存储层为消息内本地编号）。
  - **就地保留**：图片留在它被发出的消息里（触发 user / 历史 user / context_only 块），每轮原样重发以命中前缀缓存；不往触发消息搬运，不对触发句做关键词门控。
  - **统一过期**：`MULTIMODAL_IMAGE_FRESH_MINUTES`（默认 60），截止点按 30 分钟量化（`_image_expiry_cutoff`：`floor((now-60min)/30min)*30min`），整点/半点批量退场，过期图渲染为 `[图片已过期]`；触发消息自身图片始终可见。
  - **非触发消息限额**：别人的消息带出的图片，**只取最近 `_CONTEXT_IMAGE_MESSAGE_LIMIT`（2）条消息**里的图，更早消息的图不带；最近两条都没带图 → 一条都不带。这里的「消息」= 历史 user 条目各一条 + 上下文块内**每行**一条（`_block_message_keys` 解析 `[HH:MM] 发送者:` 行首，**不带图的行同样占额度**；行首解析不出时整块按一条消息兜底）。触发消息自身图片不受此限。
  - **容量上限**：可见图片超过 `MULTIMODAL_MAX_IMAGES`（默认 4）时**只保留当前触发消息中的图片**——历史 user / context_only 的图全部回收；触发图自身超限则按最旧优先裁到上限。触发消息没带图时（如「把上面那张翻译一下」）退化为保留最新的 N 张历史图。被回收的图渲染为 `[图片已过期]`（沿用 num=0 的既有写法）。
  - **渲染层全局编号**：可见图片按上下文顺序从 1 连续编号改写各消息文本；同一 URL 复用编号只注入一次。新图只在尾部追加故编号稳定；编号只在有图离开（过期/回收/裁剪，前缀本就已断）时前移。已知代价：过期事件后历史 assistant 文本里的旧编号可能指向另一张图，每半小时至多一次。
  - 可见图片表 `{编号: 原始URL}` 存 `chat._visible_images` → matcher 写入 `tg._visible_images`（ContextVar），vision / anime_trace 按编号取图，查不到返回可用编号列表。profile `multimodal:false` 时请求层剥离 `image_url`，编号表仍有效。支持 `http(s)://`、`data:image/`、`file:///`。
- **图片缓存**（`image_cache.py`）：异步下载 + 内存 LRU（单图 10MB、总 200MB，覆盖 1 小时内图片）。**直传优先**：公开 http(s) URL 原样传 API；provider 拉取失败（图片 400）时 matcher `mark_passthrough_failed()` 后原位转 base64 重试一次，仍失败再走无图重试。QQ 私有域（qpic.cn/qlogo.cn/gtimg.cn/qq.com）与内网地址始终 base64。下载失败返回空串（不回退原 URL），`_known_bad_urls` 跳过；QQ rkey 过期 400 属预期，降级 DEBUG。`resolve_urls_keep_order()` 按位置对齐返回供图片策略逐张判定；vision/anime_trace 用 `force_base64=True`。`get_chat_prompt_template()` 构建后清除不在 `prompt_messages` 中的缓存。
  - **拉图超时的静默剥图重试**：`openai_func.is_image_download_error()` 是判据唯一来源（matcher 的 `_is_image_download_error` 直接委托它）。除格式不支持/下载失败外，明确含「download + timeout/timed out」——聚合网关拉取多模态文件超时会回 400 `Download multimodal file timed out`（图片已是 base64 也照超，转 base64 救不回来）。命中后在 `stream_response` 轮内**静默剥掉 messages 里所有 `image_url` 再重试本轮**（`_image_stripped` 只一次，`_round_fx` 已有文字/工具副作用时不剥），不抛给主流程；matcher 侧同步把它算进「图片类 400」，且**这类网络问题不清理历史**（`cleanup_after_bad_request` 会把上下文白清掉）。判据刻意区别于普通请求超时（`ReadTimeout`、`流式响应超过总时间上限`）与 `gateway timeout`，避免把该重试/该放弃的场景混在一起。
  - **失败图临时黑名单**：错误文本不带 URL，无法判断是哪张图坏，所以只在**本次请求仅有一张图**且「剥掉后请求成功」时才登记（`image_cache.mark_provider_image_failed`，TTL 1800s，data URI 按 sha1 取指纹）；多图场景只剥不登记，宁可每轮多一次 400 也不误伤好图。请求前 `_drop_provider_failed_images(messages)` 把命中项从**请求副本**剥掉（文本 `[图片N]` 占位与 `_visible_images` 编号表都不动，vision 工具照常按编号取图），避免每轮先吃一次 400。登记点有两个：流式 `_stream_once` 与非流式 `_complete_once` 成功返回处。
  - **超大图先缩放再 base64**：下载后体积 > `_IMAGE_DOWNSCALE_THRESHOLD_BYTES`（**5MB**，取各协议最严的一档：Anthropic 单图上限 5MB / 20 张 / 8000px，避免同一张图在 OpenAI 兼容通道能过、切到 anthropic / gemini 原生就 400）才走 `_downscale_image()`——长边缩到 `_IMAGE_DOWNSCALE_MAX_SIDE`（1536）、JPEG q`_IMAGE_DOWNSCALE_JPEG_QUALITY`（85）、透明通道铺白底；**阈值以下原样送，不动画质**。实测 34MB PNG → 744KB（47×）。缩放失败/非图片一律返回 None 并**按原图继续**（绝不因缩放丢图）；10MB 上限改为在缩放之后判断（原先超 10MB 直接丢图，现在能救回来）。
  - **格式规范化**：`_normalize_image_for_api()` 用 PIL 嗅探**真实格式**（不信 Content-Type，QQ 图片下载常返回 `application/octet-stream`，旧实现一律标 jpeg → 字节与声明不符被上游整条拒掉）：① 真实格式属 `_ACCEPTED_IMAGE_FORMATS`（JPEG/PNG/GIF/WEBP）且未超阈值 → 原样送、mime 按真实格式修正；② 格式不受支持（bmp/tiff/avif/heic/ico…）或超阈值 → 重编码 JPEG；③ PIL 打不开（损坏/非图片）→ 返回 None，**跳过这张图**（宁可少一张，也不让整个请求 400）。上游的 `unsupported image` 400 在 matcher 里并入 `_is_image_download_error`，可走「直传转 base64 → 仍失败剥图」的既有兜底。
  - **上传前按字段上限压缩**：`shrink_data_uri(data_uri, budget)` —— 给有单字段体积上限的第三方接口用（AnimeTrace 走 multipart，part 限 1024KB，报 `Part exceeded maximum size of 1024KB.`）。base64 让体积涨 1/3，所以 ~800KB 的图必被拒。逐档降长边 + 降 JPEG 质量（`_UPLOAD_SHRINK_STEPS`）压到 ≤ `_UPLOAD_BASE64_BUDGET_BYTES`（900KB）为止，透明铺白底；已达标则原样返回；压不动/非 data URI/PIL 不可用返回 None（调用方自行决定）。实测 8.8MB base64 → 884KB（1280×1280）。
  - **为什么必须压**：原图 base64 直接进 body，6 张图能到几十 MB，网关侧（Cloudflare Worker）会因资源超限返回 `HTTP 503 Worker exceeded resource limits`（实测 09-20）。
- **视觉工具（纯文本模型看图）**：profile 设 `model_vision`（默认 `deepseek-flash`，仅 `multimodal:false` 生效）时 `get_tool_schemas` 注入 `vision` 工具；`vision(image_index, prompt)` 读 `tg._visible_images` → `resolve_urls(force_base64=True)` → `_request_openai_compatible` 调视觉模型（可选 `model_vision_base_url`/`model_vision_api_keys`/`model_vision_max_tokens` 覆盖）→ 返回文字描述。视觉配置用 ContextVar 快照（`_CURRENT_VISION_CONFIG`/`_VISIBLE_IMAGES`），依赖全局 `MULTIMODAL_ENABLE=true`。
- **Think 处理**：流式回调实时拦截成对的 `<think>…</think>`，把块内内容转入 `reasoning_content`。**兜底正则 `_strip_think_tags()` 是思考标签唯一的对外关口**（`send_segment` 发送前、`_on_reply_complete` 落库前、非流式路径都会过一遍），它清两类东西：① 成对块（内容计入 reasoning）；② **孤立标签**（只有 `<think>` 或只有 `</think>`，含 `<thinking>`/带空格/大小写变体）——**只删标签本身、保留前后正文**。②是必须的：部分模型/网关把思考并进 content 只留闭合标签（实测 Kimi-K3 经聚合网关出现 `…行，给你画。</think>画好了。…`，旧实现只认成对块，标签就原样发进群）。**思考泄漏重试（profile `think_leak_retry`，默认仅 kimi 系开启）**：在 `openai_func.stream_response` 的轮循环里做，判据只有两条——**本轮没收到 `reasoning_content`**，**正文超过 `think_leak_retry_chars`（默认 100 字）**。带工具的回复轮/中间轮最容易把思考漏进正文（网关吞掉 reasoning 或直接并进 content），因此**不区分是否带 `tool_calls`**：探测期把正文压住不发（`_probe` 三态 buffer/pass/drop，包在 `_stream_on_text` 外层），判定泄漏就丢弃本轮文本**原样重发一次**（不动 `messages`、不加提示，保住前缀缓存；此时工具尚未执行，重发无副作用），重试结果直接按正常路径输出、**不做任何额外后处理**；整个请求只重发一次。一旦中途收到 `reasoning_content` 立即判定正常、把缓冲原样补发并恢复直通（正常 kimi 回复 reasoning 先到，故不增加流式延迟）。非流式路径在同位置按 `message_dict.reasoning_content` 判定。`no_think: true` 的 profile 不参与（本来就不该回 reasoning），`type != "chat"`（摘要/印象）也不参与。后台任务（摘要/印象）的输出仍经 `chat_summary._clean_llm_text` 去掉 `<think>` 块与孤立 `</think>` 前的内容，避免思考混入印象再注入 prompt。**思考前缀剥离（profile `strip_thought_prefix`，默认关，目前仅 `gemini` 开）**：有些网关（实测 TeamoRouter 的 Gemini 4 系）把 thought summary **当普通 content 下发**——既无 `<think>` 标签也无 `reasoning_content`，上面两条防线全部落空。`_ThoughtPrefixStripper` 夹在 `_stream_once` 与泄漏探测之间：以空行分块，块内 CJK ≤2% 且 ASCII 字母 ≥45%（`_looks_like_thought_block`）判为**候选**思考块并压住，**等后面真的出现中文正文才确认丢弃**，流结束时未确认的原样回放（最坏是没剥干净，不会吞正文）；本轮带 `tool_calls` 时按丢弃处理（工具轮 content 整段都是思考）。`strip_thought_prefix_text()` 负责非流式、摘要/印象（`on_text` 为空）与收尾兜底。
- **括号噪音过滤**：实际调过工具的请求中，分段整条被一对括号包围（`_is_bracket_wrapped`）则不发送。
- **重试策略**：分两层，按错误性质分流。
  - **上游瞬时故障（轮内重试，`openai_func.stream_response` 的 except 分支）**：`is_transient_upstream_error()` 命中网关 5xx / Cloudflare「Worker exceeded resource limits」/ `RemoteProtocolError` 服务器断开 / ReadTimeout / ConnectTimeout / **ReadError（读响应体时连接被对端关闭）** / WriteError / PoolTimeout / 连接重置时，**原样重发当前这一轮请求**（`messages` 未被改动，前几轮已执行的工具不会重跑），退避 `_TRANSIENT_RETRY_DELAYS = (1.5, 5.0)` 秒、共 2 次；等待时间不计入工具循环耗时预算。
    - ⚠️ **异常文本必须拼上类名**：`err_text = f"{e.__class__.__name__}: {e}".lower()`。httpx 的 `ConnectError('')`/`ReadTimeout('')`/`ConnectTimeout('')` 的 **`str(e)` 是空串**（日志里看到的是 repr 才带类名），早期用 `str(e).lower()` 导致这些异常的分类全部落空——实测 22:17/22:18 两次 `ConnectError('')` 因此没进重试、直接失败。
    - ⚠️⚠️ **必须写 `e.__class__.__name__`，不能写 `type(e)`**：`stream_response` / `_stream_once` / `_complete_once` / `_completion_kwargs` 的形参里都有 `type`（`type: str = "chat"`），函数体内 `type` 是那个字符串 → `type(e)` 抛 `TypeError: 'str' object is not callable`，会把**整个 matcher 打挂**（群里既没回复也没失败提示）。2026-09-21 线上连炸 8 次就是这个原因。
    - matcher 侧拿到的是 `repr` 文本（含类名），不受影响；`matcher.do_msg_response` 对 `stream_response` 另有 try/except 兜底（内部自爆则降级为「请求失败」走既有错误处理），保证不再整个 matcher 挂掉。**安全闸门 `_round_fx`**（`{"text": 文字已流出, "tools": 工具已执行}`，每轮请求前重置）：本轮一旦有副作用就不再重试，避免重复发消息/重复画图；`_emit_reply`（已落库）与内部控制轮的缓冲回调以外的真实 `on_text` 转发都会置位。用完次数才按常规错误上报（matcher 落 error log + 群里提示）。**刻意不重试**：整体超时（「流式响应超过总时间上限」，模型确实慢）、400 参数错、上下文超限、403 需订阅、key 级错误（401/402/403/429 由多 key 顺延处理）。
  - **上游内容审查拦截（`is_content_review_error`：`data_inspection_failed`/`inappropriate content`/`content_filter`/`flagged`）**：聚合网关会把部分模型（实测 Kimi 系）路由到阿里系后端，其内容安全（`InternalError.Algo.DataInspectionFailed`）会拦下请求。处在该 except 分支最前面（**优先于上下文/图片分支**），按「被拦对象 + 阶梯」处理：
    - **处置阶梯（降级模型不再写死，改用本群 profile 的 `model_mini` 摘要模型）**：
      ① 本轮走的是流式 → **同模型关流式重试本轮**（`_review_nonstream_retried` 置位 + `_force_nonstream_round` 一次性标志，请求分支读取后即清零；网关输出审查只逐 delta 拦流式，实测同一请求非流式原样放行，损失最小）；
      ② 仍被拦 → **保留图片**，把本轮模型换成 `request_state["config"]["model_mini"]` 重试（改的是 `request_state["config"]["model"]`，本请求后续轮沿用；`model_mini` 缺失或与当前模型相同则跳过）；
      ③ 仍不放行 → **剥离图片 + 换回原模型**（`_review_original_model`，审查发生前的模型名）重试（`_image_stripped` 置位；`messages` 里确实有图才走这步）。**这一步不再区分被拦的是图片还是文本**（判据 `"image data" in err_text` 只用于日志措辞）——文本被拦时带图的整条请求同样可能被拒，剥图是放弃前唯一还能做的一次尝试；
      ④ 仍被拦 → 放弃：置 `_content_review_blocked=True`（同时给下面 `is_ctx_error` 的剥图分支加闸），按失败上报；**matcher 侧随即发一张随机 gif**（不再静默，见「报错兜底」）。
      阶梯不会回环：`_review_nonstream_retried` / `_review_model_switched` / `_image_stripped` 各自只置位一次。
    - matcher 侧（`do_msg_response` 失败路径）对审查失败**只落 error log + 控制台告警，不发群提示、不清理上下文**（必须放在 `_is_bad_request_error` 分支之前，否则会被当作「请求上下文异常」把近期历史清掉）。
  - **HTTP 200 拒绝文案（`is_provider_rejection_text` / `PROVIDER_REJECTION_ERROR_TEXT`）**：上游偶尔不报错而是把「The request was rejected because it was considered high risk」之类审核文案当正常 content 返回。matcher 重试循环里识别到（`success` 且无工具调用时）改判失败（`_provider_rejected`，替换为 `PROVIDER_REJECTION_ERROR_TEXT`），跳过 passthrough 与 cleanup 直接按失败处理；`send_segment` 另有拦截兜底，保证该文案永远不入群。
  - **matcher 层（`do_msg_response` 的 `for _retry in range(1 + MAX_RETRIES)`，MAX_RETRIES=2）**：① 图片 400（`_is_image_limit_error`/`_is_image_download_error`）→ 先直传转 base64、再剥图重试；② 空响应 → 清上下文重试一次；③ assistant 空 content 400 → 补占位符重试；④ 工具标记泄漏 → 丢弃本轮文本重试。
  - 历史教训：瞬时重试**不能**放在 matcher 外层——那层有「已跑过工具就不重试」的守卫，而本群绝大多数回复都是「先 tool_calls 画图、再出文字」的多轮，503 恰好落在带工具轮的后续请求上，外层重试永远不触发（实测 14:12 就是这样漏掉的）。
- **Markdown 后处理**：`openai_func.strip_chat_markdown()` 在 matcher 发送（`send_segment`）与落库（`_on_reply_complete`、失败路径）收口处把 Markdown 语法（加粗/标题/清单符号/行内代码/引用符）降级为纯文本，只去语法不去内容；`ENABLE_MSG_TO_IMG` 时不启用。规则层只写「不用 Markdown」，不堆格式禁令。同路径还有 `_strip_speaker_prefix()`：剥离回复开头与近期发送者昵称匹配的「名字：」说话人前缀（模型模仿 `[HH:MM] 名字: 内容` 历史格式的产物），互为子串 ≥2 字才剥，正常冒号句不误伤。最后再过 `strip_edge_commas()` 去掉首尾的逗号（分段切在逗号处、或剥掉名字前缀后留下的孤立「，」），句中逗号不碰。同路径先过 `normalize_reply_separators()` 把整行分隔线换成双换行（详见「多模态 / 分段」）。
- **非触发消息缓冲**：不回复的群消息由 `matcher._recent_context_buffers` 按 `chat_key` 暂存（窗口 `CONTEXT_WINDOW_SIZE + int(CONTEXT_WINDOW_SIZE * ratio)`）。`should_reply` 按本条消息独立计算。下一条触发消息通过节流后 flush 为一条 `context_only` 消息（文本头 `[群聊上下文-非触发消息]`，行格式 `[HH:MM] sender: text`）置于触发消息前。flush 时**时间衰减**：超过 `CONTEXT_BUFFER_MAX_AGE_MINUTES`（15）的条目丢弃，至少留 `CONTEXT_BUFFER_MIN_LINES`（3）条；**图片全部保留**，块内本地编号整体平移，`image_meta`（每张图 `{sender, timestamp}`）随 `update_chat_history_row(image_meta=...)` 写入供按张过期。存储层 `role="system"` + 标志 `context_only`，**渲染为 `user` 角色**（system 不接受 image 部件）；`_is_context_only_message` 按文本头识别，触发消息 = 最后一条非 context_only 的 user（`_find_trigger_msg_idx`）。不计轮数、不持久化、append-only，随所在区间被裁剪/摘要删除。
- **尾部触发标记**：`[当前触发] 回应下面这条来自 {sender} 的消息，它附带了图片/它不带图。其余上下文只作背景（，没被问到的图不用去看）。` 只进本次请求的 messages、不落库（`_on_reply_complete` 只持久化回复文本与工具消息），与 `[记忆提醒]` 同槽位、并存时合并一条。循环邮箱插入新消息前 `openai_func._drop_stale_trigger_marker` 去掉该标记行（保留同条里的其他行），避免"只回应那条"错套到新插入的消息。用户不知道图片编号，只会通过引用回复带图，故 vision / anime_trace 的 description 写成"用于当前消息自带或引用的图，明显在问上下文某张图时也可用，没人问的不主动认"。
- **Reply 上下文注入**：触发消息含 reply 段时，以 `[回复 xxx 的消息]` 前缀拼入被回复文本，被回复图片插到 `image_urls` 前并重编号。reply 消息段与 `get_msg` 返回的原始 dict 走同一吸收函数（`_absorb_reply_segments`）；reply 只给 `message_id` 时兜底调一次 `get_msg`。
- **合并转发（forward）**：`utils.extract_forward_content(bot, seg, image_offset=...)` 展开成 `[合并转发消息 共N条]\n昵称: 内容` 多行文本，**单条最多 `FORWARD_MAX_MESSAGES=20` 条消息、`FORWARD_MAX_IMAGES=4` 张图片**；单条文本截 `FORWARD_MSG_MAX_CHARS=150` 字、正文总长 `FORWARD_TOTAL_MAX_CHARS=1200` 字，超长标「内容过长已截断」。文本里的 `[图片N]` 从 `image_offset+1` 起编号，与调用方把这些 URL 插进 `image_urls` 的位置严格对齐（reply 图 1..k，转发图 k+1..）。内容优先取消息段内联 `content`，否则 `get_forward_msg`（先 `id` 再 `message_id`；返回 `messages`/`message` 两种键都认，node 的 `message`/`content`、CQ 码串、`sender.card/nickname` 都兼容）；嵌套 forward 不递归，留 `[嵌套合并转发]` 占位。任何失败返回 `("", [])`，不影响本条消息。直接转发（非 reply）也走同一条路径：`_extract_message_text_and_images` 里展开，matcher 的空消息门控把 `forward` 也算作内容。
- **报错兜底（`error_gif.py`）**：请求失败路径不再往群里发 `[系统] ...` 文案，改 `send_error_gif(matcher.send)` —— 从 `Path(NG_DATA_PATH)/gif` 随机挑一张 `.gif`（目录可增删，无需配置），先按 `file:///` 本地路径发、失败自动转 `base64://`；目录为空或两档都失败只写日志、绝不补发文字。覆盖 matcher 主链路的 5 个失败出口（400 / 上下文超限 / 空响应 / 模型请求失败 / **上游内容审查阶梯用尽**）；`rg` 指令错误与 MOSS 出图失败属功能性结果反馈，仍保留文本。内容审查这条是「最后一步」：`openai_func` 先按阶梯重试（关流式 → 换 `model_mini` → 剥图），仍被拦才会走到这里发 gif。
  - **哪些报错不会发 gif**（排查时先看这几条，别误以为兜底失灵）：图片拉取超时被轮内剥图救回、瞬时故障重试成功、key 顺延成功、工具已执行后的连接断开（`raw_parts` 非空，群里已有半截回复，按设计不补发）——这些都到不了失败出口。判断方法：日志里搜 `报错兜底：已发送 gif 表情`；没有这一行就说明根本没调用 `send_error_gif`（而不是发送失败）。

- **自定义昵称**：`rg nn <昵称>`（≤30 字）存 `PersistentDataManager._custom_nicknames`（全局），优先于群名片。
- **个人印象注入（per-turn）**：只注入触发者印象。`update_chat_history_row` 记录触发 user 前，若上下文尚无该 `user_id` 的印象 system（`is_impression`），先 append 印象 system（`[用户印象: 昵称]\n正文\n[你的记忆]...`）再 append user。同一用户整个上下文只注入一次，随绑定轮被删除后下次触发重建；印象 system 不持久化。
- **人设库带出（per-turn，只读，只服务于画图）**：人设统一存上游 AnimaTool 人设库（`GET /anima/characters`，与 `COMFYUI_BASE_URL` 同服务；群友在 `http://legend503.com:5007/anima/characters` 网页增改），**bot 只匹配和读取、不写**；记忆不再保存人设（remember 工具「不要记」已声明，4 个原先存了大量人设的群的群记忆里有 `人设库说明` 条目指向网页）。人设记录只存画图用的外观信息，**自然语言只有 `description` 一个字段**（绘制注意也写在里面；早期的 `notes`/`personality` 已去掉，上游读到旧数据时 notes 并入 description）。**只在本条消息画图功能实际启用时带出**：`anima_generate.is_draw_active(chat_key, text)`——`LLM_ENABLE_TOOLS` 且画图工具已注册，漫画模式 / force / on 恒启用，auto 需触发句含画图关键词（`DRAW_KEYWORDS`，与 matcher/openai_func 的 `_DRAWING_KEYWORDS` 同一组），off 不启用。`llm_tool_plugins/anima_characters.py`：列表缓存 + ETag（`If-None-Match`）每 60 秒后台校验、从未拉取过才同步等一次（3 秒超时），上游离线沿用旧缓存；匹配与上游 `match_characters` 同口径（名称/别名，ASCII 词边界，最长词优先），本地计算。`chat.inject_character_personas(anchor=None)`：**只扫锚点触发句**（发送者名 + 正文，**发送者自己的名字也算提及**；历史触发句的人设当时已带出、自然留在上下文里，context_only 背景块与 bot 回复不扫），**bot 自己的人设不带**（`_bot_self_names()`：当前人格名、人格名 `-` 前的主名、nonebot `nickname`，经 `anima_characters.exclude_names` 按名称/别名排除——已在人格 md 里定义）；命中的人设若**请求窗口**（`_history_buffer_round_limit()` 轮）内还没带出（`character_versions` 记录 `{id: 上游 updated_at}`；上游改过则再带出一次并标注「人设已更新」），写进锚点所在轮的前导块：该轮已有印象块就并进去（`character_text` 段，与印象/记忆**共用一条 system**），否则在该轮最前面新建 `is_impression=True`、`impression_user_id=""` 的块。渲染只输出画图内容：`[人设资料]` 标题行 + 每人 `画图: count/角色/作品/画师`、`基础外观`、`造型`、`描述`；标题行附人称提示（用户「画我/换成我」= 发送者本人，按发送者人设画）；印象块里为印象段 `[用户印象: 昵称]…` + `\n\n` + 人设段（只有人设段时以 `[人设资料]` 开头，`_is_impression_system_message` 按此前缀识别以绑定轮次）。随绑定轮裁剪/摘要删除、不持久化，删除后再被提到时重新带出；写入后不再改写（缓存不变式）。单条触发句最多 8 条（`_MAX_PERSONAS_PER_INJECTION`）。未解锁内容限制的群过滤 `nsfw` 人设。调用点：matcher 主路径在构建 prompt 之前；循环邮箱路径落库后以该 entry 为锚点调用，返回的块文本存 entry `persona_text`，`_insert_mailbox_entries` 并入 `[新消息提醒]` 让本次回复就能用上；`manga_idle_draw` 的 prompt 独立构建、不含已带出的人设块，故按最近 10 条消息匹配（同样排除 bot 自己）后附在 prompt 里。开关 `ANIMA_CHARACTERS_ENABLE`。
- **人格系统**：来源固定 `config/personas/`，运行时热加载（`chat_preset` 5 秒 TTL 缓存）；YAML `PRESETS` 仅运行时容器。profile `extra_prompt` 注入 S1 末尾。
- **Debug / Error 日志**：每次请求保存 `data/naturel_gpt/logs/{chat_key}.latest.json`（`prompt` 为请求前快照；`loop_messages` 为工具循环结束时的完整消息列表，排查邮箱插入/工具轮以它为准；`tool_messages` 仅最终回复段）；失败即写 `{chat_key}.error.json`；摘要任务写 `{chat_key}.summary.json`。写入前经 `sanitize_internal_control_text()`。内部异常文本不得存为 assistant 历史、进 prompt 或发群。
- **循环邮箱（插入式打断）**：同群新触发消息不 cancel 旧任务，而是照常落库后 `push_loop_input` 进 `TextGenerator._loop_mailbox`（锁内，entry 保留 `recorded_msg`/`sender`/`userid`/`image_urls`）。`stream_response` 每个文本轮结束调 `_insert_mailbox_entries`：本轮回复先经 `on_reply_complete` 落库，再把邮箱 entry **合并为一条多行 user 消息**（`_build_loop_batch_message`，`[图片N]` 从 `_next_image_index` 续编并并入 `tg._visible_images`），其前注入 ephemeral system `[新消息提醒] ...`（`build_mailbox_notice(count, reply_completed)` 生成，不落历史；点名要求「句中自然称呼」，写「开头点名对象」会被模型执行成「名字：」前缀的记录格式），重置轮数/计时继续循环；entry 去重按行比对。`on_reply_complete(text, tool_messages, reply_entries)` 逐回复回调：`reply_entries` 非空时用户维度历史归属到批次内各用户。终端阶段不插入，残留 entry 由 `_finalize_chat_task()` 收尾时逐条重走 `do_msg_response`（复用 `recorded_msg`）。
- **运行统计**（`stats.py`）：按日分桶持久化 `data/naturel_gpt/stats.json`；触发回复、各模型 token（兼容 OpenAI/Anthropic/DeepSeek 缓存字段）、工具调用计数。`rg stat` / `rg stat reset`。

## 关键路径

```
ATRI/plugins/nonebot_plugin_naturel_gpt/
├── chat.py / chat_memory.py / chat_history.py / chat_prompt.py / chat_summary.py   # Chat 类及 Mixin
├── matcher.py              # OneBot 入口
├── config.py               # 配置
├── openai_func.py          # LLM 调用
├── llm_tools.py / llm_tool_plugins/   # 工具
├── image_cache.py          # 图片缓存
├── error_gif.py            # 报错兜底：随机 gif 表情代替系统错误文案
├── persistent_data_manager.py         # 持久化
├── stats.py / command_func.py / persona_loader.py / utils.py / chat_manager.py
├── text_to_image.py / draw_db.py / store.py / singleton.py / logger.py
config/naturel_gpt_config.yml          # 主配置
config/personas/                       # 人格（.md 单文件 / skill 文件夹）
data/naturel_gpt/                      # 持久化状态、日志、draw.db（勿直接改）
```

# 模块说明

## 启动与多接入

- `main.py` 按 `config.yml` 的 `BotEndpoints` 中各自的 `enabled` 启动接入点。`onebot` 监听反向 WebSocket。`qq_official` 默认只有出站 HTTP 和 WebSocket，通过 `nonebot-adapter-qq` 直连 QQ 开放平台。各进程加载既有插件集，`ATRI/qq_official.py` 在进程内转换成插件使用的 OneBot v11 事件与 API。所需 OpenID 映射为稳定的数字 ID，主动推送与群管理需要平台权限。
- 官机合并转发在接入层转为内联 `forward` 段，供 Naturel GPT 原有展开逻辑处理。适配 `message_type=102`、`103` 引用的聊天记录，以及 QQ 文档中 `TMP_` 索引配单个无 `msg_idx` 正文块的形态。索引可复用同一会话缓存中已收到的原转发。
- Naturel GPT 配置各接入点共用 `config/naturel_gpt_config.yml`；`config/bots/<首个昵称>/<接入点>.yml` 只是**覆盖层**，只写与主配置不同的键（插件加载时 `base + overlay` 合并、保存时只回写覆盖键；`prepare_config` 启动时自动剔除与主配置同值的键和 NAS 共用字段——旧的全量拷贝由此自动迁移，非默认接入点的 `ADMIN_USERID`/`NAS_GAME_WHITELIST_GROUPS` 例外、始终显式保留）。改 prompt/模型只动主配置；记忆分离靠数据目录（主接入点用 `NG_DATA_PATH`，其他接入点强制 `data/endpoints/<接入点>/naturel_gpt`）。存档和统计按首个昵称写入 `NG_DATA_PATH/archives/<昵称>/`。无接入点昵称，或插件 YAML 显式配置自己的 `nickname` 时，使用该接入点的默认 `NG_DATA_PATH`。新昵称可从默认旧档首次导入匹配人格的记录与可归属的记忆，旧档保留。画图 SQLite 和 NAS 根路径、同步记录由 `NaturelGPTShared` 共享。
- 主接入点可用 `notify_superusers: false` 关闭 `essential` 的申请、邀请、权限变动和禁言通知，`superusers` 继续用于权限判定。Hikari 的上线及每日版本检查私聊由 `hikari_enabled` 控制插件是否加载。

## `__init__.py`

- 加载配置与持久化状态；初始化 `TextGenerator`；导入 `matcher`。`init_tools(config)` 按 `LLM_DISABLED_TOOLS` 条件注册；`tavily_search` 在 Tavily key 可用或仅有 `BOCHA_API_KEY` 时注册（博查无独立工具）。
- Anima 画图：启动 health check 通过则 `COMFYUI_ENABLED = True`；工作流从 `GET /anima/workflows` 动态拉取。`draw_db.init_db()`。
- MOSS 画图：独立接口 `/moss/info|schema|knowledge|generate`（**不在** `/anima/workflows` 工作流集里），启动时单独拉 schema/knowledge 后 `enable_moss_tool()` 注册；拉不到就不注册，不影响 Anima。`MOSS_ENABLE` 可整体关闭。

## `config.py`

- `GlobalConfig` / `Config` / `PresetConfig`；缺失键由 `CONFIG_TEMPLATE` 补齐并回写；`reload_config()` 整体替换对象。
- `OPENAI_PROFILES`（键=profile 名，值=模型配置 dict）+ 默认指针（见下）；每群独立 `active_profile`。`DEFAULT_PERSONA` 用于昵称未匹配时回退；人格目录在主配置旁。
- **默认模型配置用指针，不再有名叫 `default` 的独立配置**：`OPENAI_PROFILES.default` 的值是**字符串**（真实 profile 名，如 `default: ds`），不是模型配置。解析顺序 `resolve_profile_name()`：会话自选 → `default` 指针 → 旧字段 `OPENAI_ACTIVE_PROFILE`（仅兼容回落）→ 第一个真实 profile；指针失效/名字被删都会回落。配套 **Config 的方法**（⚠️ 必须是方法：各模块里 `config` 拿到的是 Config **实例**（`from .config import config`），写成模块级函数调用会 `AttributeError: 'Config' object has no attribute ...`）：`get_profile_names()`（只列真实 profile，供 `rg model` 展示与校验）、`get_default_profile_name()`、`get_profile(name)`（恒返回 dict，自动解指针，**所有读取 profile 的地方都用它，不要再直接 `.get` OPENAI_PROFILES**）、`DEFAULT_PROFILE_KEY`（ClassVar，值 `"default"`）。**全局默认只由配置文件的指针决定**：`rg model` 只写本会话 `active_profile`，`Chat.apply_profile()` 只跟随，**两者都不改写指针**（没有任何代码路径会改它，改默认就编辑 yml + `rg reload_config`）。旧配置里 `default` 仍是 dict 时按真实 profile 处理（向后兼容），旧扁平键迁移为 `main` + `default: main`。

## `persona_loader.py`

- 简单 `.md`（整文件为 prompt）与 Skill 文件夹（含 `SKILL.md`，名=文件夹名首个 `-` 前）共存。Skill 注入顺序：`SKILL.md` → `soul.md` → `limit.md` → `resource/behavior_guide.md` → `key_life_events.md` → `relationship_dynamics.md` → `speech_patterns.md`。

## `openai_func.py`

- `TextGenerator`（Singleton）。`stream_response()` 返回 `(text, success, tool_messages, reasoning_content)`；入口对 prompt 逐 dict 浅拷贝，循环内改动均为 dict 级替换，调用方 prompt 保持请求前快照。工具附件按 `chat_key` 分桶（`consume_tool_outputs(chat_key)`）。
- **Content 兜底**：`content: None` → `""`；空 assistant content 填 `"[无内容]"`（防 Moonshot 400）；assistant 消息保留 `reasoning_content`（历史中的按 profile `keep_reasoning` 剥离）。
- **工具附图进上下文**：工具附件里带 `{"type": "image", "url": ..., "into_context": True}` 的（目前只有 `browse_url` 的小红书配图）由 `_execute_tool_calls` 先收齐，循环结束后 `_inject_tool_images()` 统一追加**一条 user 消息**（文本 `[工具附图]…[图片N]` + `image_url` 块）到**全部 tool 响应之后**——不得插在 assistant(tool_calls) 与 tool 响应之间（上游 400）。只认 `into_context` 显式标记，pixiv 搜图等既有工具行为不变。`_current_request_multimodal` 为 False 时整条跳过（请求层会剥 image_url，注入白占位）；单张下载失败只跳过该张、全失败不追加任何消息（正文照常返回）；图片登记进 `_visible_images`（接在最大编号之后，文本给 `[图片N]`），纯文本 profile 的 vision 工具仍可按编号取图。图片类 400 走既有剥图重试（`round_idx > 0` 的 `is_ctx_error` 分支会剥掉 messages 里全部 image_url）。
- **工具轮**：中间轮文本经 `on_text` 实时输出；`LLM_MAX_TOOL_ROUNDS` 不计 remember-only 轮；总次数 `LLM_MAX_TOTAL_TOOL_CALLS`（默认 15）、搜索 `MAX_SEARCH_TOOL_CALLS=3`。控制文本由 `sanitize_internal_control_text()` 清洗后才发送/持久化。**搜索超限提示必须在 tool 响应全部 append 之后插入**（assistant(tool_calls) 与 tool 响应之间不得插任何消息，否则上游 400）。
- **终端轮（双闸门）**：`TERMINAL_TOOLS = {"generate_anima_image", "generate_moss_image", "remember"}`。轮数 ≥ `LLM_MAX_TOOL_ROUNDS`、总次数超限、工具 token 超 `TOOL_CONTEXT_TOKEN_BUDGET`、耗时 > `LLM_TOOL_LOOP_MAX_SECONDS`（180）任一进终端阶段：tools 数组保持全量（缓存前缀不变）、非终端调用执行侧过滤、只允许单次终端调用，之后无工具收尾轮（`tool_choice="none"`）出最终文本。所有终止路径返回已累积文本。**过滤非终端调用时必须给每个被丢弃的 tool_call_id 追加一条「未执行」占位 tool 响应**（2026-09-22 实测：终端轮模型仍返回 browse_url，静默丢弃后下一轮请求被上游 400 "insufficient tool messages following tool_calls message"）。
- **强制画图**：`draw_mode == "force"` 且含画图关键词时尾部注入 `_FORCE_DRAW_HINT_TEXT`（不用 tool_choice 指定函数，思考模式 provider 不兼容）；**触发句同时提到 moss 且有画图意图（画/p/图/换/draw）时改注入 `_FORCE_MOSS_HINT_TEXT`**，明确要求调 `generate_moss_image`、不要推回 anima。`tool_choice="none"` 被拒时省略字段重试一次。**仅 `type == "chat"` 注入**：摘要/印象也走 `stream_response`（`chat_summary.get_response`），它们不带 tools，注入画图提示纯属污染 prompt；且 `_current_chat_key` 是实例态，后台任务会继承上一次群聊的 force 模式（曾因此在 force 群连刷多行同类日志）。日志行带 `type` 与 `chat_key` 以区分群聊/后台任务。
- **请求头（`_build_provider_headers(base_url)`，非流式/流式两处均应用）**：恒定带自定义 User-Agent（`ATRI-NaturelGPT/1.0 (QQ chatbot)`）—— **必需**，httpx 默认 UA（`python-httpx/x.y`）会被 `api.commandcode.ai` 的 Cloudflare 直接 403（`error code: 1010`）。**另保留 opencode.ai（Console Go）的 `x-opencode-session` 注入**：host 含 `opencode.ai` 时用 `atri-<md5(chat_key)[:24]>`（由 `_CURRENT_CHAT_KEY` 派生，同群稳定；无 chat_key 用进程级 uuid 兜底），其他 provider 不加该头。当前生产配置已切到 commandcode，opencode 逻辑留作备用。
- 流式工具名双拼修复；`arguments` JSON 校验（全丢重试 ≤1 次，再撤工具强制出文本）；`stream_options.include_usage` 采集缓存命中到 `_last_stream_usage`；循环异常始终 return 由 matcher 重试；httpx read 超时每 chunk 重置、总上限 5 分钟；`type='summarize'/'impression'` 用 `model_mini`（空回退 `model`）；公开方法不用可变默认参数。

## `llm_tools.py` / `llm_tool_plugins/`

每个工具一个文件：schema + `run(args, config)`。`get_tool_schemas(config, chat_key)` 按 `chat_key` 注入画图 schema（函数名统一 `generate_anima_image`）。`generate_moss_image` **常驻暴露**（不按触发句屏蔽），调用时机由 schema description 约束：用户提到 moss，或需要画面内准确文字/分格漫画/海报级排版时使用，普通插画优先 anima。

- **`tavily_search.py`**：唯一搜索 schema。多 key 选额度最多；`advanced`、`max_results=20`、单条 300 字、总长受 `WEB_FETCH_MAX_CHARS`。失败标记 `_tavily_disabled` 并直调博查 fallback（`bocha_search.py` 仅内部 helper）。
- **`browse_url.py`**：短链还原 → 社交平台 SSR → Playwright → trafilatura → Tavily Extract。**小红书**走纯 HTTP 双 UA（实测 2026-11）：PC 端分享链接 `/explore/<id>?xsec_token=...&xsec_source=pc_feed` **必须桌面 UA**——移动 UA 会被风控 302 到 `/404/sec_xxx`（HTTP 200 的 404 页），数据在 `window.__INITIAL_STATE__`（`note.noteDetailMap[noteId].note`）；短链还原出的移动分享页反过来只认移动 UA，数据在 `window.__SETUP_SERVER_STATE__`（`LAUNCHER_SSR_STORE_PAGE_DATA.noteData`），两套结构由 `_xhs_note_bundle()` 归一化。**`xsec_token` 必须原样保留**（去掉即 404）。配图取前 4 张（PC 端 `imageList[].url` 常为空串，真实地址在 `urlDefault` / `infoList` 的 `WB_DFT`），以 `into_context` 附件返回见「工具附图进上下文」；`_describe_xhs_images()` 只在 profile 配了 `model_vision` 且 `multimodal=false` 时把图转成文字描述拼进正文。配图整体失败时退化为只给正文+评论。**视频字幕**：`_xhs_fetch_subtitles()` 从 `video.mediaV2`（JSON **字符串**）的 `video.subtitles` 取 srt（`source` 是原始音轨，优先于机翻的 `zh-CN`/`en-US`），`_srt_to_text()` 去序号与时间轴、合并相邻重复行，超 1500 字截断；视频笔记的 desc 通常只是一堆话题标签，字幕才是正文，所以值得多这一次请求（超时压到 10 秒，任何失败静默跳过）。
- **`pixiv_search.py`**、**`bangumi_search.py`**（`bangumi(action=...)` 单 schema）、**`danbooru_search.py`**（随画图工具暴露，`_DRAW_ONLY_TOOLS`）、**`nas_game_list.py`**（`NAS_GAME_WHITELIST_GROUPS` 白名单，路径全走配置）。
- **`anime_trace.py`**：AnimeTrace 以图识角色。`anime_trace(image_index)` 从 `tg._visible_images` 按显示编号取图 → `resolve_urls(force_base64=True)` → POST `/v1/search`（`is_multi=1`、`ai_detect=1`）；模型经 `/v1/model/list` 动态选取（缓存 1 小时）。所有 profile 暴露。
- **`anima_reverse_tag.py`**：参考图提示词反推（上游 `POST /anima/reverse_tag`，WD14 tagger，与 `COMFYUI_BASE_URL` 同服务）。`anima_reverse_tag(image_index, threshold?)` 取图方式同 anime_trace，提交 `image_base64`；上游响应的 `anima` 字段（quality_meta_year_safe/count/character/series/tags）即可作为 `generate_anima_image` 的参数基础，`character_top5` 角色候选仅 top1 ≥0.5 才展示（确认角色身份仍用 anime_trace）。**用户发图要求参考/照着画时必须先反推，再综合反推结果、图片本身和用户需求调用画图工具**（工具 description + `get_draw_s1_rules` 双处引导）。不参与自动发现，由 `enable_anima_tool()` 随画图工具一起注册、`disable_anima_tool()` 一起移除，并列入 `_DRAW_ONLY_TOOLS`（draw off 不暴露）。
- **`vision.py`**：见核心架构决策「视觉工具」。
- **`memory.py`**：长期事实记忆（只记客观长期事实；性格由印象、话题由摘要负责，人物人设归人设库）。scope `group`（本群所有人格共享）/`user`（跨群共享）；action `save`/`delete`/`consolidate`（批量）。接近上限 80% 注入整理提醒不阻断。
- **`anima_characters.py`**：人设库客户端（不是工具，已排除出自动发现），见核心架构决策「人设库带出」。
- **`anima_generate.py`**：ComfyUI 画图。
  - `rg draw [force/on/auto/off]`，持久化 `ChatData.draw_mode`；工作流动态发现（`/anima/workflows`，`deprecated` 过滤，不可达降级 `fuse`）；`select_default_workflow()` 首选 `fuse` → 含 "turbo" → API `default` → 兜底；`LEGACY_MODEL_MAP` 处理旧名。
  - knowledge 由 `_build_workflow_knowledge()` 统一压缩（≤ 2k token）拼入 schema `function.description`（`_schema_cache`）；行为短规则 `get_draw_s1_rules` 常驻 S1（含参考图先反推、人称规则「用户的我=发送者/你=bot」）。**编号/预计时间的详略只在工具返回里约束**：任务编号必须告知（提交凭证），预计时间只顺带提一句（不再要求「必须告知预计时间」，否则每次重画都会套「X秒出图、画好会自动发给你」模板）；S1 不重复这条。
  - `run()` `POST /anima/generate`（body 带 `workflow`），后台任务，结果入 `_pending_results` 由 matcher 发图；成功返回编号 `draw-XXXXXX` + 预计秒数，提示词存 `draw.db`。队列 >5 拒绝；参数全空拒绝。
  - **漫画模式**（`rg manga on/off/[画风]`）：动态默认工作流，无编号、不存 DB；`MANGA_RULES`/解锁时 `MANGA_UNLOCK_RULES` 追加到漫画 schema description；空闲检测 `should_inject_manga_idle()`（`MANGA_IDLE_MINUTES`/`MANGA_IDLE_ROUNDS`），`manga_idle_draw()` 用 mini 模型按上下文设计场景（`pending_request` 非空时严格按该请求画）。
- **`moss_generate.py`**：MOSS-Image 独立画图通道（上游 `/moss/*`，Layout JSON + 画面内文字渲染）。
  - **职责边界（防知识污染）**：主模型只给 `idea`（画面构思）+ 参考图/改图编号，不写提示词、不写 bbox/JSON；提示词由**摘要模型**（`model_mini`，`type="summarize"`）在 `/moss/knowledge` 下独立写成**新版混合写法**（`quality_meta_year_safe`/`count`/`character`/`series`/`appearance`/`artist`/`style`/`tags`/`environment` + `nltags` + 可选 `canvas`/`elements`），写完直接 `POST /moss/generate`。MOSS 的 Layout/tag 知识只进摘要模型，主模型 schema 里只有一句功能描述。**采样默认值从 `/moss/schema` 的 `steps.default` 读取**（上游已改 4 步 `res_multistep`；`/moss/info` 的 sampling 是进程启动快照，改代码没重启会过期），模块常量只作兜底。
  - **触发**：工具常驻暴露；`mentions_moss()`（`(?<![a-z0-9])moss(?![a-z0-9])`，忽略大小写）只用于 force 模式判定。本群可用 `rg moss on/off` 关闭聊天触发（`ChatData.moss_enabled`，默认开；关闭后 `get_tool_schemas` 不暴露该工具，只剩指令通道）。另有 `rg moss <画面描述>` 指令（`matcher._submit_moss_command`，在 `identity` 权限检查后、`cmd.execute` 之前拦截，以便拿到本条消息/引用回复的参考图）直接走同一流水线，不经主模型；`rg moss` 不带参数时显示当前开关与用法。
  - **参考图**：从 `tg._visible_images` 按 `[图片N]` 取图 → `image_cache.resolve_url(force_base64=True)` → 落盘 **`%TEMP%/atrI_moss_refs/<task_id>/<序号>_<md5>.<ext>`**（每任务独立目录，绝不复用共享路径；24h 后台清理）→ 作为 `reference_images` 绝对路径送上游；**仅上游在本机时传路径**（远端 ComfyUI 需自行上传）。最多 4 张，同时作为 `image_url` 给摘要模型看。**可见图片表在 `run()` 入口、任何 await 之前同步快照**（队列查询等耗时不影响编号对应关系）；日志记 `reference_sources`（编号/去掉 query 的 URL/md5）便于复查串图。
  - **改图（VAE / p 图）**：工具参数 `edit_image`（`[图片N]`）→ 底图落盘同目录 → payload 带 `edit_image` 绝对路径 + 固定 `edit_denoise`；上游 `LoadImage → ImageScale → VAEEncode` 作为初始 latent，**未给宽高时按原图尺寸各向上取整到 16 的倍数**（保持构图/比例）。与 `reference_images`（TE 语义参考）是两条独立路径，可同时用。指令通道在**单图 + 改图意图词**（改/p图/微调/换成/换装…）时自动走 edit，否则仍走参考图。
    - **口径：整体合理度 > 逐像素还原度**。改图好不好先看成图是否自洽可信（文字通顺、版式对齐、无伪文字/残肢/错位），再看像不像原图。三组同图对照（换角色、评论区改名+换头像、黑白漫画换角色+改三格）证明 VAE 明显优于 TE：TE 只在「照着这张画一张新的」时用。
    - **`edit_denoise` = 固定 0.70，不暴露**（`_EDIT_DENOISE_FIXED`）：不按场景分支、不进主模型 schema、也不让摘要模型输出（多输出也忽略）。0.70 是两类场景都能接受的通用解：漫画（换角色+三格改递进）分格/镜头/气泡全保、台词全对、格序正确、无伪字；评论区截图版式与金额保得住，后两行与小字会漂（模型能力上限，参数高低无差别）。实测更低无收益：0.60/0.50 在漫画里推不动「吃胡萝卜」这个改动（画面仍停在原图的烤鱼/蔬菜）。
    - **`_normalize_edit_denoise()` 死区护栏**（仅用于兜住外部直调接口的值）：0.33-0.58 一律抬到 0.62——这一档把原图笔画留成噪声引导，文字必然退化成读不出来的假字。
    - **`denoise=1.0` 不能用于改图**：1.0 时采样从纯噪声起步、`VAEEncode` 的 latent 被完全丢弃，**模型其实没看到原图**——全靠摘要模型转写 + elements 框位重画，构图/镜头/角色比例全变、还丢气泡改错台词。那是「重画」不是「改这张图」。
    - **改图步数下限 6**（`_MOSS_EDIT_MIN_STEPS`，非改图仍用 schema 默认 4）：同一张四格漫画同 denoise 实测——4 步底部出伪字（「烤鱼卜呢？」「急答萝卜呢？」）、6 步伪字消失且情绪递进最完整、8 步无伪字但出现悬空胡萝卜/分格顺序被打乱且更慢。
    - **提示词按「成品描述」写，不写改动方案**：摘要模型收到的是「你看到的那张图只是参照物，输出成图本身的描述」，并明确禁止「保留/不变/替换/改成/原本/原图」这类改动口吻；主模型的 `idea` 参数描述同样要求「直接描述成品最后长什么样」。另有 `[构思是唯一准绳]` 块：构思优先于参照图、点名项一条都不许漏/换/弱化、不许新增主体或整条内容、输出前自检落点。
    - **带文字的图：只承诺写得下的字**。字号够大的关键文字（用户名/标题/正文/按钮/金额/台词）逐字写进 `elements.text`；**约 20px 以下的小字（时间戳/点赞数/菜单项/卡片小注）不给元素、不承诺逐字**——在 1MP 画布上这档字号渲染出来必然是伪字，宁可一句话带过甚至留白。实测同一张评论区截图：全量转写 → 满屏伪英文；按这条精简后 → 小字变成「通顺但不对」的中文，再无乱码。**验收标准是「字形通顺 + 语义合理」，不是逐字还原。**
    - **不要拿 caption 模式去「省掉」elements**：同一张截图去掉 elements 走纯 caption，模型完全没有文字锚点，整屏内容直接重编（金额都会变成另一个数）。要减的是元素内容，不是整份 elements。
    - **分辨率不放大（已证伪）**：一度以为「像素不够所以小字变伪字」，把底图放大 1.5×。用**手写固定提示词**做对照后推翻了——同一份 layout 在原生 912×848 上重画，用户名、正文、四个点赞数全部正确。之前必须放大才有好结果，是**摘要模型每次重写提示词**造成的假象。`_edit_canvas_size()` 现在只做 16 对齐，别再往「提分辨率」上找解法。
    - **真正的变量是摘要模型的输出稳定性**：同一底图 + 同一构思，两次调用给出的 elements 数、bbox 精度、是否合并徽章都不同，渲染结果能天差地别。现在的对策：① 摘要模型用**独立档位** `reasoning_effort_mini`（默认不配 = 请求里不传，交给供应商默认；调高主对话的 `reasoning_effort` 不会连带把后台摘要拖慢；要给出图任务加规划预算就显式写 `reasoning_effort_mini: high`）；② 提示词里给硬性框位规范（一文本块一框、框高≈字号 1.5 倍、徽章并进相邻文本框）；③ 改图模式下首版**没给 elements** 就带一句提醒重试一次。
    - **漫画排版从右往左**：分格顺序、气泡顺序都是右→左、上→下；任务书与上游 `moss_expert.md` 都要求写明「右下/中间/左下」位置，否则带递进的连环会左右写反。
  - **内容尺度（破甲）**：`get_chat_unlock_content_limit(chat_key)` 为真时把 `MOSS_UNLOCK_RULES` 一并注入摘要模型（与 anima 漫画兜底的 `MANGA_UNLOCK_RULES` 同口径，措辞改成「写进自然语言 caption」）；人设库 nsfw 条目的取舍用同一个开关。主模型侧由 S1 响应规则里那条解锁文案覆盖（已补上「moss 写进 idea 画面构思」）。
  - **出图/回传**：`_check_queue()` 队列 >3 拒绝；后台任务完成后直接 OneBot 发图，消息带 `任务编号：moss-XXXXXX`（与普通模式 anima 一致）。**工具返回只给编号、不提预计耗时**（用户口径：画完自然带出即可）。摘要模型输出非法/调用失败时**退化为纯 caption**（`prompt=idea`）照常出图；Layout 被上游 400 拒绝时再退一次纯 caption。
  - **上下文防污染**：工具结果与 `generate_anima_image` 一样进 `IGNORED_TOOLS`（tool 原文不进历史），assistant(tool_calls) 由 `[作画记录]` 摘要替代（画面内容取 `idea`）；assistant 正文里的 `moss-XXXXXX` 在 `chat_prompt._message_text_for_prompt` 被替换为 `[请调用 generate_moss_image 画图工具获取编号]`。
  - 画幅由系统钉死（生成：`_CANVAS_BY_ASPECT`：1:1→1024²、3:2→1536×1024、2:3→1024×1536、16:9→1280×720、9:16→720×1280；改图：按原图尺寸向上取整到 16 的倍数），bbox 由 `_sanitize_elements` 钳进画布，steps 钳在 schema 的 4–12（默认取 schema，上游当前 4），不吃摘要模型给的 `width/height/positive`。

## `chat.py` 及 Mixin

- **`chat_history.py`**：`update_chat_history_row`（触发 user 写 `user_id`；context_only 传 `image_meta`）、`save_tool_messages`（修双拼工具名、剔除未注册工具、deep copy 后规范化，返回 assistant tool-call 消息供摘要绑定）、`remove_last_prompt_user_message`、`cleanup_after_bad_request`、`_trim_prompt_messages_without_summary`、`_cleanup_orphan_*`、`update_chat_history_row_for_user`。
- **`chat_prompt.py`**：`get_chat_prompt_template`、`_build_openai_history_messages`、`_trim_messages_to_request_budget`、`_apply_image_policy`、`_image_expiry_cutoff`、`_is_context_only_message`、`_find_trigger_msg_idx`、`_last_trigger_sender`。响应规则文案集中在模块级 `build_response_rules(unlock_content_limit, no_think)` + `format_response_rules`（离线 A/B 测试直接复用，改规则只改这一处）。历史 assistant 中的任务编号替换为占位符 `[请调用 generate_anima_image 画图工具获取编号]`（常量勿改）；MOSS 编号 `moss-XXXXXX` 走独立正则替换为 `[请调用 generate_moss_image 画图工具获取编号]`（先于 anima 前缀正则执行，避免留下「任务编号：」）。`_apply_image_policy` 在 `item_to_msg_idx` 构建后回写 normal_messages，之后不得再插入/删除消息。
- **`chat_summary.py`**：`generate_tool_call_summary`、`_compress_prompt_messages_if_needed`、日志保存。
- **prompt 结构**：`[S1 角色+响应规则+工具规则+画图短规则][S2 extra_prompt?][S3 压缩摘要?][S4 当前状态：群记忆+日期][历史轮: system印象/人设? user assistant system工具摘要? user context_only?]...[system 触发者印象/人设?][user context_only（本轮 flush）?][system [当前触发](+记忆提醒?)][user 触发句]`。S4 放头部（低频变化，保住整段历史的前缀缓存）；`[记忆提醒]` 与 `[当前触发]` 在尾部（每轮必变段）。
- **响应规则要点**：段数上限读 `REPLY_MAX_SEGMENTS`；口语化、避免 AI 腔（**按线上回复统计逐条落地**：不用「——」引出解释、不给自己的用词加引号强调、不写「不是…而是…」类对称排比、不用「一句话/说白了/说到底/总的来说」做总结；少用「不过/所以/其实」起头；结尾不反问不总结不留「说一声/需要的话」话头；不浮夸比喻、不感叹式开场，附和可极短）；`[群聊上下文-非触发消息]` 块是背景，未被点名不主动回应或点评其中图片；只回应当前触发消息与其发送者，顺着对方话题走、不主动拐题，不合并话题；不用 Markdown（`ENABLE_MSG_TO_IMG` 时允许）。模型特有的语癖（Claude 的「结论：」小结与共情长句、GPT 的分点复述、mimo 的发布会腔、GLM 的「作为一个 AI」等）按 profile 写进 `extra_prompt`，**公共规则只放跨模型共性，`extra_prompt` 不得重复 S1 已写的条目**——一句话原则：公共规则管「所有模型都会犯的」，`extra_prompt` 只管「这个模型特有的」。
- **上下文窗口**：轮数只计非 context_only 的 user。`CONTEXT_WINDOW_SIZE` 为窗口压力触发摘要后的目标窗口，基础缓冲 = 窗口 + int(窗口×`CONTEXT_COMPRESS_THRESHOLD_RATIO`)。启用摘要时，`CONTEXT_SUMMARY.pressure.max_rounds` 为请求侧硬轮数上限，缺省时在基础缓冲外额外保留 3–10 轮。token 硬预算照常生效。
- **孤立历史清理**：`_cleanup_orphan_history_messages()` 按**计数**状态机：非 context_only user `open_users +1`，普通 assistant `-1`，计数为 0 时的 assistant 丢弃；tool_calls 链保持开轮至最终 assistant。用计数而非布尔：循环邮箱会产生 `user(A) user(B) assistant(答A) assistant(答B)`，布尔"首条 assistant 关轮"会丢答 B（持久化 `_serializable`/`_load_from_dict` 同样计数）。裁剪、摘要、400 清理、prompt 构造前、持久化都用同一逻辑。
- **Token 截断**：`_cal_text_tokens` 模块级 LRU；单图估算 `IMAGE_TOKEN_ESTIMATE = 1000`。预算裁剪按最旧完整轮删，保护头部 system 与触发消息。
- **摘要压缩**：`CONTEXT_SUMMARY.pressure` 配置连续对话的提前量 `round_headroom`（默认 2）和 token 预算比例 `token_budget_ratio`（默认 0.875）。会话与邮箱收尾后，按 `CONTEXT_SUMMARY.idle.minutes` 等待空闲（默认 30 分钟），保留 `idle.keep_rounds` 轮（缺省为正常目标的一半，向上取整）。候选旧轮扣除摘要增量预计可省至少 `idle.min_saved_tokens`（默认 2048）才启动，成功后直接使用新摘要。新触发取消等待，并阻止进行中的空闲摘要替换仍在使用的历史。摘要只在完整回复后的边界切分，成功后按 `id()` 删旧段，前导印象和 context_only 随绑定轮处理。失败 120 秒冷却，任务退出清理 `_compressing_overflow_item_ids`。摘要软限用 `max_summary_tokens` 或 `CONTEXT_SUMMARY.target_chars`（默认 800），硬截 2 倍，输出 `[当前话题]`/`[群历史]` 两节。
- **用户印象**：依据本次溢出中该用户的 user 消息生成，多用户 `asyncio.gather` 并发；软限 `IMPRESSION_TARGET_CHARS`（200）。
- **记忆**：两种、无开关——群记忆 `ChatData.chat_memory`（按群，本群所有人格共享）、个人记忆 `PersistentDataManager._global_user_memories`（按用户，跨群跨人格共享）。旧的 `rg mem global` 开关（`global_memory_enabled`/`global_chat_memory`）与人格级群记忆 `PresetData.chat_memory` 已移除，统一为原开关打开时的行为；`ChatData._init_from_dict` 加载时依次合并 已有 `chat_memory` → 旧 `global_chat_memory` → 各人格旧 `chat_memory`，同名不同值改名 `键_人格名` 保留，旧字段不再落盘。超限只警告，由 LLM `consolidate` 整理；群记忆提醒在尾部，用户记忆提醒在印象 system。`rg reset` 不清记忆/印象。
- **工具调用摘要（模式 3，唯一路径）**：搜索类（`tavily_search`/`browse_url`）同步一次成稿为 `[搜索工具摘要]`；其他工具截断原文 `[调用结果]`；`generate_anima_image` / `generate_moss_image` 结果不进历史，改为 `[作画记录]`（anima 取 tags/nltags，moss 取 idea，并声明新作画请求必须重新调用对应工具）。摘要绑定 `save_tool_messages()` 返回的 `target_msg`。历史注入：tool 原文不进 prompt，assistant(tool_calls) 有摘要→摘要 system 替代，无摘要→跳过。
- 实例变量（`_compress_task`、`_pending_overflow_*`、`_compressing_overflow_item_ids`、`_compress_failure_time`、`_persona_cache*`）不得共享类状态。

## `matcher.py`

- `utils.gen_chat_payload()` 提取文本/图片 URL；允许纯图片消息。`do_msg_response()` 锁内独立算 `should_reply`：False 写 `_recent_context_buffers` 即返回；True 在节流后 flush 缓冲、构建 prompt、写 `tg._visible_images`、调 `stream_response()`。
- **`_on_reply_complete`**：每个完成回复恰好一次：流式收尾、发工具图、`save_tool_messages` + 同步工具摘要、落库群历史与用户维度历史（`reply_entries` 非空时归属批次内各用户）、记 `回复完成 | prompt=X cached=X(xx%) ...` 日志、漫画兜底检查（`_maybe_manga_autodraw` 按 `reply_entries` 锚定，尾部仅补查"无回复完成"或"最近检查后又有工具调用"）。**moss 视为已画**：`_maybe_manga_autodraw` 先扫 `generate_moss_image`，命中就 `mark_manga_drawn` 后直接返回——既不再强制补画，也不触发空闲兜底（`openai_func` 侧同一口径：`has_draw_call` 对两个画图工具都置位）。
- **重试链**：图片 400 → 直传转 base64 重试一次 → 清历史图片无图重试（≤2 次）；空 content 400 填 `[无内容]` 重试；空响应清历史至 5 条重试一次；content 混入工具 XML 时注入提示重试。
- **Reply + At 修复**：OneBot 删 reply 段后 at 段丢失 → 检查 `original_message` 补 `to_me`。
- **唤醒**：前缀唤醒词/角色名句首触发（先剥掉前导 `[图片N]` 占位再判句首，「图片后接兔酱…」也能唤醒）；名称提及走 `REPLY_ON_NAME_MENTION_PROBABILITY`；句中/句尾走 `RANDOM_CHAT_PROBABILITY`。**唤醒判定只看纯文本段**（`_wake_text`，matcher 在注入回复上下文前拼好）：`gen_chat_payload` 会把 at 段渲染成「@昵称」放在句首、回复消息还会被加上 `[回复 … 的消息]` 前缀，用整段 `trigger_text` 判句首会让「@别人 + 引用某条 + 兔酱…」永远唤不醒（实测漏回复，`REPLY_ON_NAME_MENTION_PROBABILITY=0.1` 只剩 10% 概率）。
- `_chat_response_lock` 保护 `_chat_running_tasks`/`_chat_active_inputs`；`loop_data` 默认 `None`。
- **`rg moss` 指令**：`identity`（`rg`）处理器在权限检查后、`cmd.execute` 之前用 `_submit_moss_command(raw_cmd, event, bot, chat_key)` 拦截 `rg moss ...`：
  - `rg moss on|off` 切本群聊天触发开关（`moss_generate.set_moss_enabled` → `ChatData.moss_enabled`，持久化；关闭后 schema 不再暴露 `generate_moss_image`，只能靠指令提交）；
  - `rg moss <画面描述>` 直接调 `moss_generate.submit_direct()`（摘要模型写词 → `/moss/generate` → 发图），不进主模型；参考图取本条消息 + 引用回复里的图片（`_collect_moss_refs`，最多 4 张）；
  - 不带参数时显示当前开关与用法。
  指令通道不受 `draw_mode` 与 `moss_enabled` 影响（只受 `MOSS_ENABLE` 与上游可用性约束）；draw off 时工具本身随 `_DRAW_ONLY_TOOLS` 从 schema 过滤。

## `persistent_data_manager.py`

- `ChatMessageData`：`user`/`assistant`/`tool`/`system`；`context_only`、`is_impression` 加载时强制 `role="system"` 且不持久化；`image_meta` 与 `images` 平行（仅 context_only 用）；`character_text`/`character_versions` 为印象块里的人设段（仅 `is_impression`）。
- `PresetData`（印象、`prompt_messages`、摘要；旧人格级 `chat_memory` 加载时暂存 `_legacy_chat_memory` 交 ChatData 合并）、`ChatData`（`chat_memory` 群记忆、`active_profile`、`draw_mode`、`draw_model`、`manga_mode`、`manga_style`、`moss_enabled`、`unlock_content_limit`）、`_global_user_memories`（个人记忆）、`_custom_nicknames`。
- 默认 `data/naturel_gpt/naturel_gpt.json`；`save_to_file()` 60 秒节流（`must_save=True` 跳过），事件循环运行中卸载到执行器线程（`_SAVE_LOCK` 串行），原子写 `.tmp` → `os.replace`；`save_to_file_blocking()` 仅 shutdown 用。加载/保存过滤孤儿 assistant/tool（计数版）、内部异常文本。

## `command_func.py`

- `rg` / `rg list` / `rg set <persona>`；`rg draw [force/on/auto/off | <model> | <json> | -XXXXXX]`；`rg turbo`（兼容别名）；`rg manga [on|off|画风|clr]`；`rg nolimit [on|off]`；`rg model [profile] [effort]`（effort 见「思考强度会话覆盖」）；`rg nn [昵称]`；`rg mem` / `rg mem clear <group|user|all>`（`rg mem` 有昵称门槛：未用 `rg nn` 设自定义昵称时直接提示先设昵称，不展示记忆）；`rg help`；`rg stat` / `rg stat reset`；`rg reset`（只清上下文）。
- `cmd.register()` 用 `params: Optional[list] = None`；`execute()` 返回 `{'error': str(e)}`；`rg draw-XXXXXX` 返回 `no_img: True` 强制纯文本。

## 其他

- **`draw_db.py`**：SQLite `data/naturel_gpt/draw.db`，表 `draw_prompts(task_id PK, prompt_data, created_at, updated_at)`，`threading.Lock`。
- **`utils.py`**：`gen_chat_payload()`、`_extract_message_text_and_images()`（`[图片N]`）、`async_fetch()`、用户名解析容忍 API 失败。
- **`singleton.py`**：双重检查锁；**`chat_manager.py`**：全局会话管理器；**`text_to_image.py`**：依赖 `nonebot_plugin_htmlrender`，导入失败自动关。

# Prompt 与回复规范

- 像真实群聊成员自然简短回复，段数不超过 `REPLY_MAX_SEGMENTS`；正常文本不用 Markdown。分段用 `\n\n`。
- 工具调用过程不出现在最终回复；`[调用结果]`/`[搜索工具摘要]`/`[作画记录]` 只是上下文参考，禁止模仿其格式。

# 配置字段速查

## LLM

- `OPENAI_PROFILES`：`default`（指针，值为 profile 名）/`api_keys`/`base_url`/`proxy`/`protocol`（`openai` 默认走 `/chat/completions`；`anthropic` 走 `/v1/messages`；`gemini` 走 `/v1beta/models/{model}:generateContent`，均见下）/`timeout`/`model`/`model_mini`/`max_summary_tokens`/`multimodal`/`model_vision`/`extra_prompt`/`thinking`/`no_think`（true 时注入 `/no_think`）/`keep_reasoning`（默认 false，历史 reasoning 剥离；true 时 provider 拒绝自动剥离重试）/`postprocess`（**后处理开关数组**：`strip_thought_prefix`/`think_leak_retry`/`no_think`/`keep_reasoning`/`no_stream`，见下条；旧的独立布尔字段仍兼容）/`reasoning_effort`（**仅主对话**）/`reasoning_effort_mini`（**摘要/印象专用**，未配置 = 请求里完全不传）；**可选**：`temperature`/`top_p`/`frequency_penalty`/`presence_penalty`/`max_tokens`。
  - **采样参数与 `max_tokens` 一律「配了才发」**：`_completion_kwargs` 里这些字段为 None/缺失就完全不出现在请求体（**没有兜底默认值**）。思考类模型会把 `max_tokens` 额度耗在 reasoning 上，一旦撞上限就 `finish_reason=length`、`content` 空或半截（表现为「莫名不回复」）— —所以当前各 profile 都不配 `max_tokens`，交给 provider 默认上限。要限长只能显式写进 profile。`max_summary_tokens` 不是 API 参数，只作摘要字数软目标（`chat_summary`）。
  - 当前默认：`OPENAI_PROFILES.default: ds` → `deepseek/deepseek-v4.1-flash`（多模态，主模型与 `model_mini` 同款；旧 `mimo-v2.5` 已下线替换）。`api_keys` 第一顺位即为首选 key（见「多 API Key 策略」）。**思考强度（11-11 起）**：`ds` 聊天 `max`、`kimi` 聊天 `high`，其余 profile 一律不配 `reasoning_effort`（交给供应商默认）；所有 profile 都没配 `reasoning_effort_mini` → 摘要/印象一律不传该参。
  - **网关实测（09-22）**：`xiaomi/mimo-v2.6-pro` / `xiaomi/mimo-v2.6-flash` 支持 chat/completions + 流式 + 原生 tool_calls + 图片（含流式请求内联图）；思考**关不掉**——`thinking:{"type":"disabled"}` 被接受但忽略，`reasoning_effort:"none"` 被网关 400 拒（合法值仅 `low|medium|high|xhigh|max`），low/high 对 `reasoning_tokens` 无可测影响（rtok 只在难度上区分：简单题 ~20 vs 鸡兔同笼 ~90）。故统一 `reasoning_effort: low`。
  - **TeamoRouter 实测（10-17 首测，11-17 更新，profile `argon` / `gemini`）**：`base_url: https://api.teamorouter.com/v1`，主模型分别是 `gemini-4-argon`（Google）、`gemini-4-hydrogen`（Google），`model_mini` 分别是 `claude-haiku-5-5`、`gemini-4-hydrogen`（同主模型）、`claude-haiku-5-5`。**该域名国内被 SNI 阻断**（TLS 握手 0.02s 即 RST，`curl`/`httpx` 全灭；用 IP 直连 + Host 头可通，证明是 SNI 层拦截），必须 `proxy: socks5://127.0.0.1:7789` + `use_socket_proxy: true`；且其 IP（腾讯云香港 119.28.192.209）会被订阅里的 `IP-CIDR,119.28.192.0/19,Direct` 判直连 → Clash 需一条更靠前的 `DOMAIN-SUFFIX,teamorouter.com,AI`（复用订阅自带 `AI` 组，实测 hitCount 递增）。**`gemini-4-argon` 不吃 `reasoning_effort`**（default 187 / low 220 reasoning tokens，无差别，思考照常 ~200 tokens）；`glm-5.3-flash` 吃（139 → 0，延迟 5.4s → 2.9s），但摘要走 `model_mini` 时按设计不传 `reasoning_effort`，摘要仍会思考。Claude 两档走 OpenAI 兼容端点可用（流式 ~3.4s），但官方文档明确：**Claude 用 `/v1/messages` 原生协议才有 prompt cache / thinking，OpenAI 兼容格式会丢这两项、成本与质量都吃亏**——本插件只有 OpenAI 兼容通道，介意的话别拿 Claude profile 当主力。
  - **commandcode 的 Claude 是地域限制，不是套餐问题（11-17 纠错）**：直连（中国大陆出口）调 `/provider/v1/messages` 一律 403——`claude-sonnet-5-5` 回 `MODEL_NOT_IN_PLAN: ... available in Pro and above plans or extra on demand usage`（**误导性错误**），`claude-haiku-5-5` 回 `permission_error / The provider refused this request`（对应文档的 `403 upstream_refused`）；**同一把 key 走 `socks5://127.0.0.1:7789` 立刻 200**，所以是 CC 按地区拒绝（Anthropic 不向中国大陆提供）。CC 官方 geo 表只列了 `GPT-5.6 Luna` 和 `Gemini 3.7 Flash` 在中国不可用，**Claude 没写但实际也按地区判**。额度上限（GOAT）：Claude Sonnet 5.5 $10/月、Haiku 5.5 $20/月（按公开价），Pro($20)/Max 才有全量 premium；`Provider` 计划 $15/mo + $1.01 手续费是专供 API 的 pay-as-you-go 档（含 $15 credit，无 5h/周窗口）。**`sonnet` profile 现在挂 CC + socks5 代理**；其它 CC 模型直连即可，别跟着加代理。另外 CC 的 `/chat/completions` 与 `/responses` 都可用，Claude 只能 `/messages`。
  - **Gemini 4 的 thought summary 会混进正文（11-17 实测，profile `gemini`）**：TeamoRouter 的 `gemini-4-hydrogen` / `gemini-4-carbon` 会把 Gemini 的思考摘要**直接写进 `content`**，形态是开头几段纯英文（`Initial hypothesis: ...` / `I'm thinking through ...` / `The task is to ...`，段间 `\n\n`）再接正文，**没有任何 reasoning_content / reasoning 字段**，所以 `_strip_think_tags`、`LLM_SHOW_REASONING`、`think_leak_retry` 全部失效，按 `\n\n` 分段时思考会被当成一条条消息发进群，还会经 `model_mini` 污染 `chat_summarized`。实测结论：① **与流式无关**——非流式摘要 4/4 仍以 `The request is for...` 开头（completion 563~1133，思考 token 照烧）；② **与思考档位无关**——`low/medium/high/max`、`minimal/off/0/none`、`thinking:{type:disabled}`、`reasoning:{exclude:true}`、`include_reasoning:false` 全部无效；③ **不是端点整体的锅，也不是 hydrogen 特有**（11-17 横评 TeamoRouter 12 个模型 + commandcode 5 个）：同端点上 `gpt-5.6-sol` / `gpt-6-sol` / `kimi-k3.1-pro` / `glm-5.3` 干净，`deepseek-v4-pro` / `qwen4-max` / `mimo-v2.6-pro` / `grok-4.7` 干净**且能正确回 `reasoning_content`** —— 说明网关本身支持思考分离，**唯独 Gemini 系没走这条通道**（thought summary 被拍平成 text）；commandcode 侧 `google/gemini-3.8-flash` 3/3 干净，但 gemini-4-* 套餐 403。**泄漏是偶发的、与任务相关**：摘要型 prompt 下 `gemini-4-hydrogen` 1/1、复测 1/3 中招，`gemini-4-carbon` 稳定中招，`gemini-3.8-flash` 4 次里中过 1 次，`gemini-4-argon` 3/3 干净 —— 所以**换模型只能降概率、不能根除**，`strip_thought_prefix` 是必需项；④ **换 Anthropic 协议也没用**——`POST https://api.teamorouter.com/v1/messages` 收 `gemini-4-hydrogen`，但思考仍是 `{"type":"text","text":"I'm thinking through..."}` 的普通 text block（不是 `thinking` block），显式传 `thinking:{type:"enabled"}` 时网关直接把思考吞掉、`content` 返回空且 `stop_reason=max_tokens`（512 token 全被思考烧完）；况且 anthropic 协议下本插件不发 `thinking`、`reasoning_effort` 不生效、工具轮还要转 tool_use/tool_result，只会更绕。**当前对策**：该 profile 直接走 Gemini 原生协议（`protocol: gemini`，见下条），思考走独立 `thought: true` part → `reasoning_content`，不会混入正文；`postprocess: [strip_thought_prefix]` 只作兜底，**流式已恢复**（原生端点 `:streamGenerateContent` 同样只把正文推给 content，实测 stream=1 / full=0）。已知残余：模型偶发把英文思考句插在中文段落中间（如 `Uncertain if "ba" unambiguously refers to...`）或把摘要写两版（草稿＋正式），块级前缀剥离处理不了。想彻底免维护只能换非 Gemini 系模型（gpt/kimi/glm/deepseek 等，实测不吐 thought 或能正确走 reasoning_content）。
  - **Anthropic 原生协议（profile 设 `protocol: anthropic`）**：传输层翻译放在 `anthropic_func.py`，进出口都是 OpenAI 风格，所以 `_stream_once` / `_complete_once` 上层逻辑（工具轮、reasoning 分流、usage、重试）零改动。
    - 请求方向：开头系统指令放顶层 `system`；印象、人设、工具摘要、触发标记和循环提醒按原位置渲染为标有 `[系统补充]` 的 user 文本块（CC 拒绝 `messages` 内的 system 角色）；assistant `tool_calls` → `tool_use`；`role: tool` → 归并到**同一条** user 消息开头的多个 `tool_result`，轮次提示排在整批工具结果后；`image_url` → data URI 走 `source.base64`、http URL 走 `source.url`；`reasoning_content` **必须丢弃**（没有 signature，转成 thinking block 会被 400）。
    - 响应方向：Anthropic SSE（`text_delta` / `thinking_delta` / `input_json_delta` / `message_delta`）转成 **OpenAI 同构 chunk**，工具调用按 `content_block.index → tool_calls.index` 映射，首帧带 id+name、后续只带 `arguments` 增量。
    - 两处约定：`tool_choice: "none"` 走原生 `{"type": "none"}`（旧实现是「干脆不传 tools」，模型会因此看不到工具描述）；`max_tokens` 缺失时兜底 **16000**，**配了就按 ×8 发送再收敛到 128000 上限**——Anthropic 的 `max_tokens` 和 Gemini 的 `maxOutputTokens` 一样**把 thinking 算进去**（实测 `max_tokens=300` + 困难任务 → thinking 吃掉 300、正文为空、`stop_reason=max_tokens`；×8 后思考 134 + 正文 236 正常收尾）。
    - **prompt caching**：CC 需要**逐块**打 `cache_control: {type:"ephemeral"}`。断点落在 system 末块和前一条对话消息的最后一个可缓存块。头部包含摘要或当前状态时，在第一个可变块之前额外打断点，使更新后仍可复用稳定头部。Anthropic 的 `input_tokens` **只算未命中的新增部分**，`_usage_to_openai` 把 `fresh + cached + created` 合成 `prompt_tokens`，明细放 `prompt_tokens_details.cached_tokens` / `cache_creation_tokens`。
    - **adaptive thinking（profile `anthropic_thinking: adaptive`，默认不配=不发）**：新版 Claude 只认 `{"type": "adaptive"}`，`budget_tokens` 已移除（传了直接 400）。开启后**思考只回 signature、不回正文**（`thinking` block 的 text 为空，`output_tokens_details.thinking_tokens` 才是计量），所以 `reasoning_content` 基本是空的——收益在推理质量而非「看思考」。signature 随 assistant 消息存 `_anthropic_thinking_blocks`：流式靠 `signature_delta` 累积进 sink，非流式从 content block 直接取；`split_messages` 回传时**必须排在 content 最前面**。实测普通轮不回传也不 400，工具轮带上正常。`reasoning_effort` 在 anthropic 协议下依然不生效。
  - **commandcode 端点稳定性横评（11-17，11 个模型 / 多场景）**：① **思考泄漏 33 次 0 次混入**（每模型 3 次摘要型 prompt）——TR 那种「thought 拍平成 content」只存在于 TR，CC 的 chat 端点对 Gemini 系同样干净（`google/gemini-3.8-flash` 累计 7 次 0 泄漏）；② 工具调用 11/11 正确返回 `tool_calls` 且参数 JSON 完整，**两轮工具循环**（tool_call → tool_result → 终答）在 ds/gpt/k3 上全部正常；③ 图片（96×96 纯色 data URI）11 个模型全部识别正确（ds/k3 带 reasoning 也答对）；④ 并发 8/8（ds，2.4s 完成）、6/6（gemini，5.7s）；⑤ 长上下文（3.6K prompt）3/3 正常。**结论：CC 的 `/chat/completions` 足够稳，Kimi 系无需换 `/responses`**——responses 只是把思考放在独立的 `reasoning` item 里，chat 早已给 `reasoning_content`，正文两边都干净（K2.7-Code 累计 11 次 0 泄漏，`think_leak_retry` 在 CC 上不会触发）。
  - **`/responses` 对 Kimi 没有额外价值（11-17 评估）**：responses 只是把思考放进独立的 `{"type":"reasoning","summary":[...]}` item，chat 端点本来就给 `reasoning_content`，正文两边都干净（K2.7 累计 11 次 0 泄漏）。**「把思考摘要带进历史」不建议做**：① 群聊闲聊多几十 token 换不到连续性，反而会让回复变解释性、伤人设；② `responses.previous_response_id` 才是服务端带上下文的正道（实测 `store:false` 也能用），但群聊每轮都有新消息插入、历史还会被裁，链条不连续，用不上；③ 真需要「记住思路」直接开 `keep_reasoning: true`（`reasoning_content` 随 assistant 消息回传，实测 CC chat 端点 200 接受），成本比拼进正文低、比拼 responses 改动小。
  - **Gemini 原生协议（profile 设 `protocol: gemini`）**：传输层翻译放在 `gemini_func.py`，结构与 `anthropic_func.py` 同构，`stream_response` 上层零改动。**这是 Gemini 系绕开「思考混进正文」的唯一干净解法**（见上一条：OpenAI 兼容层会把 thought summary 当 content，原生端点只回答案 + `thoughtSignature`）。
    - URL：`base_url` 去掉尾部 `/v1` 后拼 `/v1beta/models/{model}:generateContent`（流式 `:streamGenerateContent?alt=sse`）；认证用 `Authorization: Bearer <key>`。
    - 请求方向：system 合并到顶层 `systemInstruction`；`role: tool` → `functionResponse`（**靠 tool_call_id → 函数名映射**，Gemini 要求带 name）；图片只支持 `inlineData`（data URI → base64），**任意 http 图片地址会被丢弃**，所以 `chat_prompt._apply_image_policy` 与 `_build_loop_batch_message` 会按 `protocol == "gemini"` 强制 `force_base64=True`；工具 schema 递归清洗（丢 `$schema`/`additionalProperties`/`strict` 等 Gemini 不认的关键字，联合类型收敛成单类型）。
    - **思考开关**：`generationConfig.thinkingConfig.includeThoughts` 恒为 `true`（`reasoning_effort` 的 low/medium/high 映射到 `thinkingLevel`）——**思考要保留，只是不能混进正文**。原生端点会把思考放在独立的 `thought: true` part 里，`stream_to_openai_chunks` / `message_to_completion` 把它转成 `reasoning_content`（由 `LLM_SHOW_REASONING` 决定是否展示、`keep_reasoning` 决定是否带进下一轮），正文只取非 thought part；转换层同样**过滤 `thought: true` 的 part**，即使网关行为异常也不会漏进 content。思考 token 照常计费（`thoughtsTokenCount`）。
    - **`maxOutputTokens` 要留余量**：Gemini 把 thinking token 也算进 `maxOutputTokens`，思考量可达正文数倍，按配置值传会被吃光导致 content 为空，这里固定按 `max_tokens × 8` 发送（未配 `max_tokens` 时该字段不传，交给 provider 默认上限）。
    - **`thoughtSignature` 必须回传**：Gemini 3/4 多轮靠它维持上下文。响应里取签名 → 写入 `request_state["_gemini_thought_signature"]` → 工具轮挂到 `assistant_msg` → `split_messages` 塞回该轮最后一个 part。实测不回传不会 400，但会丢上下文质量。
    - usage 口径：`completion_tokens = candidatesTokenCount + thoughtsTokenCount`，与 Gemini 自己的 `totalTokenCount` 一致。
  - **profile 后处理开关统一成数组（11-17）**：`postprocess` 为数组，取值 `strip_thought_prefix` / `think_leak_retry` / `no_think` / `keep_reasoning` / `no_stream`；由 `config.profile_flags()` 归一，`config.profile_enable_stream()` 判流式。旧的独立布尔字段（`think_leak_retry` / `no_think` / `keep_reasoning` / `strip_thought_prefix` / `enable_stream: false`）继续生效，老配置不必改。`think_leak_retry_chars` 仍可单独覆盖阈值。
  - 新增 profile 是**纯配置改动**：`OPENAI_PROFILES` 是 `Dict[str, Any]`，键名自由，`model` = 主模型、`model_mini` = 摘要/印象及审查降级模型。代码里没有任何 profile 名白名单。
  - **思考强度会话覆盖（`rg model [profile] [effort]`）**：档位 = `low`/`medium`/`high`/`max`（原样透传给 `reasoning_effort`）+ 特殊档 `default`（**请求里完全省略 `reasoning_effort`**，交给供应商默认）+ `auto`（清除覆盖、回到 profile 配置值）。**不给 effort 参数就完全不动覆盖**：沿用本会话上次的档位，从未设过则用 profile 的 `reasoning_effort`；profile 也没配 → 请求里同样不带该字段。语序：`rg model kimi low` 切配置并设档；`rg model low` 只设档不切配置；`rg model kimi auto` 切配置并清除覆盖。
    - 存储：`ChatData.active_effort`（每群独立；`_init_from_dict` 只保留合法档位，脏值丢弃），读写走 `Chat.get_effort_override()` / `set_effort_override()`。
    - 生效点：`matcher._snapshot_request_profile()` 把覆盖写进本轮 profile 快照——`default` 档写 `None`，`_completion_kwargs` 只在非 None 时才发 → 等价于不传；不会污染全局 `OPENAI_PROFILES`（`get_profile()` 返回浅拷贝）。摘要/印象走 `model_mini`，读**独立的** `reasoning_effort_mini`（默认不配 = 不传），因此会话覆盖只作用于主对话，不会把摘要一起调高。
    - 档位常量在 `config.Config`：`REASONING_EFFORT_CHOICES` / `REASONING_EFFORT_DEFAULT_TOKEN` / `REASONING_EFFORT_FOLLOW_TOKEN`（ClassVar，从 `config` 实例访问）。
- `OPENAI_ACTIVE_PROFILE`（旧字段，兼容回落）、`LLM_ENABLE_STREAM`、`LLM_SHOW_REASONING`、`LLM_ENABLE_TOOLS`、`LLM_DISABLED_TOOLS`、`LLM_MAX_TOOL_ROUNDS`、`LLM_MAX_TOTAL_TOOL_CALLS`、`LLM_TOOL_LOOP_MAX_SECONDS`
- 旧扁平字段（`OPENAI_API_KEYS`、`CHAT_MODEL` 等）有 `OPENAI_PROFILES` 时可省略。代码常量：`MAX_SEARCH_TOOL_CALLS=3`、`IMAGE_TOKEN_ESTIMATE=1000`。

## 上下文

- `CONTEXT_TOKEN_BUDGET`（默认 4096）、`CONTEXT_WINDOW_SIZE`、`CONTEXT_COMPRESS_THRESHOLD_RATIO`（默认 0.5，决定基础缓冲窗口）、`CONTEXT_SUMMARY`（`enabled`、`target_chars`、`pressure`、`idle`）、`IMPRESSION_TARGET_CHARS`（200）、`TOOL_CONTEXT_TOKEN_BUDGET`（16384）。旧扁平字段 `CONTEXT_SUMMARY_ENABLED`、`CONTEXT_SUMMARY_TARGET_CHARS` 只在新节未提供对应项时回填。
- `CONTEXT_BUFFER_MAX_AGE_MINUTES`（15）、`CONTEXT_BUFFER_MIN_LINES`（3）：非触发缓冲时间衰减；`CONTEXT_BUFFER_SIZE` 仅兼容

## 多模态 / 分段

- `MULTIMODAL_ENABLE`、`MULTIMODAL_MAX_IMAGES`（4，超限只留触发消息的图）、`MULTIMODAL_IMAGE_FRESH_MINUTES`（60，30 分钟量化）、profile 级 `multimodal`
- `NG_ENABLE_MSG_SPLIT`、`REPLY_SEGMENT_INTERVAL`、`REPLY_MAX_SEGMENTS`；profile 级 `think_leak_retry`、`think_leak_retry_chars`（默认 100 字）
- 分段符归一：`openai_func.normalize_reply_separators()` 把整行分隔线（`---`/`***`/`___`/`===` 等，`_SEPARATOR_LINE_RE`）替换成双换行，接入点是 `matcher.on_text_chunk`（分段前）、`send_segment`、`_send_buffer_segmented`、`_on_reply_complete`（落库）与失败保存路径；只匹配整行分隔符，表格分隔线 `|---|---|` 与正文破折号不受影响

## 工具 / 人格

- `TAVILY_API_KEY`（多 key）、`BOCHA_API_KEY`、`BOCHA_API_BASE`、`BOCHA_SEARCH_COUNT`（10-20）；`COMFYUI_BASE_URL`、`ANIMA_CHARACTERS_ENABLE`（人设库带出，默认 true）、`MOSS_ENABLE`（MOSS 画图，默认 true）、`MANGA_IDLE_MINUTES`、`MANGA_IDLE_ROUNDS`；`WEB_FETCH_TIMEOUT`、`WEB_FETCH_MAX_CHARS`、`PLAYWRIGHT_TIMEOUT`；`NAS_GAME_*`；`UNLOCK_CONTENT_LIMIT`（每群 `rg nolimit` 覆盖）
- `DEFAULT_PERSONA`；人格目录 = 配置文件旁 `personas/`

# 开发规范

- 改动范围默认限于 `ATRI/plugins/nonebot_plugin_naturel_gpt/` 与显式配置/人格。
- 改 matcher 前梳理消息流与 `rg` 指令流；改 prompt 前读 `get_chat_prompt_template()`；改持久化前读 `persistent_data_manager.py`，保持 JSON 兼容。
- 改配置字段同步：`Config`、`CONFIG_TEMPLATE`、迁移/默认值、`README.md`、本文件。
- 新增消息处理同步 `utils.gen_chat_payload()` 与 `matcher.do_msg_response()`；新增指令用 `cmd.register(...)` 保留权限检查；新增工具加文件于 `llm_tool_plugins/`。
- 守住缓存不变式：第 k 轮请求前缀是第 k-1 轮的严格前缀（尾部触发标记/记忆提醒除外）；历史轮写入后不得改写。
- 非必要不执行访问外部 API 的命令。

# 验证清单

- 语法检查所有变更 Python 文件；大范围变更编译核心文件与 `llm_tool_plugins/*.py`。
- Matcher 变更覆盖群聊/私聊/纯图片消息、`rg` 指令、忽略前缀、禁用户/禁群、`at` 与 `at all`。
- 人格变更验证 `.md` 与 skill 文件夹共存；配置变更验证不丢未知字段、正确补默认值。
- 上下文/图片变更：用 `{chat_key}.latest.json` 的 `loop_messages` 回放，检查触发消息只带自身图片、context_only 块图片就地编号、尾部有 `[当前触发]`、`cached_tokens` 比例不降。
- 合并转发变更：离线用例覆盖内联 `content` / `get_forward_msg` / 条数与图片上限 / `image_offset` 编号对齐 / CQ 码串 / 嵌套占位；报错兜底验证 gif 池非空、本地路径失败能转 base64。

# Dice! 骰子插件（nonebot_plugin_dice）

`ATRI/plugins/nonebot_plugin_dice/` 是 Dice! `2.7.1beta1(668)`（C++，`old/nonebot_plugin_dice/Dice-newdev`）指令层的一比一移植。**响应文案、帮助词条、技能表、疯狂表由脚本从 `GlobalVar.cpp` / `RDConstant.h` 提取**（`_msgdata.py` / `_tables.py`，勿手工改），迁移与差异清单见 `docs/dice-migration.md`。

## 结构

- `rd.py` RD / DicePool / COC 作成 / 成功等级（逐行移植，含 C++ 整除向零取整 `idiv`）
- `card.py` 角色卡与 COC7 模板；`session.py` 游戏桌/旁观/牌堆实例/轮盘/日志状态
- `orders.py` BasicOrder + InnerOrder 调度；`event.py` 消息游标与 reply；`formatter.py` `{nick}` 等占位符引擎
- `decks.py` 公共牌堆（yml）+ 牌面展开 `{牌堆}`/`[1d6]`；`settings.py` 本地 yml 配置
- `constants.py` 汇总文案：`_msgdata.py` 为脚本提取产物（勿手改），未复刻指令在 `UNSUPPORTED_HELP` 里剔除、错误文案在 `HELP_OVERRIDE`/`MSG_OVERRIDE` 里覆盖
- `cloud.py` jrrp / rules / 日志上传；`store.py` JSON 持久化

## 触发与阻断

单个 `on_message(priority=1, block=False)`：命中前缀（`. ! 。 ！ ．`）且**实际处理了指令**时抛 `StopPropagation`，后续插件不再收到该消息；未命中（如 `.不是指令`、`。识别`）**不阻断**，避免吞掉其他插件。`.bot off` 后只保留基础指令（`.bot`/`.help`）响应。

## 配置（替代原 WebUI）

`data/dice/config.yml`（全局开关/自称/master/默认房规）、`messages.yml`（覆盖消息文本与帮助词条）、`decks.yml`（公共牌堆）。三者均在插件加载时读一次，**不在消息热路径做 mtime 探测**；改完用 `.system load` 重载、`.system save` 强制落盘。数据：`users.json`/`players.json`/`chats.json`/`daily.json`/`session/*.json`/`log/*.txt`。

自称不写死：`config.yml: self_name` → `DICE_SELF_NAME` → `.env` 的 `NICKNAME`（NoneBot 解析成 **set**，按名排序取首个）→ 兜底「骰娘」。展示用 Master 同理：`config.yml: master` 优先，为 0 时取 `SUPERUSERS`。

## 边界

- 群管（`.group`/`.groups`/`.welcome`/`.dismiss`）、权限（`.admin`/`.master`/`.user`/`.authorize`）、mod（`.mod`/`.reply`/`.str`）、`.link`/`.send`/`.chan`、云黑与敏感词 **不复刻**；信任分级不做、邀请人不记、不主动退群/入群。控制台只留 `.system load`（重读 yml，限主人）与 `.system save`（强制落盘）。
- 权限：`_can_host()` = `NoneBot SUPERUSERS` 或群主/管理员或私聊（对应原版 `canRoomHost()`；`e.is_master` 供 `.system`、`.me` 伪装、全局 `disabled_*` 豁免使用）。群内开关类指令因此需要群管理或主人。
- 未复刻指令**不进帮助索引**（`.help 群管` 回「未找到相关词条」）；原版文案里的错误（`.coc6` 死代码、`.rc 敏捷-10`、`.ww 3#…` 多轮重复、`{reason}` 未替换、`.help 房规` 内嵌整条 setcoc、setcoc 参数写成 0-6 等）已修正，清单见 `docs/dice-migration.md` 第 11 节。
- 对外文案品牌：`kokona` → `tuchan`，作者显示 `langzaigg`（原版署名保留一行）。**接口地址 `api.kokona.tech` / `logpainter.kokona.tech` 不能改**，那是真实服务端点。
- 功能开关保留：`.bot on/off`（总开关）、`.help on/off`、`.jrrp on/off`、`.me on/off`。
- 回环依赖注意：`store → card → formatter → names`，新增模块勿在 `constants`/`formatter` 顶层 import `store`。
