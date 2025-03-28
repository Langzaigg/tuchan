from nonebot import on_command, on_regex, on_message, get_bot, require
import nonebot
from nonebot.permission import SUPERUSER
from nonebot.message import run_preprocessor
from nonebot.exception import IgnoredException
from nonebot.params import CommandArg
from nonebot.adapters.onebot.v11 import (
    Bot,
    MessageEvent,
    GroupMessageEvent,
    FriendRequestEvent,
    GroupRequestEvent,
    GroupIncreaseNoticeEvent,
    GroupDecreaseNoticeEvent,
    GroupAdminNoticeEvent,
    GroupBanNoticeEvent,
    GroupRecallNoticeEvent,
    FriendRecallNoticeEvent,
    MessageSegment,
    Message,
)

import ATRI
import time, json, random, os, io, datetime

from PIL import Image
from collections import deque
from ATRI.log import logger as log
from ATRI.config import KaLiveConfig
from ATRI.utils.apscheduler import scheduler
from nonebot.adapters.onebot.v11 import Adapter


# 设置文件路径和读取间隔
file_path = KaLiveConfig.config['log_path']
json_path = './data/kalive/kalive.json'
jrlp_path = 'Z:/upload/图片/今日老婆'
interval = KaLiveConfig.config['interval']  # 间隔5秒
ddns_url = KaLiveConfig.config['ddns_url']

# 记录上次读取的位置
global kalive_dic
kalive_dic = {}
kalive_dic['last_position'] = 0
kalive_dic['last_time'] = time.time()

driver = ATRI.driver()

kalive_dic['ch'] = {'tv': {'isLive': False, 'title': '卡动漫', 'time': time.time(), 'watcher': 0}}
live_group = KaLiveConfig.config['live_group']
live_admin = KaLiveConfig.config['live_admin']
live_url = KaLiveConfig.config['live_url']
watch_url = KaLiveConfig.config['watch_url']

def jrlp(folder_path, c = ''):
    # 定义支持的图片后缀
    image_extensions = {'.jpg', '.jpeg', '.png'}
    characters = []
    chadic = {}

    # 遍历文件夹中的文件
    for filename in os.listdir(folder_path):
        # 分割文件名与扩展名
        basename, ext = os.path.splitext(filename)
        ext = ext.lower()
        if ext not in image_extensions:
            continue  # 跳过非图片文件 [[12]]

        # 分割角色名和后缀（按第一个下划线分割）
        parts = basename.split('_', 1)
        character = parts[0]
        chapath = os.path.join(folder_path, filename)

        if character in characters:
            chadic[character].append(chapath)
        else:
            characters.append(character)
            chadic[character]=[chapath]

    if not characters:
        return None  # 没有符合条件的文件

    # 随机选择一个条目
    selected = random.choice(characters)
    if c and c in characters:
        selected = c
    return random.choice(chadic[selected]), selected

global jrlp_dic
jrlp_dic = {}

jrlp_handle = on_command('。jrlp', aliases={'.jrlp','。今日老婆', '.今日老婆'}, priority=8, block=True)
@jrlp_handle.handle()
async def response_jrlp(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    uid = event.user_id
    now = datetime.datetime.now()
    if c:= args.extract_plain_text():
        img_path, cha = jrlp(jrlp_path, c)
    elif uid in jrlp_dic and jrlp_dic[uid][1] == now.day:
        img_path, cha = jrlp(jrlp_path, jrlp_dic[uid][0])
    else:
        img_path, cha = jrlp(jrlp_path)
    jrlp_dic[uid]= [cha, now.day]
    res = f'今天的你是{cha}粉丝！\n'
    await jrlp_handle.finish(MessageSegment.text(res) + MessageSegment.image(img_path), at_sender = True)




@driver.on_startup
async def kalive_startup():
    global kalive_dic
    try:
        with open(json_path, 'r', encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict) or 'ch' not in data:
                log.info("已读取1条数据")
            else:
                kalive_dic = data
                log.info(f"已读取{len(kalive_dic['ch'])}条数据")
    except:
        log.info("卡直播已初始化！")

@driver.on_shutdown
async def kalive_shutdown():
    global kalive_dic
    with open(json_path, 'w', encoding="utf-8") as f:
        json.dump(kalive_dic, f)
        log.info(f"已保存{len(kalive_dic['ch'])}条数据")

talk_handle = on_command('。直播', aliases={'.直播','。zb', '.zb'}, priority=9, block=True)
@talk_handle.handle()
async def response_zb(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and event.get_session_id().split("_")[1] == live_group:
        global kalive_dic
        if text := args.extract_plain_text():
            p = text.split(' ', 1)
            if len(p) == 2:
                if p[1] == 'del':
                    if p[0] in kalive_dic['ch']:
                        del kalive_dic['ch'][p[0]]
                        await talk_handle.finish(f"已删除{p[0]}频道信息")
                    else:
                        await talk_handle.finish(f"{p[0]}频道不存在")
                else:
                    if p[0] in kalive_dic['ch']:
                        pl = kalive_dic['ch'].get(p[0],{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
                        pl['title'] = p[1]
                        if pl['isLive']:
                            pl['time'] = time.time()
                        else:
                            pl['time'] = 0
                        kalive_dic['ch'][p[0]] = pl
                        await talk_handle.finish(f"已将{p[0]}频道的标题设置为{p[1]}")
                    else:
                        pl = kalive_dic['ch'].get(p[0],{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
                        pl['title'] = p[1]
                        pl['time'] = 0
                        kalive_dic['ch'][p[0]] = pl
                        await bot.send_private_msg(user_id=event.get_session_id().split("_")[2], message=f"推流地址：{live_url}\n推流码：{p[0]}")
                        await talk_handle.finish(f"已将{p[0]}频道的标题设置为{p[1]}")
            else:
                await talk_handle.finish("请输入频道代码和频道标题名")
        else:
            res = ''
            kalive_dic['ch'] = dict(sorted(kalive_dic['ch'].items(), key=lambda item: (item[1]['isLive'], item[1]['watcher'], item[1]['time']), reverse = True))
            for i in kalive_dic['ch'].keys():
                if kalive_dic['ch'][i]['isLive']:
                    if kalive_dic['ch'][i]['watcher']:
                        res += f"{i}频道直播了{get_time_interval(kalive_dic['ch'][i]['time'])}{kalive_dic['ch'][i]['title']}，{kalive_dic['ch'][i]['watcher']}人正在观看:{watch_url}/?id={i}\n"
                    else:
                        res += f"{i}频道直播了{get_time_interval(kalive_dic['ch'][i]['time'])}{kalive_dic['ch'][i]['title']}:{watch_url}/?id={i}\n"
                elif kalive_dic['ch'][i]['time']:
                    res += f"{i}频道的{kalive_dic['ch'][i]['title']}直播结束于{get_time_interval(kalive_dic['ch'][i]['time'])}前\n"
                else:
                    res += f"{i}频道的{kalive_dic['ch'][i]['title']}直播未开始\n"
            await talk_handle.finish(res.strip())


# watch = on_message(priority=80, block=False)
# @watch.handle()
# async def watch_live_log(bot: Bot, event: GroupMessageEvent):
#     # 读取新行和更新位置
#     if time.time() - kalive_dic['last_time'] < 10:
#         return
#     kalive_dic['last_time'] = time.time()
#     new_lines, kalive_dic['last_position'] = read_new_lines(file_path, kalive_dic['last_position'])
#     # 更新deque
#     for line in new_lines if len(new_lines) < 50 else []:
#         if 'rtmp publish' in line and 'New stream' in line:
#             live_id = line.split('streamPath=/live/')[-1].split()[0]
#             pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
#             pl['time'] = time.time()
#             pl['isLive'] = True
#             kalive_dic['ch'][live_id] = pl
#             await bot.send_group_msg(group_id=live_group, message=f"{live_id}频道开始直播{kalive_dic['ch'][live_id]['title']}:{watch_url}/?id={live_id}")

#         elif 'rtmp publish' in line and 'Close stream' in line:
#             live_id = line.split('streamPath=/live/')[-1].split()[0]
#             pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
#             pl['time'] = time.time()
#             if pl['isLive']:
#                 pl['isLive'] = False
#                 pl['watcher'] = 0
#                 await bot.send_group_msg(group_id=live_group, message=f"{live_id}频道的{kalive_dic['ch'][live_id]['title']}直播结束了")
#             kalive_dic['ch'][live_id] = pl

#         elif 'play] Join stream' in line:
#             live_id = line.split('streamPath=/live/')[-1].split()[0]
#             pl = kalive_dic['ch'].get(live_id,{'isLive': True, 'title': '', 'time': time.time(), 'watcher': 0})
#             pl['watcher'] += 1
#             pl['isLive'] = True
#             kalive_dic['ch'][live_id] = pl

#         elif 'play] Close stream' in line:
#             live_id = line.split('streamPath=/live/')[-1].split()[0]
#             pl = kalive_dic['ch'].get(live_id,{'isLive': True, 'title': '', 'time': time.time(), 'watcher': 0})
#             if pl['isLive'] and pl['watcher']:
#                 pl['watcher'] -= 1
#                 kalive_dic['ch'][live_id] = pl

async def watch_kalive_log() -> None:
    """
    @description  :
    检查数据库中所有主播的开播状态
    如果关注的主播开播，则通知所有关注的用户
    如果主播开播状态改变,则更新数据库
    ---------
    @param  :
    -------
    @Returns  :
    -------
    """
    
    new_lines, kalive_dic['last_position'] = read_new_lines(file_path, kalive_dic['last_position'])
    # 更新deque
    for line in new_lines if len(new_lines) < 50 else []:
        if 'rtmp publish' in line and 'New stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
            pl['time'] = time.time()
            pl['isLive'] = True
            kalive_dic['ch'][live_id] = pl
            sched_bot = nonebot.get_bot()
            await sched_bot.send_group_msg(group_id=live_group, message=f"{live_id}频道开始直播{kalive_dic['ch'][live_id]['title']}:{watch_url}/?id={live_id}")

        elif 'rtmp publish' in line and 'Close stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
            pl['time'] = time.time()
            if pl['isLive']:
                pl['isLive'] = False
                pl['watcher'] = 0
                sched_bot = nonebot.get_bot()
                await sched_bot.send_group_msg(group_id=live_group, message=f"{live_id}频道的{kalive_dic['ch'][live_id]['title']}直播结束了")
            kalive_dic['ch'][live_id] = pl

        elif 'play] Join stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': True, 'title': '', 'time': time.time(), 'watcher': 0})
            pl['watcher'] += 1
            pl['isLive'] = True
            kalive_dic['ch'][live_id] = pl

        elif 'play] Close stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': True, 'title': '', 'time': time.time(), 'watcher': 0})
            if pl['isLive'] and pl['watcher']:
                pl['watcher'] -= 1
                kalive_dic['ch'][live_id] = pl
                
            

def read_new_lines(file_path, start_position):
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            if file.seek(0,2) < start_position:
                start_position = 0
            file.seek(start_position)  # 从上次读取的位置开始
            lines = file.readlines()  # 读取所有行
            current_position = file.tell()  # 获取当前位置
            return lines, current_position
    except:
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()  # 读取所有行
            current_position = file.tell()  # 获取当前位置
            return lines, current_position



require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler
scheduler.add_job(watch_kalive_log, "interval", minutes=0.1, id="kalive_watch", misfire_grace_time=10)

    
def get_time_interval(timestamp):
    # 获取当前时间
    current_time = time.time()
    # 计算时间差
    time_diff = current_time - timestamp
    
    if time_diff < 60:
        return f"{int(time_diff)}秒"
    elif time_diff < 3600:
        return f"{int(time_diff // 60)}分钟"
    elif time_diff < 86400:
        return f"{int(time_diff // 3600)}小时"
    else:
        return f"{int(time_diff // 86400)}天"


talk_handle = on_command('。info', aliases={'.info','。sbhsk', '.sbhsk'}, priority=8, block=True)
@talk_handle.handle()
async def response_sbhsk(bot: Bot, event: MessageEvent, args: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent) and event.get_session_id().split("_")[1] == live_group and event.get_session_id().split("_")[2] in live_admin:
        if text := args.extract_plain_text():
            if 'all' in text:
                cpu_info = cpu()
                memo_info = memo()
                log_info = logined_users()
                res = ip_ad()
                res += f"\n\nCPU使用率：{cpu_info['cpu_avg']}%，核心状态：{cpu_info['per_cpu_avg']}"
                res += f"\n\n内存总量：{memo_info['memory_total']}，已使用：{memo_info['memory_used']}，使用率：{memo_info['memory_percent']}%"
                res += f"\n\n上次登录用户：{log_info[-1]['name']}，登录ip：{log_info[-1]['host']}，时间：{log_info[-1]['started']}"
                await bot.send_private_msg(user_id=event.get_session_id().split("_")[2], message=res.strip())
            if 'ddns' in text:
                ip, status =  ip_ddns()
                res = f"IP:{ip}"
                if status == 'good':
                    res += '\nDDNS已更新'
                elif status == 'nochg':
                    res += '\nDDNS未变化'
                else:
                    res += f'\n花生壳报错{status}'
                await bot.send_private_msg(user_id=event.get_session_id().split("_")[2], message=res.strip())

            
        else:
            res = ip_ad()
            await bot.send_private_msg(user_id=event.get_session_id().split("_")[2], message=res.strip())

    
import winreg	# winreg 是注册表控制模块
import psutil

def cpu():
	# 打开注册表
    key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0")
    # QueryValueEx 获取指定注册表中指定字段的内容
    cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")  # 获取cpu名称
    key.Close()
    data = dict(
        cpu_name=cpu_name[0],
        cpu_avg=psutil.cpu_percent(interval=0, percpu=False),  # cpu平均使用率
        per_cpu_avg=psutil.cpu_percent(interval=0, percpu=True),  # 每个cpu使用率
        cpu_core=psutil.cpu_count(False),  # cpu物理核心数量
        cpu_logic=psutil.cpu_count(True)  # cpu逻辑核心数量
    )
    return data

# 字节数转GB
def bytes_to_gb(sizes):
    sizes = round(sizes / (1024 ** 3), 2)
    return f"{sizes} GB"
    
def memo():
    data = dict(
        memory_total=bytes_to_gb(psutil.virtual_memory().total),  # 内容总量
        memory_available=bytes_to_gb(psutil.virtual_memory().available),  # 内容可用量
        memory_percent=psutil.virtual_memory().percent,  # 内存使用率
        memory_used=bytes_to_gb(psutil.virtual_memory().used),  # 内存使用量
    )
    return data
    
def swap():
    data = dict(
        swap_total=bytes_to_gb(psutil.swap_memory().total),	 # 交换分区总容量 
        swap_used=bytes_to_gb(psutil.swap_memory().used),	# 交换分区使用量
        swap_free=bytes_to_gb(psutil.swap_memory().free),	# 交换分区剩余量
        swap_percent=bytes_to_gb(psutil.swap_memory().percent),	# 交换分区使用率
    )
    return data

def net():
    # 获取地址信息
    addrs = psutil.net_if_addrs()
    # val.family.name取出协议地址族名称，AF_INET（ipv4)
    addrs_info = {
        k: [
            dict(
                family=val.family.name,  # 协议名称
                address=val.address,	# ip地址
                netmask=val.netmask,	# 子网掩码
                broadcast=val.broadcast	# 网关
            )
            for val in v if val.family.name == "AF_INET"
        ][0]
        for k, v in addrs.items()
    }
    # 获取输入输出信息（收发包数，收发字节数）
    io = psutil.net_io_counters(pernic=True)
    data = [
        dict(
            name=k,
            bytes_sent=v.bytes_sent,	# 发送字节数量
            bytes_recv=v.bytes_recv,	# 接受字节数量
            packets_sent=v.packets_sent,
            packets_recv=v.packets_recv,
            **addrs_info[k]
        )
        for k, v in io.items()
    ]
    return [i for i in data if "'127." not in str(i) and "'192." not in str(i) and "'172." not in str(i)]

import datetime
# 时间戳转化为时间字符方法
def td(tm):
    dt = datetime.datetime.fromtimestamp(tm)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

# 获取当前日期时间
def dt():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

# 上次开机时间
def last_boot_time():
    # psutil.boot_time() 返回的是时间戳
    return td(psutil.boot_time())

# 上次登录用户信息
def logined_users():
    users = psutil.users()
    data = [
        dict(
            name=v.name,		 # 登录用户名
            terminal=v.terminal, # 登录终端
            host=v.host,		# 登录主机
            started=td(v.started),	# 登录时间
            pid=v.pid			# 进程号
        )
        for v in users
    ]
    return data

import requests
def ip_ad():
    res = requests.get('http://myip.ipip.net', timeout=1).text
    return res

def ip_ddns():
    res = requests.get(ddns_url, timeout=1).text.split()
    return res[-1], res[0]
