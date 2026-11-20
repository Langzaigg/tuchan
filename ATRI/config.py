import os
import re
import yaml
from pathlib import Path
from datetime import timedelta
from ipaddress import IPv4Address


def load_yml(file: Path, encoding="utf-8") -> dict:
    with open(file, "r", encoding=encoding) as f:
        data = yaml.safe_load(f)
    return data


CONFIG_PATH = Path(".") / "config.yml"
config = load_yml(CONFIG_PATH)


DEFAULT_ONEBOT_ENDPOINT = "onebot"
LEGACY_ENDPOINT_NAME = "primary"
ENDPOINT_ENV = "ATRI_ENDPOINT"


class EndpointConfig:
    def __init__(self, settings: dict, *, default_onebot: bool = False):
        self.adapter = settings.get("adapter", "onebot")
        if self.adapter not in ("onebot", "qq") or (default_onebot and self.adapter != "onebot"):
            raise ValueError("BotEndpoints.onebot 使用 onebot，其他接入点支持 onebot 或 qq")
        self.enabled = settings.get("enabled", default_onebot)
        self.qq_use_websocket = settings.get("qq_use_websocket", True)
        self.has_listener = self.adapter == "onebot" or not self.qq_use_websocket
        if self.has_listener:
            self.host = IPv4Address(settings.get("host", "127.0.0.1"))
            self.port = int(settings.get("port", 8080))
        self.debug = bool(settings.get("debug", False))
        self.superusers = {str(user_id) for user_id in settings.get("superusers", [])}
        names = settings.get("nickname") or ()
        if isinstance(names, str):
            names = (names,)
        self.nicknames = tuple(str(name).strip() for name in names if name and str(name).strip())
        self.nickname = set(self.nicknames or (() if "nickname" in settings else ("ATRI", "Atri", "atri", "亚托莉", "アトリ")))
        self.command_start = set(settings.get("command_start", [""]))
        self.command_sep = set(settings.get("command_sep", ["."]))
        self.session_expire_timeout = timedelta(seconds=settings.get("session_expire_timeout", 60))
        self.proxy = settings.get("proxy")
        self.notify_superusers = settings.get("notify_superusers", default_onebot)
        self.onebot_access_token = settings.get("onebot_access_token")
        self.qq_app_id = str(settings.get("qq_app_id") or "")
        self.qq_app_secret = settings.get("qq_app_secret") or ""
        self.qq_app_secret_env = settings.get("qq_app_secret_env") or ""
        self.qq_group_members = settings.get("qq_group_members", False)
        self.hikari_enabled = settings.get("hikari_enabled", default_onebot or self.adapter == "qq")


endpoint_settings = dict(config.get("BotEndpoints") or {})
if "BotSelfConfig" in config:
    endpoint_settings.setdefault(DEFAULT_ONEBOT_ENDPOINT, config["BotSelfConfig"])

endpoint_configs = {}
for name, settings in endpoint_settings.items():
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", name) or name == LEGACY_ENDPOINT_NAME:
        raise ValueError(f"无效的 BotEndpoints 名称：{name!r}")
    endpoint_configs[name] = EndpointConfig(settings or {}, default_onebot=name == DEFAULT_ONEBOT_ENDPOINT)

if not endpoint_configs:
    raise ValueError("请在 BotEndpoints 中配置至少一个接入点")
default_endpoint = DEFAULT_ONEBOT_ENDPOINT if DEFAULT_ONEBOT_ENDPOINT in endpoint_configs else next(iter(endpoint_configs))
if not endpoint_configs[default_endpoint].enabled:
    default_endpoint = next((name for name, settings in endpoint_configs.items() if settings.enabled), default_endpoint)
endpoint_name = os.environ.get(ENDPOINT_ENV, default_endpoint)
if endpoint_name == LEGACY_ENDPOINT_NAME:
    endpoint_name = DEFAULT_ONEBOT_ENDPOINT
if endpoint_name not in endpoint_configs:
    raise ValueError(f"未配置接入点：{endpoint_name}")
BotSelfConfig = endpoint_configs[endpoint_name]


class InlineGoCQHTTP:
    config: dict = config["InlineGoCQHTTP"]

    enabled: bool = bool(config.get("enabled", True))
    accounts: list = config.get("accounts", [])
    download_domain: str = config.get("download_domain", "download.fastgit.org")
    download_version: str = str(config.get("download_version", "v1.0.0-rc1"))


class SauceNAO:
    config: dict = config["SauceNAO"]

    key: str = config.get("key", "")


class KaLiveConfig:
    config: dict = config["KaLive"]


class Setu:
    config: dict = config["Setu"]

    reverse_proxy: bool = bool(config.get("reverse_proxy", False))
    reverse_proxy_domain: str = config.get("reverse_proxy_domain", str())


class Repeater:
    config: dict = config.get("Repeater", {})

    # 复读生效群（"all" 表示全部群）
    group: list = config.get("group", [])
    # 复读触发概率生效群
    whitelist: list = config.get("whitelist", [])
    # 撤回还原功能生效群白名单，默认空，需在 config.yml 配置
    recall_whitelist: list = config.get("recall_whitelist", [])


RUNTIME_CONFIG = {
    "debug": BotSelfConfig.debug,
    "superusers": BotSelfConfig.superusers,
    "nickname": BotSelfConfig.nickname,
    "command_start": BotSelfConfig.command_start,
    "command_sep": BotSelfConfig.command_sep,
    "session_expire_timeout": BotSelfConfig.session_expire_timeout,
    "ng_endpoint_nicknames": BotSelfConfig.nicknames,
    "gocq_accounts": InlineGoCQHTTP.accounts,
    "gocq_download_domain": InlineGoCQHTTP.download_domain,
    "gocq_version": InlineGoCQHTTP.download_version,
    "repeater_group": Repeater.group,
    "repeater_whitelist": Repeater.whitelist,
    "repeater_recall_whitelist": Repeater.recall_whitelist,
}
if BotSelfConfig.has_listener:
    RUNTIME_CONFIG.update(host=BotSelfConfig.host, port=BotSelfConfig.port)
