from nonebot import on_command, on_regex, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent, PrivateMessageEvent, GroupMessageEvent, MessageSegment
import time
from nonebot.adapters import Message
from nonebot.params import CommandArg
import requests, re, asyncio

default_api = 'app-Uk03IYYgrFueliwe0xo7XJf6'
api_dic = {'研究':'app-s9ggihmZ7cTzQaTdGe7s7BSq',
           '简洁':'app-Uk03IYYgrFueliwe0xo7XJf6',
           '5e':'fastgpt-nRhwhRcGIqWDmzdcXt95UeRfJUj7YoYf4RKTPKkBlVjVmjDkn1ib',
           '深入':'app-62VOZUdNdLsRDA1rrycicnyP',
           'bing': 'app-SkEnMkojTE9AruVQqo9nWKAL'}
# default_api = 'app-SkEnMkojTE9AruVQqo9nWKAL'
# api_dic = {'dnd':'fastgpt-zamtZ3rk5w4OSm5EAmFAt8krK3QjShAG5cZLRiDQs5As2lk8xn6PtWWDvy0Gt',
#            '旮旯':'fastgpt-r49xAjzWWMzYgmOTcGEwNFkhPcwv4MTzZ30EDW10rwNoQUv0MxQP',
#            '5e':'fastgpt-nRhwhRcGIqWDmzdcXt95UeRfJUj7YoYf4RKTPKkBlVjVmjDkn1ib',
#            '测试':'fastgpt-jnlpcbXrQLHoTKqBGe1bovpgryk4if33WNJt9KLvHbILhbdRezsy3ApRRJfqdbM',
#            'metaso': 'app-62VOZUdNdLsRDA1rrycicnyP'}
group_api = {}
cid_dic = {}

# talk_handle = on_command('.t',aliases={'.T',"。t","。T"}, priority=7, block=True)
# @talk_handle.handle()
# async def response_a_talk(event: MessageEvent, args: Message = CommandArg()):
#     if isinstance(event, GroupMessageEvent):
#         chat_key = 'group_' + event.get_session_id().split("_")[1]
#     elif isinstance(event, PrivateMessageEvent):
#         chat_key = 'private_' + event.get_user_id()
#     if text := args.extract_plain_text():
#         api_key = group_api.get(chat_key, default_api)
#         ans = fastgpt(text, chat_key, api_key)
#         await talk_handle.finish(remove_markers(ans))
#     else:
#         await talk_handle.finish("你想说什么")
    
# ts_handle = on_command('.ts',aliases={'.Ts',"。ts","。Ts"}, priority=8, block=True)
# @ts_handle.handle()
# async def response_a_ts(event: MessageEvent, args: Message = CommandArg()):
#     if isinstance(event, GroupMessageEvent):
#         chat_key = 'group_' + event.get_session_id().split("_")[1]
#     elif isinstance(event, PrivateMessageEvent):
#         chat_key = 'private_' + event.get_user_id()
#     global group_api
#     if text := args.extract_plain_text():
#         if 'fastgpt' in text:
#             group_api[chat_key] = text
#             await ts_handle.finish(f'已设置对话{chat_key}的APIKEY为{text}')
#         else:
#             group_api[chat_key] = api_dic.get(text, default_api)
#             await ts_handle.finish(f'已设置对话{chat_key}的预设为{text}')
#     else:
#         await ts_handle.finish("请输入APIKEY或者预设名")
    


talk_handle = on_command('.t',aliases={'.T',"。t","。T"}, priority=7, block=True)
@talk_handle.handle()
async def response_a_talk(event: MessageEvent, args: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent):
        chat_key = 'group_' + event.get_session_id().split("_")[1]
    elif isinstance(event, PrivateMessageEvent):
        chat_key = 'private_' + event.get_user_id()
    if text := args.extract_plain_text():
        api_key = group_api.get(chat_key, default_api)
        cid = cid_dic.get(chat_key, '')
        ans, cid = dify(text, chat_key, api_key, cid)
        cid_dic[chat_key] = cid
        res_list = remove_markdown_and_split_images(ans)
        for res in res_list:
            if res:
                if res.startswith('http') and res.endswith('.jpg'):
                    await talk_handle.send(MessageSegment.image(file = res or ''))
                else:
                    await talk_handle.send(res)
            await asyncio.sleep(1)
    else:
        await talk_handle.finish("你想说什么")

ts_handle = on_command('.ts',aliases={'.Ts',"。ts","。Ts"}, priority=8, block=True)
@ts_handle.handle()
async def response_a_ts(event: MessageEvent, args: Message = CommandArg()):
    if isinstance(event, GroupMessageEvent):
        chat_key = 'group_' + event.get_session_id().split("_")[1]
    elif isinstance(event, PrivateMessageEvent):
        chat_key = 'private_' + event.get_user_id()
    global group_api
    if text := args.extract_plain_text():
        if chat_key in cid_dic:
            del cid_dic[chat_key]
        if 'app' in text:
            group_api[chat_key] = text
            await ts_handle.finish(f'已设置对话{chat_key}的APIKEY为{text}')
        else:
            group_api[chat_key] = api_dic.get(text, default_api)
            await ts_handle.finish(f'已设置对话{chat_key}的预设为{text}')
    else:
        await ts_handle.finish("请输入APIKEY或者预设名")

def remove_markers(text):
    return text.replace('**', '').replace('###', '').replace('---', '').strip()

def fastgpt(question, id = '1234', apikey = default_api, apiurl='http://localhost:3000/api/v1/chat/completions'):
    headers = {
        'Authorization': f'Bearer {apikey}',
        'Content-Type': 'application/json'
    }
    data = {
        "chatId": id,
        "stream": False,
        "detail": False,
        "messages": [
            {
                "content": question,
                "role": "user"
            }
        ]
    }
    response = requests.post(apiurl, headers=headers, json=data)
    print(response.json())
    res = response.json().get('choices')[-1].get('message').get('content')
    if isinstance(res, str):
        return res
    return res[-1].get('text').get('content')


def dify(question, user = '1234', apikey = default_api, cid = '', apiurl='http://localhost:4080/v1/chat-messages'):
    headers = {
        'Authorization': f'Bearer {apikey}',
        'Content-Type': 'application/json'
    }
    data = {
        "inputs" : {},
        "query": question,
        "user": user,
        "conversation_id": cid,
        "response_mode": "blocking",
    }
    response = requests.post(apiurl, headers=headers, json=data)
    print(response.json())
    res = response.json().get('answer')
    cid = response.json().get('conversation_id')
    return res, cid

def remove_markdown_and_split_images(text):
    img_pattern = re.compile(r'!\[.*?\]\((.*?)\)')
    matches = list(img_pattern.finditer(text))
    
    parts = []
    last_end = 0
    for match in matches:
        start, end = match.start(), match.end()
        url = match.group(1)
        parts.append(text[last_end:start])
        parts.append(url)
        last_end = end
    parts.append(text[last_end:])
    
    cleaned_parts = []
    for part in parts:
        if part in [m.group(1) for m in matches]:
            cleaned_parts.append(part)
            continue
        
        # 修复强调语法（支持**和__，且确保闭合符号一致）
        cleaned = re.sub(r'(\*\*|__)(.*?)\1', r'\2', part)  # 关键修改点1
        cleaned = re.sub(r'(\*|_)(.*?)\1', r'\2', cleaned)
        
        # 修复链接语法
        cleaned = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', cleaned)
        
        # 修复代码块
        cleaned = re.sub(r'`(.*?)`', r'\1', cleaned)
        
        # 修复标题
        cleaned = re.sub(r'^#+\s*', '', cleaned, flags=re.MULTILINE)
        
        # 修复列表符号（严格匹配格式）
        cleaned = re.sub(r'^\s*[-*+]\s+', '', cleaned, flags=re.MULTILINE)  # 关键修改点2
        
        if cleaned.strip():
            cleaned_parts.append(cleaned.strip())
    
    return cleaned_parts