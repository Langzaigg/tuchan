from nonebot import on_command, on_regex, on_message
from nonebot.adapters.onebot.v11 import Message, MessageEvent, PrivateMessageEvent, GroupMessageEvent, MessageSegment
import time
from nonebot.adapters import Message
from nonebot.params import CommandArg
import requests

default_api = 'fastgpt-b9cqyYtmwcjznDToughbDzkBD24e2G5s1CjzwKyF7z0iVNI6H8AX'
api_dic = {'dnd':'fastgpt-zamtZ3rk5w4OSm5EAmFAt8krK3QjShAG5cZLRiDQs5As2lk8xn6PtWWDvy0Gt',
           '旮旯':'fastgpt-r49xAjzWWMzYgmOTcGEwNFkhPcwv4MTzZ30EDW10rwNoQUv0MxQP',
           '5e':'fastgpt-nRhwhRcGIqWDmzdcXt95UeRfJUj7YoYf4RKTPKkBlVjVmjDkn1ib',
           '测试':'fastgpt-jnlpcbXrQLHoTKqBGe1bovpgryk4if33WNJt9KLvHbILhbdRezsy3ApRRJfqdbM',
           'bing': 'fastgpt-nzcYPcwIs5sLCx5I2CJFY8L9YenYQFdgHn2RKTYwFje02dU1AgtlbbX94W'}
group_api = {}

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

def remove_markers(text):
    return text.replace('**', '').replace('###', '')

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