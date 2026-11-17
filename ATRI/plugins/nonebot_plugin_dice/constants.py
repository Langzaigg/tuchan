# -*- coding: utf-8 -*-
"""Dice! 复刻版常量：消息文本、帮助文档、技能表、版本号。"""

from ._msgdata import PLAIN_MSG, HELP_DOC
from ._tables import (
    BASIC_COC7,
    EXPRESSION_COC7,
    LONG_INSANITY,
    SKILL_DEFAULT_VAL,
    SKILL_NAME_REPLACE,
    STR_FEAR,
    STR_PANIC,
    TEMP_INSANITY,
    VARIABLE_COC7,
)

__all__ = [
    "PLAIN_MSG",
    "HELP_DOC",
    "GLOBAL_MSG",
    "GLOBAL_HELP",
    "get_msg_text",
    "get_help_text",
    "BASIC_COC7",
    "EXPRESSION_COC7",
    "LONG_INSANITY",
    "SKILL_DEFAULT_VAL",
    "SKILL_NAME_REPLACE",
    "STR_FEAR",
    "STR_PANIC",
    "TEMP_INSANITY",
    "VARIABLE_COC7",
    "DICE_VER",
    "DICE_SHORT_VER",
    "DICE_FULL_VER",
    "CHAT_CONF",
    "PLAYER_ERRORS",
]

# 与 GlobalVar.h 保持一致
DICE_BUILD = 668
DICE_VER_WITHOUT_BUILD = "2.7.1beta1"
DICE_VER = "%s(%d)" % (DICE_VER_WITHOUT_BUILD, DICE_BUILD)
DICE_SHORT_VER = "Dice! by langzaigg Ver " + DICE_VER


def _ci(d):
    """dict_ci：大小写不敏感，后出现的同键值覆盖先出现的值。"""
    out = {}
    for k, v in d.items():
        out[(k or "").lower()] = v
    return out


GLOBAL_MSG = _ci(PLAIN_MSG)
GLOBAL_HELP = _ci(HELP_DOC)

# ---------------------------------------------------------------------------
# 帮助索引与文案的本地化
#
# `_msgdata.py` 是从 `GlobalVar.cpp` 原样提取的产物（文件头写明勿手工改），
# 所以删词条、换文案一律在这里做：
#   UNSUPPORTED_HELP —— 本移植版没有复刻的指令，留在索引里会让用户查到
#                       「有这个指令」却毫无响应，直接剔除；
#   HELP_OVERRIDE    —— 与实现不符、原版写错、需要换品牌的词条整条覆盖；
#   MSG_OVERRIDE     —— 同样的处理，针对 PLAIN_MSG（普通回复文案）。
# `settings` 的基线快照取自这两份 dict，所以 `.system load` 不会把剔除的词条复活。
# ---------------------------------------------------------------------------
UNSUPPORTED_HELP = (
    # 群管 / 取群列表
    "group",
    "群管",
    "groups_list",
    "取群列表",
    # 权限：认主、授权、退群
    "authorize",
    "授权许可",
    "master",
    "dismiss",
    "退群",
    "退群指令",
    # 云黑与敏感词
    "censor",
    "敏感词检测",
    # 自定义回复（依赖 mod）
    "reply",
    "回复触发限制",
    "回复列表",
    # mod / 入群欢迎 / 窗口转发 / 后台发送
    "mod",
    "welcome",
    "入群欢迎",
    "入群欢迎词",
    "link",
    "消息链接",
    "send",
    # 内容完全依赖 mod 与许可证系统的词条（渲染出来是空串）
    "扩展指令",
    "扩展牌堆",
    "其他插件",
    "姐妹骰",
    "窥屏可能",
    "骰娘用户群",
)

HELP_OVERRIDE = {
    # ---------------------------------------------------------------- 指令一览
    "指令": (
        "指令前接at可以指定骰娘响应，如\n"
        "{at:self}.bot on\n"
        "请.help对应指令 获取详细信息，如.help r\n"
        "控制指令:\n"
        ".bot 版本信息\n"
        ".bot on/off 启用/停用指令\n"
        ".game 游戏领域\n"
        ".rule 规则设置/速查\n"
        ".r 掷骰\n"
        ".log 日志记录\n"
        ".ob 旁观模式\n"
        ".set 设置默认骰\n"
        ".coc COC7人物作成\n"
        ".coc6 COC6人物作成\n"
        ".dnd DND人物作成\n"
        ".st 属性记录\n"
        ".pc 角色卡记录\n"
        ".rc 检定\n"
        ".setcoc 设置rc房规\n"
        ".sc 理智检定\n"
        ".en 成长/增强检定\n"
        ".ri 先攻\n"
        ".init 先攻列表\n"
        ".ww 骰池其他指令\n"
        ".nn 设置称呼\n"
        ".draw 抽牌\n"
        ".deck 牌堆实例\n"
        ".name 随机姓名\n"
        ".ak 安科/安价\n"
        ".jrrp 今日人品\n"
        ".system load/save 重载配置/强制存盘\n"
        "为了避免未预料到的指令误判，请尽可能在参数之间使用空格"
    ),
    # ---------------------------------------------------------------- 服务协议
    "协议": (
        "0.本协议是Dice!默认服务协议。如果你看到了这句话，意味着Master应用默认协议，请注意。\n"
        "1.邀请骰娘、使用掷骰服务和在群内阅读此协议视为同意并承诺遵守此协议，"
        "否则请将骰娘移出群聊。\n"
        "2.不允许禁言、移出骰娘或刷屏掷骰等对骰娘的不友善行为，这些行为将会提高骰娘被制裁的风险。"
        "开关骰娘响应请使用.bot on/off。\n"
        "3.邀请骰娘前请确认已获群内同意并认可本协议。因擅自邀请而使骰娘遭遇不友善行为时，"
        "邀请者应承担相应责任。\n"
        "4.禁止将骰娘用于赌博及其他违法犯罪行为。\n"
        "5.对于设置敏感昵称等无法预见但有可能招致言论审查的行为，骰娘可能会出于自我保护而拒绝提供服务\n"
        "6.由于技术以及资金原因，我们无法保证机器人100%的时间稳定运行，可能不定时停机维护或遭遇冻结，"
        "但是相应情况会及时通过各种渠道进行通知，敬请谅解。临时停机的骰娘不会有任何响应，"
        "故而不会影响群内活动，此状态下仍然禁止不友善行为。\n"
        "7.对于违反协议的行为，骰娘将视情况终止对用户和所在群提供服务。\n"
        "8.本协议内容随时有可能改动。请注意帮助信息等处的骰娘动态。\n"
        "9.骰娘提供掷骰服务是完全免费的，欢迎投食。\n"
        "10.本服务最终解释权归服务提供方所有。"
    ),
    # ---------------------------------------------------------------- 骰娘设定
    "设定": (
        "{self}的设定：\n"
        "自称：{self}\n"
        "Master：{print:master}\n"
        "配置文件：data/dice/config.yml（自称/默认房规/各功能开关）\n"
        "改完配置执行 .system load 生效，.system save 强制存盘\n"
        "权限：NoneBot 主人（SUPERUSERS）全域放行，群内开关限群主/管理员\n"
        "本移植版不含信任分级、群管指令、云黑、自定义回复、mod、入群欢迎"
    ),
    # ---------------------------------------------------------------- 链接与作者
    "链接": (
        "Dice!论坛: https://tuchan.tech\n"
        "Dice!手册: https://v2docs.tuchan.tech\n"
        "NoneBot 移植版: https://github.com/Langzaigg/nonebot-plugin-dice"
    ),
    "作者": (
        "Dice! 移植版：langzaigg\n"
        "https://github.com/Langzaigg/nonebot-plugin-dice\n"
        "原版 Dice! 2.7.1beta1(668)：w4123溯洄、String.Empty"
    ),
    # ---------------------------------------------------------------- 需要纠错的指令
    # 原版把整条 .setcoc 帮助嵌进来，会展开成几十行
    "房规": "Dice!目前只为COC7检定提供房规，各房规的判定方式见.help setcoc",
    # 原版写 0-6，实际 668 版已加入 7（BRP）
    "setcoc": (
        "为当前群或讨论组设置COC房规，如.setcoc 1,当前参数0-7\n"
        "`.setcoc show`查看当前房规\n"
        "`.setcoc clr`清除当前房规\n"
        "0 规则书\n"
        "出1大成功\n"
        "不满50出96 - 100大失败，满50出100大失败\n"
        "1\n"
        "不满50出1大成功，满50出1 - 5大成功\n"
        "不满50出96 - 100大失败，满50出100大失败\n"
        "2\n"
        "出1 - 5且 <= 成功率大成功\n"
        "出100或出96 - 99且 > 成功率大失败\n"
        "3\n"
        "出1 - 5大成功\n"
        "出96 - 100大失败\n"
        "4\n"
        "出1 - 5且 <= 十分之一大成功\n"
        "不满50出 >= 96 + 十分之一大失败，满50出100大失败\n"
        "5\n"
        "出1 - 2且 < 五分之一大成功\n"
        "不满50出96 - 100大失败，满50出99 - 100大失败\n"
        "6 绿色三角洲\n"
        "出1或出个位十位相同且<=成功率大成功\n"
        "出100或出个位十位相同且>成功率大失败\n"
        "7 BRP\n"
        "简单难度双倍成功率，五分之一特殊成功率\n"
        "出96-100必定失败且视成功率判大失败\n"
        "\n"
        "如需其他房规可向开发者反馈\n"
        "无论如何，群内检定只会调用群内设置，以免团内成员不对等\n"
        "未设置房规时使用 data/dice/config.yml 的 default_coc_rule（初始为0）"
    ),
    # 本移植版不做信任分级；群内开关交给群管理或主人
    "bot": (
        ".bot on/off 开启/静默骰子指令（限群管理或主人）\n"
        ".bot及回复词无视静默状态，只要插件开启总是有效"
    ),
    "deck": (
        "牌堆实例.deck\n"
        "`.deck set (牌堆名=)公共牌堆名` //从公共牌堆创建实例\n"
        "`.deck set (牌堆名=)member` //从群成员列表创建实例\n"
        "`.deck set (牌堆名=)range 下限 上限` //创建等差数列作为实例\n"
        "`.deck show` //列出所有牌堆实例\n"
        "`.deck show 牌堆名` //查看牌堆剩余卡牌\n"
        "`.deck reset 牌堆名` //重置剩余卡牌\n"
        "`.deck clr` //清空所有实例\n"
        "`.deck new 牌堆名=[卡面1](...|[卡面n])` //自定义牌堆\n"
        "例:\n"
        ".deck new 俄罗斯轮盘=有弹|::5::无弹 //::张数::卡面\n"
        "*实例抽牌不会放回直到抽空，可以操作抽牌洗牌*\n"
        "*每个群至多保存10个实例*\n"
        "*.draw时，牌堆实例优先级高于同名公共对象*\n"
        "*除show外其他群内操作需要群管理或主人*"
    ),
    # 扩展牌堆词条已移除，去掉相应指引
    "draw": (
        "抽牌：.draw [牌堆名称] ([抽牌数量])\t\n"
        ".draw _[牌堆名称] ([抽牌数量])\t//暗抽，结果私聊发送\n"
        ".drawh [牌堆名称] ([抽牌数量])\t//暗抽，参数h后必须留空格\n"
        ".draw _狼人杀\t//抽牌结果通过私聊获取，可ob\n"
        "*牌堆名称优先调用牌堆实例，如未设置则从同名公共牌堆生成临时实例*\n"
        "*抽到的牌不放回，牌堆抽空后无法继续*\n"
        "*查看{self}已安装牌堆，可.help 全牌堆列表"
    ),
    # .send 已移除，日志文件改为本地自取
    "log": (
        "跑团日志记录.log\n"
        "`.log new 日志名` 新开日志并开始记录\n"
        "`.log on` 继续记录\n"
        "`.log off` 暂停记录\n"
        "`.log end` 完成记录并发送日志文件\n"
        "本桌有登记gm或pl时（见.game）记录gm和pl的发言，否则记录所有人发言\n"
        "日志名须作为文件名合法，省略则使用创建时间戳。"
        "发送失败时可在 data/dice/log/ 目录下自取"
    ),
}

MSG_OVERRIDE = {
    # .help 无参数时的主帮助页：去掉 .dismiss 与 .help群管 两项未复刻入口
    "strHlpMsg": (
        ".bot on/off ID（或at或后四位） //开启或关闭指令\n"
        ".help协议 确认服务协议\n"
        ".help指令 查看指令列表\n"
        ".help设定 确认骰娘设定\n"
        ".help链接 查看源码文档\n"
        "官方论坛: https://forum.tuchan.tech/"
    ),
    # 原版这句是给白名单/信任体系用的，本移植不设信任分级
    "strWhiteQQDenied": "该操作仅限群管理或{self}的主人×",
}

for _key in UNSUPPORTED_HELP:
    GLOBAL_HELP.pop(_key.lower(), None)
GLOBAL_HELP.update({k.lower(): v for k, v in HELP_OVERRIDE.items()})
GLOBAL_MSG.update({k.lower(): v for k, v in MSG_OVERRIDE.items()})
del _key

# 群管词条：词条名 -> 所需信任级别（DiceConsole mChatConf）
CHAT_CONF = {
    "停用指令": 1,
    "禁用回复": 1,
    "禁用jrrp": 1,
    "禁用draw": 1,
    "禁用me": 1,
    "禁用help": 1,
    "许可使用": 3,
    "免清": 3,
    "免黑": 4,
    "忽略": 5,
    "协议无效": 5,
}

# PlayerErrors 的取值在 CharacterCard.cpp 中大小写不一致，按其字面量对照到 _msgdata 中的键
PLAYER_ERRORS = {
    -1: "strPcCardFull",
    -3: "strPCNameEmpty",
    -4: "strPCNameExist",
    -5: "strPcNameNotExist",
    -6: "strPCNameInvalid",
    -7: "strPcInitDelErr",
    -21: "strPcLockedKill",
    -22: "strPcLockedName",
    -23: "strPcLockedWrite",
    -24: "strPcLockedRead",
}


def get_msg_text(key: str) -> str:
    return GLOBAL_MSG.get((key or "").lower(), "")


def get_help_text(key: str) -> str:
    return GLOBAL_HELP.get((key or "").lower(), "")
