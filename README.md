# 群管助手

## 功能
1. **禁发消息**：群内消息命中关键词时自动拦截（撤回 + 警告/禁言/移出）
2. **定时消息**：按设定时间自动向指定群发送消息
3. 每个功能均可用指令单独开关

## 指令一览

| 指令 | 说明 |
|---|---|
| `/gmg on` / `/gmg off` | 总开关 |
| `/gmg status` | 查看整体状态 |
| `/gmg ban on` / `/gmg ban off` | 禁发功能开关 |
| `/gmg ban list` | 查看禁发关键词 |
| `/gmg ban add 关键词` | 添加禁发关键词 |
| `/gmg ban del 关键词` | 删除禁发关键词 |
| `/gmg timer on` / `/gmg timer off` | 定时消息开关 |
| `/gmg timer list` | 查看定时任务 |
| `/gmg timer add HH:MM 消息内容` | 添加定时消息（在当前群生效） |
| `/gmg timer del ID` | 删除定时消息 |

## 配置项
- `master_enable`：插件总开关
- `ban_enable`：禁发功能开关
- `ban_keywords`：禁发关键词列表
- `ban_action`：触发操作（warn/mute/kick）
- `ban_warn_text`：警告文案
- `ban_mute_duration`：禁言时长（分钟）
- `timer_enable`：定时消息开关
- `timer_tasks`：定时任务列表

## 使用步骤
1. 插件列表找到本插件 → 点击【配置】可手动设置
2. 也可直接在群内用 `/gmg` 系列指令管理
