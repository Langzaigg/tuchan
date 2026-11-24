import asyncio
import sys
import traceback
from collections import Counter
from typing import List, Tuple

import httpx
import nonebot
from nonebot.adapters.onebot.v11 import MessageSegment
from nonebot.log import logger

from .bili_client import bili_client
from .bili_task import bili_task_manager
from .exception import (
    BiliAPIRetCodeError,
    BiliConnectionError,
    BiliDatebaseError,
    BiliNoLiveRoom,
    BiliStatusCodeError,
)

__PLUGIN_NAME = "[bilibilibot~直播]"


async def check_bili_live() -> None:
    """检查关注的直播间，更新已确认的状态并通知订阅者。"""
    liver_list = list(bili_task_manager.liver_list.values())
    if not liver_list:
        return

    sched_bot = nonebot.get_bot()
    async with httpx.AsyncClient(headers=bili_client.headers) as client:
        results = await asyncio.gather(
            *(
                bili_client.get_live_status(client, liver["liver_uid"], liver["room_id"])
                for liver in liver_list
            ),
            return_exceptions=True,
        )

    connection_errors = Counter(result.reason for result in results if isinstance(result, BiliConnectionError))
    if connection_errors:
        details = "，".join(f"{reason} ×{count}" for reason, count in connection_errors.items())
        logger.error(
            f"{__PLUGIN_NAME} {sum(connection_errors.values())}/{len(liver_list)} 个直播间请求失败，"
            f"保留这些直播间的原状态，下轮再查（{details}）"
        )

    for liver, result in zip(liver_list, results):
        if isinstance(result, BiliConnectionError):
            continue
        uid, name, room_id = liver["liver_uid"], liver["liver_name"], liver["room_id"]
        if isinstance(result, BaseException):
            logger.error(
                f"{__PLUGIN_NAME} 检测主播 <{name}> 开播情况失败（{result.__class__.__name__}: {result}）"
            )
            continue
        is_live, title, cover_url = result
        if bili_task_manager.liver_list.get(uid) is not liver or is_live == liver["is_live"]:
            continue

        if is_live:
            logger.info(f"{__PLUGIN_NAME} 检测到主播 <{name}> 已开播！")
            message = f"【直播动态】\n<{name}>正在直播!\n标题: {title}\n链接: https://live.bilibili.com/{room_id}"
            message += MessageSegment.image(cover_url)
        else:
            logger.info(f"{__PLUGIN_NAME} 检测到主播 <{name}> 已下播")
            message = f"【直播动态】\n<{name}>直播结束!"
        bili_task_manager.update_liver_info(uid, is_live)
        await asyncio.gather(*(
            sched_bot.send_msg(message=message, user_id=user_id) for user_id in liver["user_follower"]
        ))
        await asyncio.gather(*(
            sched_bot.send_msg(message=message, group_id=group_id) for group_id in liver["group_follower"]
        ))


async def follow_liver(uid: str, user_id: str, user_type: int) -> Tuple[bool, str]:
    '''根据用户/群关注主播，修改数据库

    Args:
        uid (str): 主播的uid
        user_id (str): 用户的uid或群号
        user_type (int): 0-用户，1-群

    Returns:
        Tuple[bool, str]: [是否成功，主播名(uid) | 主播uid(失败原因)]
    '''
    
    if not uid.isdigit():
        logger.error(f'{__PLUGIN_NAME}存在错误参数 <{uid}>')
        return (False, uid + "(错误参数)")
    uid = str(int(uid))
    try:
        if uid not in bili_task_manager.liver_list:
            liver_name, room_id = await bili_client.init_liver_info(uid)
            
            bili_task_manager.add_liver_info(uid, liver_name, False, room_id)
            if user_type == 0:
                bili_task_manager.add_user_follower(1, uid, user_id)
            else:
                bili_task_manager.add_group_follower(1, uid, user_id)
            #bili_database.insert_relation(2 + user_type, uid, user_id)

            logger.info(f"{__PLUGIN_NAME}用户/群 <{user_id}> 关注主播 <{liver_name}> 成功")
            return (True, liver_name + f"(uid: {uid})")
        
        if user_type == 0 and user_id in bili_task_manager.liver_list[uid]["user_follower"] or \
            user_type == 1 and user_id in bili_task_manager.liver_list[uid]["group_follower"]:
            logger.debug(f'{__PLUGIN_NAME}主播 <{bili_task_manager.liver_list[uid]["liver_name"]}> 已关注')
            return (False, uid + "(已关注)")

        if user_type == 0:
            bili_task_manager.add_user_follower(1, uid, user_id)
        elif user_type == 1:
            bili_task_manager.add_group_follower(1, uid, user_id)

        logger.info(f"{__PLUGIN_NAME}用户/群 <{user_id}> 关注主播 <{bili_task_manager.liver_list[uid]['liver_name']}> 成功")
        return (True, bili_task_manager.liver_list[uid]["liver_name"] + f"(uid: {uid})")
    except (BiliConnectionError, BiliAPIRetCodeError, BiliStatusCodeError):
        ex_type, ex_val, _ = sys.exc_info()
        exception_msg = f'【错误报告】\n获取主播 <{uid}> B站信息发生错误\n错误类型: {ex_type}\n错误值: {ex_val}\n'
        logger.error(f"{__PLUGIN_NAME}" + exception_msg + traceback.format_exc())
        return (False, uid + "(网络错误)")
    except BiliNoLiveRoom:
        return (False, uid + "(未开通直播间)")
    except BiliDatebaseError:
        ex_type, ex_val, _ = sys.exc_info()
        exception_msg = f'【错误报告】\n关注主播 <{uid}> 时数据库发生错误\n错误类型: {ex_type}\n错误值: {ex_val}\n'
        logger.error(f"{__PLUGIN_NAME}" + exception_msg + traceback.format_exc())
        return (False, uid + "(数据库错误)")
    except Exception:
        ex_type, ex_val, _ = sys.exc_info()
        exception_msg = f'【错误报告】\n关注主播 <{uid}> 时发生意料之外的错误\n错误类型: {ex_type}\n错误值: {ex_val}\n'
        logger.error(f"{__PLUGIN_NAME}" + exception_msg + traceback.format_exc())
        return (False, uid + "(未知错误,请查看日志)")

async def unfollower_liver(uid: str, user_id: str, user_type: int) -> Tuple[bool, str]:
    '''根据用户取关主播，修改主播文件

    Args:
        uid (str): 主播的uid
        user_id (int):QQ号/群号
        user_type (int): 0-个人用户，1-群

    Returns:
        Tuple[bool, str]: [是否成功，主播名 | 失败原因]
    '''
    if not uid.isdigit():
        logger.error(f'{__PLUGIN_NAME}存在错误参数 <{uid}>')
        return (False, uid + "(错误参数)")
    try:
        # 处理未关注
        if uid not in bili_task_manager.liver_list or \
            user_type == 0 and user_id not in bili_task_manager.liver_list[uid]["user_follower"] or \
            user_type == 1 and user_id not in bili_task_manager.liver_list[uid]["group_follower"]:
            logger.info(f'{__PLUGIN_NAME}用户/群 <{user_id}> 未关注主播 <{uid}>')
            return (False, uid + "(未关注)")

        # 进行取关
        liver_name = bili_task_manager.liver_list[uid]["liver_name"]
        if user_type == 0:
            bili_task_manager.remove_user_follower(1, uid, user_id)
        else:
            bili_task_manager.remove_group_follower(1, uid, user_id)
        
        return (True, liver_name + f"(uid: {uid})")
        
    except BiliDatebaseError:
        ex_type, ex_val, _ = sys.exc_info()
        exception_msg = f'【错误报告】\n取关主播 <{uid}> 时数据库发生错误\n错误类型: {ex_type}\n错误值: {ex_val}\n'
        logger.error(f"{__PLUGIN_NAME}" + exception_msg + traceback.format_exc())
        return (False, uid + "(数据库错误)")

async def follow_liver_list(
    user_id: int, 
    uid_list: List[str],
    user_type: int
    ) -> List[List[str]]:
    '''用户/群对主播进行关注

    Args:
        user_id (int): 用户qq或群号
        uid_list (List[str]): 关注的主播uid
        user_type (int): 0-用户, 1-群

    Returns:
        List[List[str]]: [[关注成功], [关注失败]]
    '''

    success_list = []
    fail_list = []

    for uid in uid_list:
        isSuccess, s = await follow_liver(uid, str(user_id), user_type)
        if isSuccess:
            success_list.append(s)
        else:
            fail_list.append(s)

    return [success_list, fail_list]


async def unfollow_liver_list(
    user_id: int, 
    uid_list: List[str],
    user_type: int
    ) -> List[List[str]]:
    '''用户/群对主播取关

    Args:
        user_id (int): 用户qq/群号
        uid_list (List[str]): 取关主播列表
        user_type (int): 0-用户, 1-群

    Returns:
        List[List[str]]: [成功列表，失败列表]
    '''

    success_list = []
    fail_list = []

    for uid in uid_list:
        isSuccess, s = await unfollower_liver(uid, str(user_id), user_type)
        if isSuccess:
            success_list.append(s)
        else:
            fail_list.append(s)
    
    return [success_list, fail_list]
