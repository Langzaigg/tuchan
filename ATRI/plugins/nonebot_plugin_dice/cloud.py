# -*- coding: utf-8 -*-
"""外部数据接口：jrrp（今日人品）与 rules（规则速查）。

对齐 DiceSchedule.cpp::DiceToday::getJrrp 与 GetRule.cpp::get。
两者都依赖溯洄的公共接口 api.kokona.tech:5555，凭据即骰娘自身 QQ 号。
"""

import random
from typing import Optional, Tuple

import httpx

from .constants import DICE_VER_WITHOUT_BUILD, get_msg_text


def _sub(text: str, **kw) -> str:
    for k, v in kw.items():
        text = text.replace("{" + k + "}", str(v))
    return text

JRRP_URL = "http://api.kokona.tech:5555/jrrp"
RULES_URL = "http://api.kokona.tech:5555/rules"
# 对应 DiceJob.cpp::log_put —— 匿名 PUT 到 dicelogger 桶，再由 logpainter 渲染
LOG_S3_URL = "https://dicelogger.s3-accelerate.amazonaws.com/%s"
LOG_PAINT_URL = "https://logpainter.kokona.tech/?s3=%s"
HEADERS = {"User-Agent": "Dice/%s" % DICE_VER_WITHOUT_BUILD,
           "Content-Type": "application/x-www-form-urlencoded"}
TIMEOUT = 8.0


async def fetch_jrrp(dice_maid: int, uid: int) -> Tuple[Optional[int], str]:
    """返回 (人品值, 错误信息)；成功时错误信息为空。"""
    data = {"QQ": str(dice_maid), "v": "20190114", "QueryQQ": str(uid)}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            resp = await client.post(JRRP_URL, content=_encode(data))
        if resp.status_code != 200:
            return None, _sub(get_msg_text("strRequestRetCodeErr"), error=resp.status_code)
        text = resp.text.strip()
        if not text:
            return None, get_msg_text("strRequestNoResponse")
        return int(text), ""
    except Exception as exc:  # 网络异常
        return None, "%s: %s" % (type(exc).__name__, exc)


async def fetch_rule(dice_maid: int, rule: str, name: str) -> Tuple[Optional[str], str]:
    """返回 (规则正文, 错误信息)。空正文视为「未找到」。"""
    data = {"Name": name, "QQ": str(dice_maid), "v": "20190114"}
    if rule:
        data["Type"] = "Rules-" + rule
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, headers=HEADERS) as client:
            resp = await client.post(RULES_URL, content=_encode(data))
        if resp.status_code != 200:
            return None, _sub(get_msg_text("strRequestRetCodeErr"), error=resp.status_code)
        text = resp.text
        if not text.strip():
            return None, get_msg_text("strRequestNoResponse")
        return text, ""
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def local_jrrp(uid: int) -> int:
    """接口不可用时的本地兜底（对齐 C++ 的 jrrp_local 行为：按当日固定）。"""
    rng = random.Random("%s-%s" % (uid, _today()))
    return rng.randint(1, 100)


def log_object_name(file_name: str) -> str:
    """对应 Base64urlEncode(log_file)：URL 安全、无 '=' 补位。"""
    import base64
    raw = base64.urlsafe_b64encode(file_name.encode("utf-8")).decode("ascii")
    return raw.rstrip("=")


async def upload_log(file_name: str, content: bytes):
    """上传日志正文，返回 (log_url, 错误信息)。"""
    key = log_object_name(file_name)
    try:
        async with httpx.AsyncClient(timeout=30.0,
                                     headers={"User-Agent": HEADERS["User-Agent"],
                                              "Content-Type": "text/plain; charset=utf-8"}) as client:
            resp = await client.put(LOG_S3_URL % key, content=content)
        if resp.status_code not in (200, 201, 204):
            return None, "HTTP %d" % resp.status_code
        return LOG_PAINT_URL % key, ""
    except Exception as exc:
        return None, "%s: %s" % (type(exc).__name__, exc)


def _today() -> str:
    import time
    return time.strftime("%Y-%m-%d", time.localtime())


def _encode(data: dict) -> bytes:
    from urllib.parse import quote_plus
    return "&".join("%s=%s" % (k, quote_plus(str(v))) for k, v in data.items()).encode("utf-8")
