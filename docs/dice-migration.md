# Dice! → NoneBot2 迁移清单

对比基准：`old/nonebot_plugin_dice/Dice-newdev`（Dice! `2.7.1beta1(668)`，C++ 源码）。
移植实现：`ATRI/plugins/nonebot_plugin_dice/`。

响应文案全部来自 `Dice/GlobalVar.cpp` 的 `PlainMsg` / `HelpDoc`，由脚本原样提取到
`_msgdata.py`（356 条消息、148 条帮助词条），**不是重写措辞**。
帮助索引在此基础上剔除了 28 条未复刻词条、并修正了若干错误文案（见第 8、11 节）。

---

## 1. 触发规则

| 项目 | 原版 | 本移植 |
| --- | --- | --- |
| 前缀 | `.` `!` `。` `！` `．`（`init2()` 归一为 `.`） | 一致（`event.normalize_message`） |
| 前导空白 | 先 trim | 一致 |
| 群内 @ 骰娘 | 可省略前缀 | 一致（前导 at 段剥离后仍需前缀） |
| @ 别人 | `isOtherCalled && !isCalled` → 不响应 | 一致 |
| 群内没被点名 | 正常响应（仅受群开关约束） | 一致 |
| 未命中指令 | 不回复、不拦截 | 一致（不会吞掉其他插件的消息） |
| 命中指令 | — | **抛 `StopPropagation`，后续插件不再收到该消息** |
| `.bot off` 后 | 只有 `.bot` / `.help` 等基础指令仍响应 | 一致（`.r` 等转为不拦截） |
| 全局静默 | `DisabledGlobal` | `data/dice/config.yml: disabled_global` |

实测（`test/dice/probe4_block.py`）：

```
普通聊天           骰子回复=0  其他插件收到 ✓
.r 1d6 / 。r / ！r / ．r   骰子回复=1  其他插件未收到 ✓
.不是指令 / 。识别 图片     骰子回复=0  其他插件收到 ✓
@别人 .r            骰子回复=0  其他插件收到 ✓
.bot off 后 .r       骰子回复=0  其他插件收到 ✓
```

---

## 2. 掷骰核心

| 指令 | 状态 | 说明 / 响应 |
| --- | --- | --- |
| `.r [表达式] [原因]` | ✅ 完整 | `RD` 类逐行移植：表达式归一化、`+ - * /`、`X` 乘除、`K` 取高、`F` 命运骰、`P/B` 奖惩骰、`{}` 括号、`>20` 骰排序、`FormStringSeparate/Combined/Complete/Short` 四套格式全部一致 |
| `.rh` | ✅ | 暗骰：公屏回 `strRollHidden`，私聊发 `在<窗口>中 …`，同时私发给本桌 OB |
| `.rs` | ✅ | 只给结果（`FormShortString`） |
| `.r3#d6` | ✅ | 轮数解析、`strRollTurn`（`3#d` 才回显轮数）、多轮拼接 `", "`+`line(8)` |
| `.r 100` | ✅ | 与原版一致：纯数字不是表达式 → 当作掷骰原因，实际掷默认骰 |
| `.w` / `.ww` / `.wh` | ✅ | `DicePool` 移植：`Na` 加骰参数、目标值 8、加骰链、`to_circled` 圈号、`≥100` 骰的统计式展示 |
| 错误分支 | ✅ | `Value/Input/ZeroDice/ZeroType/DiceTooBig/TypeTooBig/AddDiceVal/DiceCnt` 八种错误码对应文案 |
| 骰点统计 | ✅ | `pc.cntRollStat` / `cntRcStat`，`.pc stat` 用 `DiceEst`（含原版用期望均值算方差的写法） |

## 3. 检定与人物卡

| 指令 | 状态 | 说明 |
| --- | --- | --- |
| `.rc` / `.ra` | ✅ | 暗骰 `h`/`_`、轮数 `N#`、难度（困难/极难/极限/简单/自动成功）、`*n` 乘、`+n` 加、`/n` 除、`卡名:属性`、`卡名的属性`、缺省技能值查卡、`SkillDefaultVal` 兜底、房规 0-7 成功等级、隐藏检定 |
| `.sc` | ✅ | `成功/失败`表达式、大失败取最大值、自动回写 SAN、`strSanityRoll` 的 `grade/rank` 分档 |
| `.st` | ✅ | 录入 / `show` / `del` / `clr`、`属性+1d6` 变化、`&表达式`、`卡名::属性`、文本属性、`>9` 位数值保护、`strStDetail` 的 5 种变更行格式 |
| `.en` | ✅ | 成长/增强检定、`+1d10`、`失败值/成功值`、回写角色卡 |
| `.pc` | ✅ | `new / build / redo / show / list / grp / nn / del / cpy / tag / clr / type / temp / tojson / stat` 全部实现，含模板预设、32 张上限、重名/冒号校验、错误码文案 |
| `.coc` / `.coc6` | ✅ | `COC7()` / `COC6()` 文本逐行一致（含 `%d/%d/%d` 三档） |
| `.coc7d` / `.coc6d` / `.cocd` | ✅ | 详细作成文本逐行一致 |
| `.dnd` | ✅ | `4D6K3`、降序、`<10` 补空格 |
| `.ti` / `.li` | ✅ | 10 条临时/总结症状 + 100 条恐惧症/躁狂症表（原样提取） |
| `.setcoc` | ✅ | `show / clr / 0-7`，8 段房规说明文本一致 |
| `.set` | ✅ | 默认骰 1-9999，写角色卡或用户配置 |

## 4. 外部数据（依赖溯洄接口）

| 指令 | 状态 | 说明 |
| --- | --- | --- |
| `.jrrp` | ✅ 实测可用 | `POST http://api.kokona.tech:5555/jrrp`，`QQ=<骰娘QQ>&v=20190114&QueryQQ=<uid>`；当日缓存；接口失败回落本地固定值（对应 `jrrp_local`） |
| `.rule set [规则]` | ✅ | 写当前桌/用户的默认规则 |
| `.rules [规则:]词条` | ✅ 实测可用 | `POST .../rules`，`Name=<URL编码>&QQ=<骰娘QQ>&v=20190114[&Type=Rules-<规则>]`；命中回正文，空回包按 `strRuleNotFound` 处理；找不到时回 `strRuleErr + 具体信息` |
| 凭据 | — | 与原版一致：仅需骰娘自身 QQ 号（`dice_maid_qq`，缺省取事件 `self_id`），无 token |

实测：`.rules 大失败`、`.rules COC7:大失败`、`.jrrp` 均返回真实数据。

## 5. 牌堆与抽牌

| 指令 | 状态 | 说明 |
| --- | --- | --- |
| `.draw [牌堆] [数量]` | ✅ | 会话实例优先 → 公共牌堆临时实例；`_`/`h` 暗抽；抽空回 `strDeckEmpty` |
| `.deck set` | ✅ | `公共牌堆` / `牌堆名=公共牌堆` / `member`（群成员）/ `range 起 止 [步长]` |
| `.deck new` | ✅ | `牌堆名=卡1|卡2…`，支持 `内容::张数::内容` 权重语法 |
| `.deck show` / `reset` / `del` / `clr` | ✅ | 剩余牌列表、重置、删除、清空，10 个实例上限 |
| `.name [类型] [数量]` | ✅ | `随机姓名_<类型>` 子牌堆优先，`1-10` 数量限制，`、` 分隔 |
| `.nnn [类型]` | ✅ | 随机称呼并写入当前窗口 |
| 牌堆文本展开 | ✅ | `{牌堆}` 嵌套、`{%牌堆}` 可放回、`[1d6]` 内嵌掷骰、`\` 转义 |
| 公共牌堆数据 | ✅ | 三个来源，后者覆盖前者：代码内置（`DEFAULT_DECKS`）→ `data/dice/PublicDeck/*.json`（**原版牌堆包可直接拷进来**，格式 `{"牌堆名": ["卡面", ...]}`，多文件合并）→ `data/dice/decks.yml`（本地配置，优先级最高） |

## 6. 游戏桌 / 旁观 / 日志 / 安科 / 先攻

| 指令 | 状态 | 说明 |
| --- | --- | --- |
| `.game new/open/master/set/join/call/close/over/exit/kick/state/rou` | ✅ | 桌号 `名字#序号`，`rou` 支持 `N`、`N*M`（≤100）、`hist`、`reset`、`clr` |
| `.ob join/exit/list/clr/on/off` | ✅ | 暗骰私发旁观者；`no_ob` 开关 |
| `.init list/clr/del` | ✅ | 先攻表按点数降序，`\n1.名字: 点数` |
| `.ri [加值][名字]` / `.ri N#名字` | ✅ | 自动记入先攻表、`init_exp` 记录表达式、批量生成、轮数上限校验 |
| `.ak new/add/del/get/show/clr` | ✅ | `#标题`、`+选项|选项`、`-序号`、`=` 随机抽取并结束本轮、`fork` 传递 |
| `.log new/on/off/end` | ✅ | **玩家发言与骰娘回复都会落盘**（`fwdMsg` / `logEcho`），文件名 `桌号_日志名.txt` |
| 日志文件直发 | ✅ | `.log end` 先调 `upload_group_file` / `upload_private_file` 把文件发到群/私聊 |
| 日志上传 + 润色 | ✅ 实测可用 | 匿名 PUT 到 `https://dicelogger.s3-accelerate.amazonaws.com/<base64url(文件名)>`，成功后回 `strLogUpSuccess` 并给出 `https://logpainter.kokona.tech/?s3=<key>`（即原版「润色」入口）；失败重试 3 次（原版 2 分钟×3，本版 3 秒×3），最终回 `strLogUpFailureEnd` |
| 日志落盘位置 | — | `data/dice/log/<桌号>_<日志名>.txt`（UTF-8，格式与原版一致：`昵称(QQ) 时间\n正文\n\n`） |

实测：`.log new 第一次跑团` → 聊天 → `.r` → `.log off` → `.log end`，文件内容正确，
上传后 `logpainter` 页面 HTTP 200。

## 7. 其他

| 指令 | 状态 | 说明 |
| --- | --- | --- |
| `.help [词条]` | ✅ | 裸指令回版本+引导；精确命中回词条；唯一近似词条回 `strHelpRedirect`；多近似词条回 `.help xxx` 列表；未找到回 `strHelpNotFound` |
| `.help on/off` | ✅ | 群内开关（功能开关保留） |
| `.bot` | ✅ | `试验型 Dice! by 溯洄 & Shiki Ver 2.7.1beta1(668) on\nNoneBot2(OneBot v11)\n使用.help 查看<骰娘>帮助文档` |
| `.bot on/off` | ✅ | **骰子插件总开关**（原「停用指令」群选项） |
| `.nn [称呼]` | ✅ | `del` / `clr` / 50 字节上限 / 写群或全局称呼 |
| `.me [动作]` / `.me [群号] [动作]` / `.me on/off` | ✅ | 第三人称动作、私聊指定群发送、`禁用me` 开关 |
| `.jrrp on/off` | ✅ | 群内开关（功能开关保留） |
| 消息格式化 | ✅ | `{nick} {pc} {self} {at} {at:xxx} {print:master} {case:…} {grade:…} {sample:…} {var:…} {ran:…} {len:…} {expired:…}`、`&消息键` 重定向、`\{` 转义、`ResList` 分页与分行 |
| 角色卡模板 | ✅ | COC7 模板（9 项主属性 + 100 条技能默认值 + 120 条武器表达式 + 别名替换） |
| 会话持久化 | ✅ | `data/dice/session/*.json`（桌号/规则/GM/PL/OB/轮盘/牌堆/日志/自定义表） |

---

## 8. 未复刻项与替代方案

原版这些能力依赖 WebUI / 云服务 / QQ 平台权限，按要求（群管、权限管理不复刻）改为本地 yml：

| 未复刻 | 原版作用 | 本移植替代 |
| --- | --- | --- |
| `.group` / `.groups` | 群管：禁言、踢人、名片、头衔、潜水成员、群选项 | 不实现；群选项仍由 `.bot on/off`、`.help on/off`、`.jrrp on/off`、`.me on/off`、`.draw` 的群开关写入 |
| `.admin` / `.master` / `.user` / `.authorize` / `.dismiss` | Master 认主、信任级别、拉黑、授权、退群 | 不实现；权限校验一并移除（所有群设置类指令不再校验身份） |
| `.welcome` | 入群欢迎词 | 不实现 |
| `.reply`（含 `.reply set/list/del/show`） | 自定义关键词回复（mod 驱动） | 不实现；问答类需求请用 `.ak` 或 yml 文本覆盖 |
| `.mod` / `.str` / `.helpdoc` | 模块管理、自定义消息、帮助词条编辑 | 改为 `data/dice/messages.yml`（`messages:` / `help:`）手工维护 |
| `.system` / `.cloud` | 控制台：存盘/重载/状态/更新/云黑 | 仅保留 `.system save`（强制落盘）与 `.system load`（重读 yml）；其余改为 `data/dice/config.yml`，进程管理交给 NoneBot |
| `.link` / `.send` | 窗口转发、向 Master 发消息 | 不实现 |
| `.chan` | QQ 频道独立开关 | 不实现（本部署不使用频道） |
| 黑名单 / 云黑 / 敏感词 / 刷屏监控 | `BlackListManager`、`DiceCensor`、`FrqMonitor` | 不实现 |
| WebUI | 浏览器管理面板 | 全部改为本地 yml（见下） |

### 帮助索引同步剔除

上表涉及的词条不会出现在 `.help` 里（`constants.py` 的 `UNSUPPORTED_HELP`）：
`group`/`群管`/`groups_list`/`取群列表`、`authorize`/`授权许可`/`master`/`dismiss`/`退群`/`退群指令`、
`censor`/`敏感词检测`、`reply`/`回复触发限制`/`回复列表`、`mod`、
`welcome`/`入群欢迎`/`入群欢迎词`、`link`/`消息链接`、`send`，
以及渲染结果为空的 `扩展指令`/`扩展牌堆`/`其他插件`/`姐妹骰`/`窥屏可能`/`骰娘用户群`。

因此 `.help 群管` 现在是「未找到相关词条」，`.help` 主页面也不再出现 `.dismiss` 与 `.help群管`。

---

## 9. 本地配置文件（替代 WebUI）

首次运行自动生成。**配置在插件加载时读取一次，不逐条消息扫盘**；改完执行 `.system load` 即可生效（无需重启），`.system save` 可强制落盘。

### `data/dice/config.yml`

```yaml
self_name: ""          # 骰娘自称，空则用 bot 昵称
master: 0              # Master QQ（仅用于 {print:master} 展示）
default_coc_rule: 0    # 默认 COC 房规 0-7
disabled_global: false # 全局静默
disabled_draw: false   # 全局禁用 .draw
disabled_deck: false   # 全局禁用 .deck
disabled_jrrp: false   # 全局禁用 .jrrp
disabled_me: false     # 全局禁用 .me
disabled_send: false   # 预留（.send 未复刻）
```

### `data/dice/messages.yml`

```yaml
messages:              # 覆盖内置消息文本，键同 PlainMsg
  strJrrp: "{nick}今天的人品值是: {res}"
help:                  # 覆盖/新增 .help 词条
  我的房规: "本群房规：……"
```

### `data/dice/decks.yml`

```yaml
decks:
  随机姓名: ["{随机姓氏}{随机名字}"]
  随机姓氏: [王, 李, 张]
  俄罗斯轮盘: ["有弹", "::5::无弹"]   # 1/6 概率有弹
```

### 数据目录

```
data/dice/
├── config.yml          # 全局开关（原 WebUI 配置页）
├── messages.yml        # 自定义文本/帮助（原 WebUI 自定义文本页）
├── decks.yml           # 公共牌堆（原 WebUI 牌堆页）
├── users.json          # 用户：信任级别、称呼、个人配置、jrrp 兜底
├── players.json        # 角色卡
├── chats.json          # 群记录：群名、群选项、房规
├── daily.json          # 当日 jrrp 缓存
├── session/*.json      # 游戏桌（含牌堆实例、轮盘、日志状态）
└── log/*.txt           # 跑团日志正文
```

---

## 10. 已知差异（有意为之）

1. **随机源**：原版 `mt19937(rdtsc)`，本移植用 Python `random`（Mersenne Twister）。分布一致，序列不可比；所有概率公式、取整方向、展示格式保持原样。
2. **权限改用 NoneBot 生态的字段**：原版 `canRoomHost()` = `trusted>3 || isChannel || isPrivate || isGroupAdmin || inviter==uid`。本移植改成 `NoneBot SUPERUSERS（主人） || 群主/管理员 || 私聊`，不做信任分级、不记邀请人、不主动退群/入群。因此群内开关（`.bot` / `.help` / `.jrrp` / `.me` / `.deck` / `.setcoc` / `.game new` 等）要群管理或主人才能用。
3. **昵称**：原版会实时查 QQ 昵称；本移植只用事件里的群名片/昵称与本地缓存，未知用户回落 `陌生人[QQ]`。
4. **日志重传节奏**：原版失败后 2 分钟重试，共 3 次；本移植 3 秒重试，避免长时间占用会话。
5. **`{list_extern_deck}` / `{list_reply_deck}` / `{list_dice_sister}`**：返回空（mod、自定义回复、姐妹骰未复刻）；`{list_all_deck}` 正常返回 `decks.yml` 里的牌堆名。

---

## 11. 原版缺陷修正

原版（以及照抄过来的本移植）有几处「帮助文档这么写、实际不生效」的地方，按「明显 bug 就修」的要求改掉：

| 问题 | 修正前 | 修正后 |
| --- | --- | --- |
| `.coc6` 整段是死代码（`return 0  # 群管` 后接的 `if` 被注释吞掉） | `.coc6 3` 落到 `.coc` 分支，生成 **3 张 COC7 卡** | 恢复 `if lower[n:n+4] == "coc6"`，正确走 COC6 作成 |
| `.rc 敏捷-10`（帮助文档写明的用法） | `read_num` 见前导 `-` 返回 `-3`，负修正恒不生效 | 解析时先取符号再读数，`-10` 正常参与 `value*倍率+修正` |
| `.ww 3#10a8`（已有角色卡时） | `main` 的字符合法集漏了 `#`，整段被当属性名 | 字符合法集加入 `#`，轮数正常解析 |
| `.ww 3d6` 这类非法写法 | `d` 当属性名、剩下的 `6` 并进骰数，变成 `36D10` | `d` 一并收进合法字符，按 `3D6`（3 颗、加骰参数 6）解析 |
| `.ww 3#10a8` 的多轮结果 | 三行**完全相同**：`roll()` 把 `nDiceCnt` 消费到 0，第二遍起返回 `ZeroDice_Err`，而调用方不看返回值 | `roll()` 结束时归还骰数，每轮独立掷 |
| `.ww [骰池] 原因` | 回复里 `{reason}` 原样输出（`_ww` 没写进 ctx） | 补写 `e.ctx["reason"]` |
| `.help 房规` | 内嵌 `{setcoc}` 把整条 setcoc 帮助展开几十行 | 改为「各房规判定见 .help setcoc」 |
| `.help setcoc` | 写「当前参数0-6」，但 668 版已加到 7（BRP） | 改为 0-7；末行配置项名同步为 `default_coc_rule` |
| `.help 授权许可` | 重定向写错成 `&authoize`，渲染为空 | 该词条随权限体系一并移除 |
| `.help 扩展指令` / `扩展牌堆` / `姐妹骰` | 内容只有 `{list_extern_*}`，渲染为空串 | 从帮助索引移除 |
| `.help 设定` | 展示白名单制/移出反制/云黑/姐妹骰等本移植不存在的设置 | 改为自称、Master、配置文件位置与开关说明 |
| `.help 协议` | 提到 `.dismiss`、声称会自动同意群邀请 | 去掉 `.dismiss`，邀请相关改为「邀请前须获群内同意」 |
| `.name jp/en/enzh` | 缺 `随机姓名_jp` 等牌堆，静默回落中文名 | 内置日文/英文/英文中译名牌堆 |
| `.help deck` / `.help bot` | 仍写「需要 GM 权限」「限群管理」 | 与本移植「不做权限校验」一致 |

同时把对外文案的品牌换掉：`kokona` → `tuchan`，`作者` 词条与 `.bot` 版本行改为 `langzaigg`，
原版作者以「原版 Dice! 2.7.1beta1(668)：w4123溯洄、String.Empty」一行保留（AGPL 署名要求）。
接口地址（`api.kokona.tech`、`logpainter.kokona.tech`）**不动**，那是真实服务端点。


---

## 11. 验证记录

| 脚本 | 覆盖 |
| --- | --- |
| `test/dice/probe1_smoke.py` | 指令冒烟：help/掷骰/人物作成/规则/人物卡/检定/SC/EN/先攻/游戏桌/安科/房规 |
| `test/dice/probe2_checks.py` | 深度：卡属性检定、奖惩骰、多轮、多卡、`.pc build/stat/temp/nn`、轮盘骰、`.init del`、`{case}`/帮助嵌套 |
| `test/dice/probe3_trigger.py` / `test/dice/probe4_block.py` | 触发：五种前缀、@ 自己/他人、非指令放行、`StopPropagation` 阻断、`.bot off` 静默 |
| `test/dice/probe5_log.py` | 日志：`.log new/off/on/end`，落盘内容、文件直发、S3 上传、logpainter 链接 |
| `test/dice/probe6_deck.py` | 牌堆：`.name`/`.nnn`/`.draw`（含暗抽、数量、抽空）/`.deck set range|member|new|show|reset|del|clr`/权重语法 |

另：`.jrrp`、`.rules`、日志上传均对真实接口验证通过（HTTP 200）。
