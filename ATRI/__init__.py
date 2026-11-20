import os
from pathlib import Path
from pkgutil import iter_modules
from time import sleep

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from nonebot.drivers import HTTPServerSetup
from .config import (
    DEFAULT_ONEBOT_ENDPOINT,
    BotSelfConfig,
    InlineGoCQHTTP,
    RUNTIME_CONFIG,
    endpoint_name,
)
from .naturel_gpt_storage import import_draw_history, prepare_config

__version__ = "YHN-001-A05.fix1"


def asgi():
    return nonebot.get_asgi()


def driver():
    return nonebot.get_driver()


class ReverseWebSocketAdapter(Adapter):
    def setup_http_server(self, setup: HTTPServerSetup) -> None:
        # Named endpoints receive events exclusively over the authenticated WS route.
        pass


def _register_qq_adapter() -> None:
    from .qq_official import OfficialQQAdapter, QQOneBotDispatcher, superuser_id

    secret = os.environ.get(BotSelfConfig.qq_app_secret_env) or BotSelfConfig.qq_app_secret
    if not BotSelfConfig.qq_app_id or not secret:
        raise ValueError(f"接入点 {endpoint_name} 需要 qq_app_id 和 QQ AppSecret")
    BotSelfConfig.superusers = {superuser_id(user_id) for user_id in BotSelfConfig.superusers}
    driver().config.superusers = BotSelfConfig.superusers
    driver().config.qq_bots = [{
        "id": BotSelfConfig.qq_app_id,
        "secret": secret,
        "use_websocket": BotSelfConfig.qq_use_websocket,
        "intent": {
            "guilds": False,
            "guild_members": False,
            "guild_message_reactions": False,
            "at_messages": False,
            "message_audit": False,
            "c2c_group_at_messages": True,
            "group_members": BotSelfConfig.qq_group_members,
        },
    }]
    driver().register_adapter(QQOneBotDispatcher, app_id=BotSelfConfig.qq_app_id)
    driver().register_adapter(OfficialQQAdapter)


def init():
    if not BotSelfConfig.enabled:
        raise RuntimeError(f"接入点 {endpoint_name} 已关闭")

    runtime_config = dict(RUNTIME_CONFIG)
    if BotSelfConfig.adapter == "qq":
        runtime_config["driver"] = "~httpx+~websockets"
        if BotSelfConfig.has_listener:
            runtime_config["driver"] = "~fastapi+~httpx+~websockets"
    nonebot.init(**runtime_config)

    default_onebot = endpoint_name == DEFAULT_ONEBOT_ENDPOINT
    if BotSelfConfig.adapter == "qq":
        _register_qq_adapter()
    else:
        if not default_onebot or BotSelfConfig.onebot_access_token is not None:
            driver().config.onebot_v11_access_token = BotSelfConfig.onebot_access_token or None
        if default_onebot:
            driver().register_adapter(Adapter)
        else:
            driver().config.onebot_v11_ws_urls = set()
            driver().config.onebot_v11_api_roots = {}
            driver().register_adapter(ReverseWebSocketAdapter)
    shared_draw_db = prepare_config(driver().config)
    endpoints_dir = Path("data/endpoints").resolve()
    if not default_onebot:
        # Existing plugins use paths relative to their process for local caches and JSON state.
        endpoint_runtime = endpoints_dir / endpoint_name / "runtime"
        endpoint_runtime.mkdir(parents=True, exist_ok=True)
        os.chdir(endpoint_runtime)
    plugins_dir = Path(__file__).resolve().parent / "plugins"
    names = {f"ATRI.plugins.{item.name}" for item in iter_modules([str(plugins_dir)])}
    plugins = nonebot.load_all_plugins(names, [])
    if not any(plugin.name == "nonebot_plugin_naturel_gpt" for plugin in plugins):
        raise RuntimeError(f"接入点 {endpoint_name} 无法加载 Naturel GPT")
    import_draw_history(shared_draw_db, Path(driver().config.ng_archive_fallback_dir) / "draw.db", endpoints_dir)
    if BotSelfConfig.hikari_enabled:
        nonebot.load_plugin("hikari_bot")
    if default_onebot and InlineGoCQHTTP.enabled:
        nonebot.load_plugin("nonebot_plugin_gocqhttp")
        nonebot.load_plugin("nonebot_plugin_test")
    sleep(3)


def run():
    nonebot.run()
