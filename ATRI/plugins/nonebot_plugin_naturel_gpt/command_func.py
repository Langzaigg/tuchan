import asyncio
import difflib
import json
import re
from typing import Optional, Dict, Any

from .chat import Chat
from .chat_manager import ChatManager
from .config import *
from .logger import logger
from .persistent_data_manager import PersistentDataManager
from .llm_tool_plugins import enable_anima_tool, disable_anima_tool
from .llm_tool_plugins import anima_generate
from . import draw_db

# 选项类型  bool只要有就是True，str则需要跟上参数值
option_type = {
    'target': str,
    'global': bool,
    'admin': bool,
    'deep': bool,
    'to_default': bool,
    'default': str,
    'show': bool,
}

class CommandManager:
    def __init__(self):
        self.command_router = {}
        # 指令路由 通过指令路由来规范指令的参数格式
        # arg_list: [参数1, 参数2, ...] 多余的参数会被拼接到最后一个参数中
        # func: 指令执行函数
        # command_router = {
        #     'rg': {'arg_list': [], 'func': None},
        # }

    def register(self, route, params: Optional[list] = None):
        """注册指令修饰方法"""
        # print('register:', route, params)
        def wrapper(func):
            self.command_router[route] = {'arg_list': params or [], 'func': func}
            return func
        return wrapper

    def execute(self, chat:Chat, command:str, chat_presets_dict:dict, user_id:str='') -> Optional[dict]:
        """执行指令"""
        # 特殊处理: rg draw-XXXXXX 查询绘图提示词
        cmd_stripped = command.strip()
        draw_id_match = re.match(r'^rg\s+draw-([A-Za-z0-9]{6})$', cmd_stripped)
        if draw_id_match:
            task_id = f"draw-{draw_id_match.group(1)}"
            return self._query_draw_prompt(task_id)

        option_dict, param_dict, target_route = self.resolve_command(command)
        logger.info(f'执行命令: "{command}";  指令匹配路由: {target_route}')
        if target_route:
            try:
                return self.command_router[target_route]['func'](option_dict, param_dict, chat, chat_presets_dict, user_id)
            except Exception as e:
                return {'error': str(e)}
        return None

    def _query_draw_prompt(self, task_id: str) -> dict:
        """查询绘图提示词并返回 JSON"""
        prompt_data = draw_db.get_prompt(task_id)
        if prompt_data is None:
            return {'msg': f"未找到任务编号 {task_id} 对应的绘图提示词", 'no_img': True}
        return {'msg': json.dumps(prompt_data, ensure_ascii=False, indent=2), 'no_img': True}

    def submit_commands(self):
        """提交指令注册 *在所有指令注册完成后调用*"""
        # 将指令路由字典根据键中包含的`/`数量进行降序排序 以便于匹配时优先匹配更长的指令
        self.command_router = dict(sorted(self.command_router.items(), key=lambda x: len(x[0].split('/')), reverse=True))
        # print('所有指令注册完成 共计:', len(self.command_router), '条指令')

    def resolve_command(self, command:str):
        """解析命令"""
        # 命令格式: 一级命令 二级命令 ... (选项1 选项2) ... 参数1 参数2 参数3
        # 命令名和参数之间必须有一个或多个空格
        # 命令名和参数之间可以有换行
        # 选项和参数顺序可以任意

        # 生成命令参数列表
        cmd_list = [c.strip() for c in command.split(' ') if c.strip()]

        # 生成命令选项字典 并去除已经解析的选项
        # 如果是以 - 开头的参数，根据参数类型进行解析
        # 格式: -参数名 参数值 (对于布尔值，参数值可以省略)
        option_dict = {}
        for i in range(len(cmd_list)):
            if cmd_list[i].startswith('-'):
                option = cmd_list[i][1:]
                if option not in option_type:
                    continue  # 跳过未定义的选项
                cmd_list[i] = ''  # 去除已经解析的参数
                if option_type[option] == bool:
                    option_dict[option] = True
                elif option_type[option] == str:
                    if i + 1 < len(cmd_list):
                        option_dict[option] = cmd_list[i + 1]
                        cmd_list[i + 1] = ''  # 去除已经解析的参数
        cmd_list = [c.strip() for c in cmd_list if c.strip()]  # 去除已经解析的参数

        # 按照 一级命令/二级命令... 匹配命令路由 如果有多余的参数则以空格为间隔拼接后存放到最后一个参数中
        target_route = ''
        for route, params_list in self.command_router.items():
            params_list = params_list['arg_list']
            # print(f"command route matching: {route} => {params_list}")
            try:
                if '/'.join(cmd_list).startswith(route):
                    param_dict = {}
                    # 截去路由匹配的一级/二级...命令
                    if len(params_list) > 0:
                        cmd_list = cmd_list[len(route.split('/')):]
                        for i in range(len(params_list) - 1):
                            param_dict[params_list[i]] = cmd_list[i]
                        # 将剩余的内容以空格为间隔拼接后存放到最后一个参数中
                        param_dict[params_list[-1]] = ' '.join(cmd_list[len(params_list) - 1:])
                    target_route = route
                    break
            except Exception as e: # 解析出错跳过
                logger.error(f'解析指令出错: {command} => reason: {e}')
                continue
        else:
            param_dict = {}
        return option_dict, param_dict, target_route

cmd:CommandManager = CommandManager()


def _refresh_dynamic_personas() -> int:
    try:
        return reload_dynamic_personas()
    except Exception as e:
        logger.warning(f"动态加载人格失败: {e!r}")
        return 0


def _available_presets(chat_presets_dict: dict) -> Dict:
    _refresh_dynamic_personas()
    presets = dict(config.PRESETS)
    presets.update(chat_presets_dict)
    return presets


def _render_preset_list(chat: Chat, chat_presets_dict: dict, admin: bool = False) -> str:
    presets = _available_presets(chat_presets_dict)
    presets_show_text = '\n'.join([
        f'  -> {k + " (当前)" if k == chat.preset_key else k}'
        for k in presets.keys()
    ])
    if admin:
        return (
            f"当前可用人格预设列表:\n"
            f"{presets_show_text}\n"
            f"=======================\n"
            f"+ 切换人格: rg set <预设名>\n"
            f"+ 查看人格: rg query <预设名>\n"
            f"+ 刷新/展示人格列表: rg 或 rg list\n"
            f"+ 编辑预设: rg edit <预设名> <人格信息> <-global?>\n"
            f"+ 添加预设: rg new <预设名> <人格信息> <-global?>\n"
            f"+ 删除预设: rg del <预设名> <-global?>\n"
            f"+ 重命名预设: rg rename <原预设名> <新预设名> <-global?>\n"
            f"+ 开关会话: rg <on/off> <-global?>\n"
            f"+ 重置会话: rg reset <-global?>\n"
            f"+ 清除记忆: rg mem clear <group|user|all>\n"
            f"+ 查询记忆: rg mem\n"
            f"+ 设置昵称: rg nn <昵称>\n"
            f"+ 查询会话(超管): rg chats\n"
            f"* -global 参数表示全局设置，仅超管可用\n"
        )
    return (
        f"会话: {chat.chat_key} [{'启用' if chat.is_enable else '禁用'}]\n"
        f"当前可用人格预设列表:\n"
        f"{presets_show_text}\n"
        f"提示: 使用 `rg set <预设名>` 切换人格。"
    )

""" 注册指令 """
@cmd.register(route='rg')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    return {'msg': _render_preset_list(chat, chat_presets_dict, bool(option_dict.get('admin')))}

@cmd.register(route='rg/list')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    return {'msg': _render_preset_list(chat, chat_presets_dict, bool(option_dict.get('admin')))}


@cmd.register(route='rg/set', params=['preset_key', 'preset_intro'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    _refresh_dynamic_personas()
    target_preset_key = param_dict['preset_key']
    
    # 尝试从全局预设添加到会话预设
    if target_preset_key in config.PRESETS and target_preset_key not in chat_presets_dict:
        chat.add_preset_from_config(target_preset_key, config.PRESETS[target_preset_key])
        chat_presets_dict = chat.chat_data.preset_datas
    
    # 如果预设不存在，匹配最相似的预设
    if target_preset_key not in chat_presets_dict:
        available_presets = _available_presets(chat_presets_dict)
        matched_preset_keys = difflib.get_close_matches(target_preset_key, available_presets.keys(), n=1, cutoff=0.3)
        if not matched_preset_keys:
            return {'msg': "找不到匹配的人格预设"}
        target_preset_key = matched_preset_keys[0]
        # 匹配到的预设可能在全局预设中但不在会话预设中
        if target_preset_key in config.PRESETS and target_preset_key not in chat_presets_dict:
            chat.add_preset_from_config(target_preset_key, config.PRESETS[target_preset_key])
            chat_presets_dict = chat.chat_data.preset_datas
        if target_preset_key not in chat_presets_dict:
            return {'msg': "找不到匹配的人格预设"}

    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.change_presettings_for_all(preset_key=target_preset_key)
        return {'msg': f"应用预设: {target_preset_key} (￣▽￣)-Completed! (所有会话) '成功:{success_cnt}, 失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        target_chat.change_presettings(target_preset_key)
        return {'msg': f"应用预设: {target_preset_key} (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
    else:   # 当前会话应用
        chat.change_presettings(target_preset_key)
        return {'msg': f"应用预设: {target_preset_key} (￣▽￣)-ok!", 'is_progress': True}

@cmd.register(route='rg/new', params=['preset_key', 'preset_intro'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    target_preset_key = param_dict['preset_key']
    bot_self_introl = param_dict.get('preset_intro', '')
    
    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.add_preset_for_all(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        return {'msg': f"添加预设: {target_preset_key} (￣▽￣)-ok! (所有会话) 成功:{success_cnt}，失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        success, err_msg = target_chat.add_preset(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        if success:
            return {'msg': f"添加预设: {target_preset_key} (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
        else:
            return {'msg': f"添加预设: {target_preset_key} 失败! (会话: {target_chat_key}) (；′⌒`)\n{err_msg}", 'is_progress': True}
    else:   # 当前会话应用
        success, err_msg = chat.add_preset(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        if success:
            return {'msg': f"添加预设: {target_preset_key} (￣▽￣)-ok!", 'is_progress': True}
        else:
            return {'msg': f"添加预设: {target_preset_key} 失败! (；′⌒`)\n{err_msg}", 'is_progress': True}

@cmd.register(route='rg/edit', params=['preset_key', 'preset_intro'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    target_preset_key = param_dict['preset_key']
    bot_self_introl = param_dict.get('preset_intro', '')
    
    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.update_preset_for_all(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        return {'msg': f"编辑预设: {target_preset_key} (￣▽￣)-ok! (所有会话) 成功:{success_cnt}，失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        success, err_msg = target_chat.update_preset(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        if success:
            return {'msg': f"编辑预设: {target_preset_key} (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
        else:
            return {'msg': f"编辑预设: {target_preset_key} (会话: {target_chat_key}) 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}
    else:   # 当前会话应用
        success, err_msg = chat.update_preset(preset_key=target_preset_key, bot_self_introl=bot_self_introl)
        if success:
            return {'msg': f"编辑预设: {target_preset_key} (￣▽￣)-ok!", 'is_progress': True}
        else:
            return {'msg': f"编辑预设: {target_preset_key} 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}

@cmd.register(route='rg/del', params=['preset_key'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    target_preset_key = param_dict['preset_key']

    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.del_preset_for_all(preset_key=target_preset_key)
        return {'msg': f"删除预设: {target_preset_key} (￣▽￣)-ok! (所有会话) 成功:{success_cnt}，失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        success, err_msg = target_chat.del_preset(preset_key=target_preset_key)
        if success:
            return {'msg': f"删除预设: {target_preset_key} (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
        else:
            return {'msg': f"删除预设: {target_preset_key} (会话: {target_chat_key}) 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}
    else:   # 当前会话应用
        success, err_msg = chat.del_preset(preset_key=target_preset_key)
        if success:
            return {'msg': f"删除预设: {target_preset_key} (￣▽￣)-ok!", 'is_progress': True}
        else:
            return {'msg': f"删除预设: {target_preset_key} 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}
        
@cmd.register(route='rg/rename', params=['old_preset_key', 'new_preset_key'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    target_old_preset_key = param_dict['old_preset_key']
    target_new_preset_key = param_dict['new_preset_key']

    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.rename_preset_for_all(old_preset_key=target_old_preset_key, new_preset_key=target_new_preset_key)
        return {'msg': f"重命名预设: {target_old_preset_key} (￣▽￣)-ok! (所有会话) 成功:{success_cnt}，失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        success, err_msg = target_chat.rename_preset(old_preset_key=target_old_preset_key, new_preset_key=target_new_preset_key)
        if success:
            return {'msg': f"重命名预设: {target_old_preset_key} (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
        else:
            return {'msg': f"重命名预设: {target_old_preset_key} (会话: {target_chat_key}) 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}
    else:   # 当前会话应用
        success, err_msg = chat.rename_preset(old_preset_key=target_old_preset_key, new_preset_key=target_new_preset_key)
        if success:
            return {'msg': f"重命名预设: {target_old_preset_key} (￣▽￣)-ok!", 'is_progress': True}
        else:
            return {'msg': f"重命名预设: {target_old_preset_key} 错误 ＞﹏＜!\n{err_msg}", 'is_progress': True}

@cmd.register(route='rg/reset')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if option_dict.get('global'):   # 全局应用
        success_cnt, fail_cnt = ChatManager.instance.reset_chat_for_all()
        return {'msg': f"重置会话(￣▽￣)-ok! (所有会话) 成功:{success_cnt}，失败:{fail_cnt}", 'is_progress': True}
    elif option_dict.get('target'): # 指定会话应用
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if not target_chat:
            return {'msg': f"会话: {target_chat_key} 不存在! (；′⌒`)"}
        if target_chat.reset_chat():
            return {'msg': f"重置 (￣▽￣)-ok! (会话: {target_chat_key})", 'is_progress': True}
        else:
            return {'msg': f"重置 (会话: {target_chat_key}) 错误 ＞﹏＜!", 'is_progress': True}
    else:   # 当前会话应用
        if chat.reset_chat():
            return {'msg': f"重置 (￣▽￣)-ok!", 'is_progress': True}
        else:
            return {'msg': f"重置 错误 ＞﹏＜!", 'is_progress': True}

@cmd.register(route='rg/mem')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    preset = chat.chat_preset
    pdm = PersistentDataManager.instance
    is_global = chat.chat_data.global_memory_enabled
    parts = []

    # global 状态提示
    if is_global:
        parts.append("[global 记忆模式: 已开启]")

    # 群记忆
    if is_global:
        mem = chat.chat_data.global_chat_memory
    else:
        mem = preset.chat_memory
    if mem:
        lines = [f"  {i+1}. {k}: {v}" for i, (k, v) in enumerate(mem.items())]
        parts.append(f"[群记忆] ({len(mem)}/{config.MEMORY_MAX_LENGTH})\n" + "\n".join(lines))
    else:
        parts.append(f"[群记忆] (0/{config.MEMORY_MAX_LENGTH}) 无")

    # 当前用户记忆
    if is_global:
        user_mem = pdm.get_global_user_memories(user_id) if user_id else {}
    else:
        user_mem = preset.user_memories.get(user_id, {}) if user_id else {}
    if user_mem:
        lines = [f"  {i+1}. {k}: {v}" for i, (k, v) in enumerate(user_mem.items())]
        parts.append(f"[你的记忆] ({len(user_mem)}/{config.MEMORY_MAX_LENGTH})\n" + "\n".join(lines))
    else:
        parts.append(f"[你的记忆] (0/{config.MEMORY_MAX_LENGTH}) 无")

    # 当前用户印象
    if user_id and user_id in preset.chat_impressions:
        imp = preset.chat_impressions[user_id].chat_impression.strip()
        if imp:
            parts.append(f"[对你的印象]\n  {imp}")
        else:
            parts.append("[对你的印象] 暂无")
    else:
        parts.append("[对你的印象] 暂无")

    return {'msg': f"人格: {preset.preset_key}\n\n" + "\n\n".join(parts)}

@cmd.register(route='rg/mem/clear', params=['scope'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    scope = param_dict.get('scope', '').strip().lower()
    preset = chat.chat_preset
    pdm = PersistentDataManager.instance
    is_global = chat.chat_data.global_memory_enabled

    if scope == 'group':
        if is_global:
            chat.chat_data.global_chat_memory.clear()
        else:
            preset.chat_memory.clear()
        return {'msg': f"已清除 {preset.preset_key} 的群记忆 (￣▽￣)-ok!", 'is_progress': True}
    elif scope == 'user':
        if not user_id:
            return {'msg': "无法获取当前用户信息 (；′⌒`)"}
        if is_global:
            pdm.set_global_user_memories(user_id, {})
            return {'msg': f"已清除 {preset.preset_key} 的用户记忆 (global) (￣▽￣)-ok!", 'is_progress': True}
        if user_id in preset.user_memories:
            del preset.user_memories[user_id]
            return {'msg': f"已清除 {preset.preset_key} 的用户记忆 (￣▽￣)-ok!", 'is_progress': True}
        return {'msg': f"{preset.preset_key} 没有关于你的记忆 (；′⌒`)"}
    elif scope == 'all':
        if is_global:
            chat.chat_data.global_chat_memory.clear()
            pdm.set_global_user_memories(user_id, {}) if user_id else None
        else:
            preset.chat_memory.clear()
            preset.user_memories.clear()
        return {'msg': f"已清除 {preset.preset_key} 的全部记忆 (￣▽￣)-ok!", 'is_progress': True}
    else:
        return {'msg': "用法: rg mem clear <group|user|all>\n  group=群记忆  user=你的记忆  all=全部"}

@cmd.register(route='rg/mem/global', params=['action'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    action = param_dict.get('action', '').strip().lower()
    pdm = PersistentDataManager.instance
    chat_data = chat.chat_data
    is_global = chat_data.global_memory_enabled

    # rg mem global (无参数) → 显示状态并切换
    if not action:
        new_state = not is_global
        chat_data.global_memory_enabled = new_state
        if new_state:
            report = pdm.init_global_memory(chat_data.chat_key)
            return {'msg': f"global 记忆已开启\n{report}", 'is_progress': True}
        else:
            return {'msg': "global 记忆已关闭，恢复使用按人格隔离的记忆。", 'is_progress': True}

    # rg mem global on
    if action == 'on':
        if is_global:
            return {'msg': "global 记忆已经是开启状态。"}
        chat_data.global_memory_enabled = True
        report = pdm.init_global_memory(chat_data.chat_key)
        return {'msg': f"global 记忆已开启\n{report}", 'is_progress': True}

    # rg mem global off
    if action == 'off':
        if not is_global:
            return {'msg': "global 记忆已经是关闭状态。"}
        chat_data.global_memory_enabled = False
        return {'msg': "global 记忆已关闭，恢复使用按人格隔离的记忆。", 'is_progress': True}

    return {'msg': "用法: rg mem global [on|off]\n  无参数=切换  on=开启  off=关闭"}

@cmd.register(route='rg/on')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if option_dict.get('global'):
        ChatManager.instance.toggle_chat_for_all(enabled=True)
        return {'msg': f"启用所有会话 (￣▽￣)-ok!"}
    elif option_dict.get('target'):
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if target_chat:
            target_chat.toggle_chat(enabled=True)
            return {'msg': f"启用会话: {target_chat_key} (￣▽￣)-ok!"}
        else:
            return {'error': f"找不到会话: {target_chat_key}"}
    else:
        chat.toggle_chat(enabled=True)
        return {'msg': f"启用当前会话 (￣▽￣)-ok!"}

@cmd.register(route='rg/off')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if option_dict.get('global'):
        ChatManager.instance.toggle_chat_for_all(enabled=False)
        return {'msg': f"禁用所有会话 (￣▽￣)-ok!"}
    elif option_dict.get('target'):
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if target_chat:
            target_chat.toggle_chat(enabled=False)
            return {'msg': f"禁用会话: {target_chat_key} (￣▽￣)-ok!"}
        else:
            return {'error': f"找不到会话: {target_chat_key}"}
    else:
        chat.toggle_chat(enabled=False)
        return {'msg': f"禁用当前会话 (￣▽￣)-ok!"}

@cmd.register(route='rg/lock')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if option_dict.get('global'):
        ChatManager.instance.toggle_auto_switch_for_all(enabled=False)
        return {'msg': f"锁定所有会话人格 (￣▽￣)-ok!"}
    elif option_dict.get('target'):
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if target_chat:
            target_chat.toggle_auto_switch(enabled=False)
            return {'msg': f"锁定会话人格: {target_chat_key} (￣▽￣)-ok!"}
        else:
            return {'error': f"找不到会话: {target_chat_key}"}
    else:
        chat.toggle_auto_switch(enabled=False)
        return {'msg': f"锁定当前会话人格 (￣▽￣)-ok!"}

@cmd.register(route='rg/unlock')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if option_dict.get('global'):
        ChatManager.instance.toggle_auto_switch_for_all(enabled=True)
        return {'msg': f"解锁所有会话 (￣▽￣)-ok!"}
    elif option_dict.get('target'):
        target_chat_key = option_dict.get('target')
        target_chat = ChatManager.instance.get_chat(chat_key=target_chat_key)
        if target_chat:
            target_chat.toggle_auto_switch(enabled=True)
            return {'msg': f"解锁会话: {target_chat_key} (￣▽￣)-ok!"}
        else:
            return {'error': f"找不到会话: {target_chat_key}"}
    else:
        chat.toggle_auto_switch(enabled=True)
        return {'msg': f"解锁当前会话 (￣▽￣)-ok!"}

@cmd.register(route='rg/chats')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    chat_info:str = ''
    for c in ChatManager.instance.get_all_chats():
        chat_info += f"+ {c.generate_description(not option_dict.get('show'))}"
    return {'msg': f"当前已加载的会话:\n{chat_info}"}

@cmd.register(route='rg/reload_config')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    reload_config()
    PersistentDataManager.instance.load_from_file()
    return {'msg': f"配置文件重载成功! ver:{config.VERSION}"}

@cmd.register(route='rg/draw', params=['mode'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    mode = param_dict.get('mode', '').strip()
    valid_modes = ('force', 'on', 'auto', 'off')

    # 无参数：显示当前模式
    if not mode:
        current = anima_generate.get_chat_mode(chat.chat_key)
        return {'msg': f"当前画图模式: {current}\n用法:\n  rg draw <force|on|auto|off>  切换画图模式\n  rg draw <json字符串>  根据 JSON 创建绘图任务\n  rg draw-XXXXXX  查询绘图提示词"}

    # 检查是否是 JSON 字符串（以 { 开头）
    if mode.startswith('{'):
        return _create_draw_task_from_json(mode, chat)

    # 模式切换
    mode = mode.lower()
    if mode not in valid_modes:
        return {'msg': f"无效模式: {mode}\n用法: rg draw <force|on|auto|off>"}

    if mode == 'off':
        anima_generate.set_chat_mode(chat.chat_key, 'off')
        # 若没有任何会话需要工具，则卸载
        if not anima_generate.any_chat_enabled():
            disable_anima_tool()
            if config.COMFYUI_ENABLED:
                config.COMFYUI_ENABLED = False
                save_config()
        return {'msg': "Anima 画图已关闭 (￣▽￣)-ok!"}

    # force/on/auto 都需要工具可用
    # 1) health check
    ok, err = anima_generate.health_check_sync()
    if not ok:
        return {'msg': f"Anima 画图服务离线，无法开启: {err}"}

    # 2) 加载 schema + knowledge
    ok, err = anima_generate.fetch_schema_and_knowledge_sync()
    if not ok:
        return {'msg': f"加载 Anima 规范失败: {err}"}

    # 3) 注册工具（全局注册一次即可）
    if enable_anima_tool():
        logger.info(f"[会话: {chat.chat_key}] Anima 画图工具已注册")

    # 4) 设置当前会话模式
    anima_generate.set_chat_mode(chat.chat_key, mode)

    # 5) 持久化开启状态
    if not config.COMFYUI_ENABLED:
        config.COMFYUI_ENABLED = True
        save_config()

    mode_desc = {'force': '常驻+拦截', 'on': '常驻', 'auto': '按需注入'}
    return {'msg': f"Anima 画图已设为 {mode} 模式（{mode_desc[mode]}）(￣▽￣)-ok!"}


def _create_draw_task_from_json(json_str: str, chat: Chat) -> dict:
    """从 JSON 字符串创建绘图任务"""
    # 解析 JSON
    try:
        args = json.loads(json_str)
    except json.JSONDecodeError as e:
        return {'msg': f"JSON 解析失败: {e}\n请检查 JSON 格式是否正确"}

    if not isinstance(args, dict):
        return {'msg': "JSON 必须是一个对象（字典格式）"}

    # 提示词模板字段（允许空值）
    prompt_fields = [
        'quality_meta_year_safe', 'aspect_ratio', 'count', 'character', 'series',
        'artist', 'style', 'appearance', 'tags', 'environment', 'nltags', 'neg',
        'steps', 'cfg'
    ]

    # 构建标准化的提示词数据（保留所有字段，包括空值）
    prompt_data = {}
    for field in prompt_fields:
        prompt_data[field] = str(args.get(field, '') or '')

    # 生成任务编号
    task_id = anima_generate._generate_task_id()

    # 保存到数据库
    draw_db.save_prompt(task_id, prompt_data)

    # 构建绘图参数（填充默认值）
    draw_args = dict(prompt_data)
    if not draw_args.get('steps'):
        draw_args['steps'] = '35'
    if not draw_args.get('cfg'):
        draw_args['cfg'] = '5'

    # 检查画图服务是否可用
    ok, err = anima_generate.health_check_sync()
    if not ok:
        return {'msg': f"Anima 画图服务离线，无法创建任务: {err}"}

    # 检查队列状态
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as executor:
                future = executor.submit(asyncio.run, anima_generate._check_queue(int(draw_args.get('steps', 35))))
                queue_info = future.result(timeout=15)
        else:
            queue_info = loop.run_until_complete(anima_generate._check_queue(int(draw_args.get('steps', 35))))
    except Exception as e:
        logger.warning(f"队列检查失败: {e}")
        queue_info = None

    if queue_info is not None:
        if queue_info.get("queue_too_long"):
            mins = queue_info.get("estimated_remaining_minutes", "?")
            qlen = queue_info.get("queue_length", 0)
            return {'msg': f"当前画图队列繁忙（{qlen} 个任务，预计等待 {mins} 分钟），请稍后再试。"}
        est_seconds = queue_info.get("estimated_remaining_seconds", 60)
        queue_length = queue_info.get("queue_length", 0)
        est_seconds = est_seconds + queue_length * 90 - 30
        est_minutes = max(1, round(est_seconds / 60))
    else:
        est_seconds = int(60 + (int(draw_args.get('steps', 35)) - 35) * 1.5)
        est_minutes = max(1, round(est_seconds / 60))

    # 提交后台生成任务
    positive_desc = anima_generate._build_positive(draw_args)
    try:
        from nonebot import get_bot
        loop = asyncio.get_event_loop()
        # 构建 send_ctx 以便图片能发送到正确的会话
        try:
            bot = get_bot()
            bot_id = bot.self_id
        except Exception:
            bot_id = None
        chat_type = "group" if chat.chat_key.startswith("group_") else "private"
        group_id = chat.chat_key.split("_")[1] if chat_type == "group" else None
        user_id_val = chat.chat_key.split("_")[1] if chat_type == "private" else None
        send_ctx = {
            "chat_key": chat.chat_key,
            "bot_id": bot_id,
            "group_id": group_id,
            "user_id": user_id_val,
        }
        loop.create_task(anima_generate._do_generate(draw_args, config, send_ctx=send_ctx, task_id=task_id))
    except Exception as e:
        logger.error(f"提交绘图任务失败: {e}")
        return {'msg': f"提交绘图任务失败: {e}"}

    return {
        'msg': f"绘图任务已创建！\n任务编号：{task_id}\n预计生成时间：{est_seconds}秒（约{est_minutes}分钟）\n提示词：{positive_desc[:200]}{'...' if len(positive_desc) > 200 else ''}"
    }

@cmd.register(route='rg/model', params=['profile_name'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    from .openai_func import TextGenerator
    profile_name = param_dict.get('profile_name', '').strip()
    profiles = config.OPENAI_PROFILES

    if not profiles:
        return {'msg': "未配置 OPENAI_PROFILES，无法切换"}

    # 无参数：列出所有 profile 及当前会话的配置
    if not profile_name:
        chat_profile = chat.get_active_profile()
        lines = ["可用配置:"]
        for name, p in profiles.items():
            marker = " ← 当前" if name == chat_profile else ""
            lines.append(f"  {name}: {p.get('model', '?')} / {p.get('model_mini', '?')}{marker}")
        lines.append(f"\n用法: rg model <配置名>")
        return {'msg': '\n'.join(lines)}

    # 切换 profile（按群）
    if profile_name not in profiles:
        return {'msg': f"配置 '{profile_name}' 不存在，可用: {', '.join(profiles.keys())}"}

    profile = profiles[profile_name]
    chat.set_active_profile(profile_name)
    # 立即应用到 TextGenerator
    tg = TextGenerator.instance
    tg.switch_profile(profile_name, profile)
    config.OPENAI_ACTIVE_PROFILE = profile_name
    PersistentDataManager.instance.save_to_file()
    return {'msg': f"已切换到 {profile_name}: {profile.get('model', '?')}"}

@cmd.register(route='rg/nn', params=['nickname'])
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    if not user_id:
        return {'msg': "无法获取当前用户信息 (；′⌒`)"}
    pdm = PersistentDataManager.instance
    nickname = param_dict.get('nickname', '').strip()

    if not nickname:
        # 无参数：显示当前昵称或清除
        current = pdm.get_custom_nickname(user_id)
        if current:
            return {'msg': f"你当前的自定义昵称: {current}\n发送 `rg nn 清除` 可以删除自定义昵称。"}
        else:
            return {'msg': "你还没有设置自定义昵称。\n用法: rg nn <昵称> 设置在 bot 中的固定昵称。"}

    if nickname in ('清除', 'clear', 'reset', '删除', 'del'):
        pdm.set_custom_nickname(user_id, "")
        PersistentDataManager.instance.save_to_file(must_save=True)
        return {'msg': "已清除自定义昵称，将使用群名片。", 'is_progress': True}

    if len(nickname) > 30:
        return {'msg': "昵称最长 30 个字符 (；′⌒`)"}

    pdm.set_custom_nickname(user_id, nickname)
    PersistentDataManager.instance.save_to_file(must_save=True)
    return {'msg': f"已将你的昵称设为: {nickname}", 'is_progress': True}

@cmd.register(route='rg/help')
def _(option_dict, param_dict, chat:Chat, chat_presets_dict:dict, user_id:str=''):
    from .llm_tool_plugins import TOOL_REGISTRY
    from .llm_tool_plugins.anima_generate import is_chat_enabled as is_anima_chat_enabled

    # 构建工具列表
    tools = list(TOOL_REGISTRY.keys())
    # 检查 Anima 画图状态
    anima_status = "已启用" if is_anima_chat_enabled(chat.chat_key) else "未启用"
    # 构建工具显示文本（一段式）
    tools_text = "、".join(tools) if tools else "无"

    help_text = f"""兔酱人格指令帮助

【基础指令】
  rg / rg list        显示人格列表
  rg set <预设名>      切换人格
  rg on / rg off       启用/禁用当前会话
  rg reset             重置会话上下文
  rg help              显示本帮助

【记忆管理】
  rg mem               查看当前记忆
  rg mem clear <scope> 清除记忆 (group/user/all)
  rg mem global [on|off] 开关全局记忆

【画图相关】
  rg draw              查看画图模式
  rg draw <mode>       切换画图模式 (force/on/auto/off)
  rg draw <json>       根据JSON创建绘图任务
  rg draw-XXXXXX       查询绘图提示词

【其他】
  rg model [配置名]    查看/切换LLM配置
  rg nn <昵称>         设置自定义昵称
  rg nn 清除           清除自定义昵称

【选项】
  -global              全局设置 (仅超管)
  -target <会话>       指定会话操作

【当前会话工具】Anima画图: {anima_status} | 已注册: {tools_text}"""
    return {'msg': help_text}

# 提交指令注册
cmd.submit_commands()

# if __name__ == '__main__':
#     print(cmd.execute(
#         command='rg new -target group_123456 test test_intro 123',
#         chat=None,
#         chat_presets_dict={},
#     ))

