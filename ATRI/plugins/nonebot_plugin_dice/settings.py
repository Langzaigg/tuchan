# -*- coding: utf-8 -*-
"""本地 yml 配置（替代原版 WebUI 的管理面板）。

data/dice/config.yml   —— 全局开关与运行参数
data/dice/messages.yml —— 自定义消息文本 / 帮助词条（原 WebUI 的「自定义文本」）
"""

from typing import Any, Dict

import yaml

from . import constants
from .store import store

DEFAULT_CONFIG: Dict[str, Any] = {
    "self_name": "",
    "master": 0,
    "default_coc_rule": 0,
    "disabled_global": False,
    "disabled_draw": False,
    "disabled_deck": False,
    "disabled_jrrp": False,
    "disabled_me": False,
    "disabled_send": False,
}

CONFIG_HEADER = """# Dice! 本地配置（替代原 WebUI 管理面板）
# 本文件在启动时读取一次；改完生效需执行 .system load（无需重启）。
#
# self_name          骰娘自称，留空则用 bot 昵称
# master             Master 的 QQ（仅用于文案展示；0 则取 NoneBot 的 SUPERUSERS）
# default_coc_rule   默认 COC 房规 0-7
# disabled_global    全局静默（仅 .bot 仍可查看版本；关闭需改本文件）
# disabled_draw      全局禁用 .draw
# disabled_deck      全局禁用 .deck
# disabled_jrrp      全局禁用 .jrrp
# disabled_me        全局禁用 .me
# disabled_send      全局禁用 .send
"""

MESSAGES_HEADER = """# 自定义消息文本 / 帮助词条（覆盖内置文本）
# messages 下的键对应内置消息键，如 strJrrp；help 下的键对应 .help 词条名。
# 例：
# messages:
#   strJrrp: "{nick}今天的人品值是: {res}（本群特供版）"
# help:
#   我的规则: "本群房规：……"
"""


class Settings:
    def __init__(self):
        self.dir = store.dir
        self.config: Dict[str, Any] = dict(DEFAULT_CONFIG)
        self._msg_base = dict(constants.GLOBAL_MSG)
        self._help_base = dict(constants.GLOBAL_HELP)

    # ------------------------------------------------------------------ 文件
    @property
    def config_path(self):
        return self.dir / "config.yml"

    @property
    def messages_path(self):
        return self.dir / "messages.yml"

    def _ensure(self, path, header: str, payload):
        if path.exists():
            return
        try:
            with path.open("w", encoding="utf-8") as f:
                f.write(header)
                yaml.safe_dump(payload, f, allow_unicode=True, sort_keys=False)
        except OSError:
            pass

    def load(self):
        """启动时加载一次；之后只有 .system load 会重新读取。

        刻意不在消息热路径上做 mtime 探测：每来一条消息 stat 两个文件虽不算重，
        但在高活跃群里属于无意义的系统调用。
        """
        self._load_config()
        self._load_messages()

    def _load_config(self):
        path = self.config_path
        self._ensure(path, CONFIG_HEADER, DEFAULT_CONFIG)
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return
        cfg = dict(DEFAULT_CONFIG)
        if isinstance(data, dict):
            for key in DEFAULT_CONFIG:
                if key in data:
                    cfg[key] = data[key]
        self.config = cfg

    def _load_messages(self):
        path = self.messages_path
        self._ensure(path, MESSAGES_HEADER, {"messages": {}, "help": {}})
        try:
            with path.open("r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
        except Exception:
            return
        msgs = data.get("messages") if isinstance(data, dict) else None
        helps = data.get("help") if isinstance(data, dict) else None
        constants.GLOBAL_MSG.clear()
        constants.GLOBAL_MSG.update(self._msg_base)
        if isinstance(msgs, dict):
            for key, val in msgs.items():
                if val is not None:
                    constants.GLOBAL_MSG[str(key).lower()] = str(val)
        constants.GLOBAL_HELP.clear()
        constants.GLOBAL_HELP.update(self._help_base)
        if isinstance(helps, dict):
            for key, val in helps.items():
                if val is not None:
                    constants.GLOBAL_HELP[str(key).lower()] = str(val)

    # ------------------------------------------------------------------ 取值
    def __getattr__(self, item: str):
        cfg = self.__dict__.get("config") or {}
        if item in cfg:
            return cfg[item]
        raise AttributeError(item)

    @property
    def self_name(self) -> str:
        return str(self.config.get("self_name") or "")

    @property
    def master(self) -> int:
        try:
            return int(self.config.get("master") or 0)
        except (TypeError, ValueError):
            return 0

    @property
    def default_coc_rule(self) -> int:
        try:
            return int(self.config.get("default_coc_rule") or 0)
        except (TypeError, ValueError):
            return 0


settings = Settings()
settings.load()
