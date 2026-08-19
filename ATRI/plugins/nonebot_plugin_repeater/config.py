from nonebot import get_driver, logger

config = get_driver().config.dict()

if 'repeater_group' not in config:
    logger.warning('[复读姬] 未发现配置项 `repeater_group` , 采用默认值: []')
if 'repeater_min_message_length' not in config:
    logger.warning('[复读姬] 未发现配置项 `repeater_min_message_length` , 采用默认值: 1')
if 'repeater_min_message_times' not in config:
    logger.warning('[复读姬] 未发现配置项 `repeater_min_message_times` , 采用默认值: 3')
if 'repeater_blacklist' not in config:
    logger.warning('[复读姬] 未发现配置项 `repeater_blacklist` , 采用默认值: []')

repeater_group = config.get('repeater_group', [])
shortest_length = config.get('repeater_min_message_length', 1)
shortest_times = config.get('repeater_min_message_times', 3)
blacklist = config.get('repeater_blacklist', [])
whitelist = config.get('repeater_whitelist', [])
repeat_probability = config.get('repeater_probability', 0.05)
# 撤回还原功能生效群白名单（只有这里的群会监听消息撤回并 at 撤回者还原原文）
# 默认为空，需在 config.yml 的 Repeater.recall_whitelist 中配置群号才会生效
recall_whitelist = config.get('repeater_recall_whitelist', [])
if not recall_whitelist:
    logger.warning('[复读姬] 撤回还原功能未启用：`repeater_recall_whitelist` 为空，请在 config.yml 的 Repeater.recall_whitelist 中配置群号')
