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

repeater_group = config.get('repeater_group', ['149378291','726905061'])
shortest_length = config.get('repeater_min_message_length', 1)
shortest_times = config.get('repeater_min_message_times', 3)
blacklist = config.get('repeater_blacklist', [])
whitelist = config.get('repeater_whitelist', ['149378291'])
repeat_probability = config.get('repeater_probability', 0.05)
