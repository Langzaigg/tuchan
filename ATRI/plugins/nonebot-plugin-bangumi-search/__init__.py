from nonebot.plugin import on_command
from nonebot.adapters.onebot.v11 import Message, MessageEvent, GroupMessageEvent, MessageSegment
from nonebot.adapters.onebot.v11.helpers import HandleCancellation
from nonebot.typing import T_State
from nonebot.params import CommandArg

from .database import search_confirm, bgm_screenshoot1, bgm_screenshoot2



bgm = on_command('.bgm',aliases={"。bgm"}, priority=12, block=True)



@bgm.handle()
async def get_search_input(event: MessageEvent, state: T_State, args: Message = CommandArg()):
    if search_content := args.extract_plain_text():
        search_input = 0
        state["search_type"], state["search_menu"], state["search_sort"] = await search_confirm(search_input)
        search_sort, search_type = state["search_sort"], state["search_sort"]
        image, state["content"] = await bgm_screenshoot1(search_content,search_sort,search_type)
        if image == None or state["content"] == None:
            await bgm.finish(Message('没有搜索到相关内容哦~'))
        else:
            await bgm.send(MessageSegment.image(f"base64://{image}"), at_sender=True)
    else:
        
        await bgm.finish(Message('请输入搜索内容~'))


@bgm.got(
    "browse_num", prompt="请输入你想访问的条目的数字顺序：",
    parameterless=[HandleCancellation("已取消")]
)
async def get_browse_num(state: T_State):
    browse_num_base = str(state["browse_num"])
    if browse_num_base not in ["0","1","2","3","4","5","6","7","8","9","10"]:
        await bgm.finish('未输入编号，已取消')
    browse_num = int(browse_num_base)
    content, search_menu = state["content"], state["search_menu"]
    url, image_l, image_r, image_c = await bgm_screenshoot2(content,search_menu,browse_num)
    await bgm.finish(
            f"{url}"
            + MessageSegment.image(f"base64://{image_l}")
            + MessageSegment.image(f"base64://{image_r}")
            + MessageSegment.image(f"base64://{image_c}"), at_sender=True
            )
