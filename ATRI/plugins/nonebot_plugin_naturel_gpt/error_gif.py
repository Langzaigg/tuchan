"""报错时的表情包兜底。

群里不发送 "[系统] xxx" 这类技术性错误文案，改从 ``<NG_DATA_PATH>/gif`` 随机挑一张
gif 发出去。gif 目录可随时增删图片，无需改配置；目录缺失/为空时静默跳过（只写日志），
绝不因为兜底失败再抛异常打断主流程。
"""
import base64
import random
from pathlib import Path
from typing import Any, Awaitable, Callable, List, Optional

from .config import config
from .logger import logger

GIF_SUBDIR = "gif"

# 发送失败时依次尝试的形态：本地路径（协议端自己读文件）→ base64（跨机器/协议端不支持本地路径）
_SEND_MODES = ("uri", "base64")


def get_error_gif_dir() -> Path:
    return Path(config.NG_DATA_PATH) / GIF_SUBDIR


def list_error_gifs() -> List[Path]:
    """列出可用的 gif（按名称排序，保证随机池稳定可复现）。"""
    gif_dir = get_error_gif_dir()
    try:
        return sorted(
            p for p in gif_dir.iterdir()
            if p.is_file() and p.suffix.lower() == ".gif"
        )
    except OSError as e:
        logger.warning(f"报错兜底：读取 gif 目录失败 {gif_dir}: {e!r}")
        return []


def pick_error_gif() -> Optional[Path]:
    gifs = list_error_gifs()
    if not gifs:
        return None
    return random.choice(gifs)


def _build_gif_payload(path: Path, mode: str) -> str:
    if mode == "uri":
        return path.resolve().as_uri()
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"base64://{data}"


async def send_error_gif(send: Callable[[Any], Awaitable[Any]]) -> bool:
    """随机发一张 gif 代替系统错误提示。

    ``send`` 为发送协程（如 ``matcher.send``）。返回是否发送成功；失败只写日志，
    由调用方决定是否再兜底（默认不再发任何文字，避免把错误信息重新泄进群里）。
    """
    gif = pick_error_gif()
    if gif is None:
        logger.warning(f"报错兜底：gif 目录为空或不存在，跳过表情发送: {get_error_gif_dir()}")
        return False

    from nonebot.adapters.onebot.v11 import MessageSegment

    for mode in _SEND_MODES:
        try:
            payload = _build_gif_payload(gif, mode)
            await send(MessageSegment.image(file=payload))
            logger.info(f"报错兜底：已发送 gif 表情 {gif.name}（{mode}）")
            return True
        except Exception as e:
            logger.warning(f"报错兜底：gif 发送失败 {gif.name}（{mode}）: {e!r}")
    return False
