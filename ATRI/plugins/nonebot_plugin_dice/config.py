# -*- coding: utf-8 -*-
"""Dice! for NoneBot2 —— 配置项。"""

from typing import List

from pydantic import BaseModel


class Config(BaseModel):
    # 骰娘自称，留空则取 bot 昵称
    dice_self_name: str = ""
    # 骰娘 QQ，用于 jrrp / rules 接口的 QQ 参数，留空则取事件里的 self_id
    dice_maid_qq: int = 0
    # 触发前缀（与原插件一致，默认 . ! 。 ！ ．）
    dice_prefix: List[str] = [".", "!", "。", "！", "．"]
    # 是否响应 @ 骰娘 的消息（默认开启）
    dice_listen_at: bool = True
