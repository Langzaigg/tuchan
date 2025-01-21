from nonebot import on_command, on_regex, on_message
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
import time, json
from collections import deque
from ATRI.log import logger as log
from ATRI.utils.apscheduler import scheduler
from nonebot.adapters.onebot.v11 import Adapter


# 设置文件路径和读取间隔
file_path = "D:\\nms\\server-logs\\nms-service.out.log"
json_path = './data/kalive/kalive.json'
interval = 5  # 间隔5秒

# 记录上次读取的位置
global kalive_dic
kalive_dic = {}
kalive_dic['last_position'] = 0
kalive_dic['last_time'] = time.time()

driver = ATRI.driver()

kalive_dic['ch'] = {'tv': {'isLive': False, 'title': '卡动漫', 'time': time.time(), 'watcher': 0}}
live_group = '149378291'
live_admin = ['448489320', '1104155706', '619227931']

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
                        await bot.send_private_msg(user_id=event.get_session_id().split("_")[2], message=f"推流地址：rtmp://legend503.site:5005/live\n推流码：{p[0]}")
                        await talk_handle.finish(f"已将{p[0]}频道的标题设置为{p[1]}")
            else:
                await talk_handle.finish("请输入频道代码和频道标题名")
        else:
            res = ''
            kalive_dic['ch'] = dict(sorted(kalive_dic['ch'].items(), key=lambda item: (item[1]['isLive'], item[1]['watcher'], item[1]['time']), reverse = True))
            for i in kalive_dic['ch'].keys():
                if kalive_dic['ch'][i]['isLive']:
                    if kalive_dic['ch'][i]['watcher']:
                        res += f"{i}频道直播了{get_time_interval(kalive_dic['ch'][i]['time'])}{kalive_dic['ch'][i]['title']}，{kalive_dic['ch'][i]['watcher']}人正在观看:http://legend503.site:5007/live/?id={i}\n"
                    else:
                        res += f"{i}频道直播了{get_time_interval(kalive_dic['ch'][i]['time'])}{kalive_dic['ch'][i]['title']}:http://legend503.site:5007/live/?id={i}\n"
                elif kalive_dic['ch'][i]['time']:
                    res += f"{i}频道的{kalive_dic['ch'][i]['title']}直播结束于{get_time_interval(kalive_dic['ch'][i]['time'])}前\n"
                else:
                    res += f"{i}频道的{kalive_dic['ch'][i]['title']}直播未开始\n"
            await talk_handle.finish(res.strip())


watch = on_message(priority=80, block=False)
@watch.handle()
async def watch_live_log(bot: Bot, event: GroupMessageEvent):
    # 读取新行和更新位置
    if time.time() - kalive_dic['last_time'] < 10:
        return
    kalive_dic['last_time'] = time.time()
    new_lines, kalive_dic['last_position'] = read_new_lines(file_path, kalive_dic['last_position'])
    # 更新deque
    for line in new_lines if len(new_lines) < 50 else []:
        if 'rtmp publish' in line and 'New stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
            pl['time'] = time.time()
            pl['isLive'] = True
            kalive_dic['ch'][live_id] = pl
            await bot.send_group_msg(group_id=live_group, message=f"{live_id}频道开始直播{kalive_dic['ch'][live_id]['title']}:http://legend503.site:5007/live/?id={live_id}")

        elif 'rtmp publish' in line and 'Close stream' in line:
            live_id = line.split('streamPath=/live/')[-1].split()[0]
            pl = kalive_dic['ch'].get(live_id,{'isLive': False, 'title': '', 'time': time.time(), 'watcher': 0})
            pl['time'] = time.time()
            if pl['isLive']:
                pl['isLive'] = False
                pl['watcher'] = 0
                await bot.send_group_msg(group_id=live_group, message=f"{live_id}频道的{kalive_dic['ch'][live_id]['title']}直播结束了")
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
            file.seek(start_position)  # 从上次读取的位置开始
            lines = file.readlines()  # 读取所有行
            current_position = file.tell()  # 获取当前位置
            return lines, current_position
    except:
        with open(file_path, 'r', encoding='utf-8') as file:
            lines = file.readlines()  # 读取所有行
            current_position = file.tell()  # 获取当前位置
            return lines, current_position

    
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
    res = requests.get('http://myip.ipip.net', timeout=5).text
    return res