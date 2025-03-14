from time import sleep

import nonebot
from nonebot.adapters.onebot.v11 import Adapter
from .config import RUNTIME_CONFIG, InlineGoCQHTTP

__version__ = "YHN-001-A05.fix1"


def asgi():
    return nonebot.get_asgi()


def driver():
    return nonebot.get_driver()


def init():
    nonebot.init(**RUNTIME_CONFIG)
    driver().register_adapter(Adapter)
    nonebot.load_plugins("ATRI/plugins")
    nonebot.load_plugin("nonebot_plugin_anime_trace")
    nonebot.load_plugin("nonebot_plugin_imagesearch")
    nonebot.load_plugin('hikari_bot')
    if InlineGoCQHTTP.enabled:
        nonebot.load_plugin("nonebot_plugin_gocqhttp")
        nonebot.load_plugin("nonebot_plugin_test")
    sleep(3)


def run():
    nonebot.run()
