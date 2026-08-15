"""
群管助手｜v1.6.0
- 禁发关键词消息拦截（撤回 + 警告/禁言/移出，操作结果准确反馈成功或失败）
- warn模式增强：短时间窗口内连续违规自动禁言 / 当日累计违规自动踢出
- 分层警告文案：前3次违规各有独立文案，第4次起共用文案，均可单独配置
- 每个用户违规次数累计统计（可查看/清空，记录时间戳用于高频检测）
- 定时消息推送（支持一次性/每日循环/间隔循环，支持私信指定群号）
- 定时任务删除后重建 ID 自动重排
- 每个功能均可用指令单独开关
"""
import asyncio
import re
from datetime import datetime, timedelta
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.event.filter import EventMessageType
from astrbot.api.star import Context, Star
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, At


class GroupManager(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = config
        self.master_enable: bool = self.cfg.get("master_enable", True)
        self.ban_enable: bool = self.cfg.get("ban_enable", True)
        self.timer_enable: bool = self.cfg.get("timer_enable", True)
        self.ban_keywords: list = self.cfg.get("ban_keywords", [])
        self.timer_tasks: list = self._parse_timer_tasks(self.cfg.get("timer_tasks", []))
        self._violation_counts: dict = {}
        self._fired_keys: set = set()
        self._cached_bot = None
        self._timer_task: asyncio.Task = None
        self._start_timer_loop()

        logger.info(
            "[群管助手] ✅ 加载完成 | 总开关=%s | 禁发=%s | 定时=%s | 关键词数=%d | 定时任务数=%d",
            self.master_enable, self.ban_enable, self.timer_enable,
            len(self.ban_keywords), len(self.timer_tasks)
        )

    # ==================== 工具方法 ====================

    def _parse_timer_tasks(self, raw_list: list) -> list:
        """把配置中的字符串列表解析为结构化任务列表"""
        tasks = []
        for idx, item in enumerate(raw_list):
            if isinstance(item, dict):
                if "repeat" not in item:
                    item["repeat"] = "daily"
                tasks.append(item)
                continue
            parts = str(item).split("|", 3)
            if len(parts) == 4:
                repeat_str = parts[2].strip()
                task = {
                    "id": idx + 1,
                    "time": parts[0].strip(),
                    "group": parts[1].strip(),
                    "repeat": "daily",
                    "message": parts[3].strip()
                }
                if repeat_str == "once":
                    task["repeat"] = "once"
                elif repeat_str == "daily":
                    task["repeat"] = "daily"
                elif repeat_str.startswith("interval:"):
                    task["repeat"] = "interval"
                    try:
                        task["interval"] = int(repeat_str.split(":")[1])
                    except (ValueError, IndexError):
                        task["interval"] = 60
                    task["last_fired"] = datetime.now().isoformat()
                tasks.append(task)
            elif len(parts) == 3:
                tasks.append({
                    "id": idx + 1,
                    "time": parts[0].strip(),
                    "group": parts[1].strip(),
                    "repeat": "daily",
                    "message": parts[2].strip()
                })
        return tasks

    def _save_timer_tasks_to_cfg(self):
        """把结构化任务列表写回配置格式"""
        result = []
        for t in self.timer_tasks:
            repeat = t.get("repeat", "daily")
            if repeat == "interval":
                repeat_str = f"interval:{t.get('interval', 60)}"
            else:
                repeat_str = repeat
            result.append(f"{t['time']}|{t['group']}|{repeat_str}|{t['message']}")
        self.cfg["timer_tasks"] = result

    def _start_timer_loop(self):
        try:
            loop = asyncio.get_event_loop()
            self._timer_task = loop.create_task(self._timer_loop())
        except RuntimeError:
            logger.warning("[群管助手] ⚠️ 无法获取事件循环，定时任务将在下次事件时重试启动")

    async def _timer_loop(self):
        """每30秒检查一次是否到了定时消息的发送时间"""
        while True:
            try:
                await asyncio.sleep(30)
                if not self.master_enable or not self.timer_enable:
                    continue
                now = datetime.now()
                current_time = now.strftime("%H:%M")
                to_delete = []
                interval_fired = False
                for task in self.timer_tasks:
                    should_fire = False
                    repeat = task.get("repeat", "daily")

                    if repeat == "interval":
                        # 间隔循环模式：检查距上次发送是否已过指定间隔
                        interval_min = task.get("interval", 60)
                        last_fired_str = task.get("last_fired")
                        if last_fired_str:
                            try:
                                last_fired = datetime.fromisoformat(last_fired_str)
                                if (now - last_fired).total_seconds() >= interval_min * 60:
                                    should_fire = True
                            except Exception:
                                should_fire = True
                        else:
                            should_fire = True
                    else:
                        # 一次性 / 每日模式：按时间点匹配
                        if task["time"] != current_time:
                            continue
                        fk = f"{current_time}_{task['id']}"
                        if fk in self._fired_keys:
                            continue
                        self._fired_keys.add(fk)
                        should_fire = True

                    if not should_fire:
                        continue

                    try:
                        await self._send_group_message(task["group"], task["message"])
                        logger.info("[群管助手] ⏰ 定时消息已发送 → 群%s: %s", task["group"], task["message"][:50])
                        if repeat == "once":
                            to_delete.append(task)
                        elif repeat == "interval":
                            task["last_fired"] = now.isoformat()
                            interval_fired = True
                    except Exception as e:
                        logger.error("[群管助手] ❌ 定时消息发送失败: %s", e)
                        # 间隔任务发送失败也更新时间，避免连续重试刷屏
                        if repeat == "interval":
                            task["last_fired"] = now.isoformat()
                            interval_fired = True

                if to_delete:
                    for t in to_delete:
                        self.timer_tasks.remove(t)
                        logger.info("[群管助手] 🗑️ 一次性任务 ID:%s 已完成并删除", t["id"])
                    self._save_cfg()

                # 间隔任务的 last_fired 有更新时保存配置
                if interval_fired:
                    self._save_timer_tasks_to_cfg()

                # 清理过期的 fired_keys（用 datetime 比较，避免跨天问题）
                cutoff = now - timedelta(minutes=5)
                self._fired_keys = {
                    k for k in self._fired_keys
                    if not self._parse_fired_key_time(k, now) < cutoff
                }

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[群管助手] ❌ 定时循环异常: %s", e)

    def _parse_fired_key_time(self, key: str, now: datetime) -> datetime:
        """把 fired_key 中的 HH:MM 解析为 datetime（自动处理跨天）"""
        try:
            hh, mm = key.split("_")[0].split(":")
            dt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if dt > now:
                dt -= timedelta(days=1)
            return dt
        except Exception:
            return now - timedelta(hours=1)

    def _ensure_bot(self, ev=None):
        """获取可用的平台客户端：优先缓存 → 事件 → 上下文"""
        if self._cached_bot is not None:
            return self._cached_bot
        if ev is not None:
            try:
                bot = getattr(ev, "bot", None)
                if bot is not None:
                    self._cached_bot = bot
                    logger.info("[群管助手] ✅ 已缓存平台客户端（来自事件）")
                    return self._cached_bot
            except Exception:
                pass
        try:
            pm = getattr(self.context, "platform_manager", None)
            if pm is not None:
                for platform in getattr(pm, "platform_instances", []):
                    bot = getattr(platform, "bot", None)
                    if bot is not None:
                        self._cached_bot = bot
                        logger.info("[群管助手] ✅ 已缓存平台客户端（来自上下文）")
                        return self._cached_bot
        except Exception as e:
            logger.warning("[群管助手] ⚠️ 从上下文获取平台客户端失败: %s", e)
        return None

    async def _send_group_message(self, group_id: str, message: str):
        """向指定群发送消息（通过 OneBot API 直接调用）"""
        bot = self._ensure_bot()
        if bot is None:
            logger.error("[群管助手] ❌ 平台客户端未就绪，请先发送任意消息触发缓存")
            return
        chain = MessageChain([Plain(message)])
        try:
            gid = int(group_id)
        except (ValueError, TypeError):
            logger.error("[群管助手] ❌ 群号无效: %s", group_id)
            return
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
            messages = await AiocqhttpMessageEvent._parse_onebot_json(chain)
            if messages:
                await bot.call_action(
                    "send_group_msg",
                    group_id=gid,
                    message=messages,
                )
                logger.info("[群管助手] ✅ 定时消息已发送 → 群%s", gid)
        except Exception as e:
            logger.error("[群管助手] ❌ 发送群消息失败: %s，尝试降级发送", e)
            try:
                await bot.call_action(
                    "send_group_msg",
                    group_id=gid,
                    message=[{"type": "text", "data": {"text": message}}],
                )
                logger.info("[群管助手] ✅ 定时消息已发送(降级) → 群%s", gid)
            except Exception as e2:
                logger.error("[群管助手] ❌ 降级发送也失败: %s", e2)

    def _get_group_id(self, ev: AstrMessageEvent) -> str:
        try:
            gid = ev.get_group_id()
            return str(gid) if gid else ""
        except Exception:
            return ""

    def _is_group_message(self, ev: AstrMessageEvent) -> bool:
        return bool(self._get_group_id(ev))

    def _get_tiered_warn_text(self, violation_count: int) -> str:
        """根据违规次数选取分层警告文案，留空回退到通用文案"""
        fallback = self.cfg.get("ban_warn_text", "⚠️ 你发送的内容包含违禁词，请遵守群规！")
        if violation_count == 1:
            return self.cfg.get("ban_warn_text_1", "") or fallback
        elif violation_count == 2:
            return self.cfg.get("ban_warn_text_2", "") or fallback
        elif violation_count == 3:
            return self.cfg.get("ban_warn_text_3", "") or fallback
        else:
            return self.cfg.get("ban_warn_text_4plus", "") or fallback

    def _save_cfg(self):
        self.cfg["master_enable"] = self.master_enable
        self.cfg["ban_enable"] = self.ban_enable
        self.cfg["timer_enable"] = self.timer_enable
        self.cfg["ban_keywords"] = self.ban_keywords
        self.cfg["ban_warn_auto_mute_threshold"] = self.cfg.get("ban_warn_auto_mute_threshold", 3)
        self.cfg["ban_warn_auto_mute_window"] = self.cfg.get("ban_warn_auto_mute_window", 60)
        self.cfg["ban_warn_auto_kick_threshold"] = self.cfg.get("ban_warn_auto_kick_threshold", 10)
        self._save_timer_tasks_to_cfg()

    async def _send_warn(self, ev: AstrMessageEvent, text: str, sender_id: str = ""):
        """发送警告消息，可选@违规用户"""
        try:
            at_user = self.cfg.get("ban_warn_at_user", True)
            if at_user and sender_id:
                try:
                    uid = int(sender_id)
                    await ev.send(MessageChain([At(qq=uid), Plain(" " + text)]))
                except (ValueError, TypeError):
                    await ev.send(MessageChain([Plain(text)]))
            else:
                await ev.send(MessageChain([Plain(text)]))
        except Exception as e:
            logger.warning("[群管助手] ⚠️ 发送提示消息失败: %s", e)

    # ==================== 消息拦截 ====================

    @filter.event_message_type(EventMessageType.ALL)
    async def on_group_message(self, ev: AstrMessageEvent):
        """监听群消息，检查是否包含禁发关键词"""
        if not self.master_enable or not self.ban_enable:
            return
        if not self.ban_keywords:
            return
        if not self._is_group_message(ev):
            return

        # 缓存平台客户端引用，供定时消息使用
        self._ensure_bot(ev)

        msg_text = ev.message_str.strip()
        if not msg_text:
            return

        # 检查是否命中任何关键词
        hit_keyword = None
        for kw in self.ban_keywords:
            if kw and kw.lower() in msg_text.lower():
                hit_keyword = kw
                break

        if not hit_keyword:
            return

        logger.info("[群管助手] 🚫 命中禁发关键词「%s」 | 群: %s | 消息: %s", hit_keyword, self._get_group_id(ev), msg_text[:80])

        group_id = self._get_group_id(ev)
        sender_id = ev.get_sender_id()
        message_id = getattr(ev.message_obj, "message_id", "")

        action = self.cfg.get("ban_action", "warn")

        # 累计违规次数（按天记录，跨天清零）+ 记录违规时间戳用于短时高频检测
        sid = str(sender_id)
        now_dt = datetime.now()
        today = now_dt.strftime("%Y-%m-%d")
        entry = self._violation_counts.get(sid)
        if not isinstance(entry, dict) or entry.get("date") != today:
            entry = {"date": today, "count": 1, "timestamps": [now_dt]}
            self._violation_counts[sid] = entry
        else:
            entry["count"] += 1
            entry.setdefault("timestamps", []).append(now_dt)
            # 清理5分钟以前的时间戳，避免列表无限增长
            cutoff_ts = now_dt - timedelta(minutes=5)
            entry["timestamps"] = [t for t in entry["timestamps"] if t > cutoff_ts]
        violation_count = self._violation_counts[sid]["count"]

        # 先撤回消息（所有操作通用）
        try:
            await ev.bot.call_action("delete_msg", message_id=int(message_id))
            logger.info("[群管助手] ✅ 消息已撤回")
        except Exception as e:
            logger.warning("[群管助手] ⚠️ 撤回失败: %s", e)

        if action == "mute":
            mute_min = self.cfg.get("ban_mute_duration", 10)
            tiered_text = self._get_tiered_warn_text(violation_count)
            mute_success = False
            try:
                await ev.bot.call_action(
                    "set_group_ban",
                    group_id=int(group_id),
                    user_id=int(sender_id),
                    duration=mute_min * 60,
                )
                mute_success = True
                logger.info("[群管助手] ✅ 已禁言用户 %s %d分钟", sender_id, mute_min)
            except Exception as e:
                logger.warning("[群管助手] ⚠️ 禁言失败: %s", e)
            if mute_success:
                await self._send_warn(ev, f"{tiered_text}\n✅ 已禁言 {mute_min} 分钟。\n📊 今日第 {violation_count} 次违规", sender_id)
            else:
                await self._send_warn(ev, f"{tiered_text}\n❌ 禁言失败。\n📊 今日第 {violation_count} 次违规", sender_id)

        elif action == "kick":
            kick_success = False
            tiered_text = self._get_tiered_warn_text(violation_count)
            try:
                await ev.bot.call_action(
                    "set_group_kick",
                    group_id=int(group_id),
                    user_id=int(sender_id),
                    reject_add_request=False,
                )
                kick_success = True
                logger.info("[群管助手] ✅ 已移出用户 %s", sender_id)
            except Exception as e:
                logger.warning("[群管助手] ⚠️ 移出失败: %s", e)
            if kick_success:
                await self._send_warn(ev, f"{tiered_text}\n✅ 已将该成员移出群聊。\n📊 今日第 {violation_count} 次违规", sender_id)
            else:
                await self._send_warn(ev, f"{tiered_text}\n❌ 移出失败。\n📊 今日第 {violation_count} 次违规", sender_id)

        else:
            # warn 模式：仅警告，但检测短时高频违规自动禁言 / 累计违规自动踢出
            auto_mute_threshold = self.cfg.get("ban_warn_auto_mute_threshold", 3)
            auto_mute_window = self.cfg.get("ban_warn_auto_mute_window", 60)
            auto_kick_threshold = self.cfg.get("ban_warn_auto_kick_threshold", 10)

            kicked = False
            muted = False
            tiered_text = self._get_tiered_warn_text(violation_count)

            # 检查是否触发自动踢出（当日累计达到阈值）
            if auto_kick_threshold > 0 and violation_count >= auto_kick_threshold:
                try:
                    await ev.bot.call_action(
                        "set_group_kick",
                        group_id=int(group_id),
                        user_id=int(sender_id),
                        reject_add_request=False,
                    )
                    kicked = True
                    logger.info("[群管助手] ✅ warn模式自动踢出用户 %s（今日累计%d次违规）", sender_id, violation_count)
                except Exception as e:
                    logger.warning("[群管助手] ⚠️ 自动踢出失败: %s", e)
                if kicked:
                    await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规\n🚫 累计违规达 {auto_kick_threshold} 次，已被移出群聊！", sender_id)
                else:
                    await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规\n❌ 自动踢出失败", sender_id)

            # 检查是否触发自动禁言（短时间窗口内达到阈值）
            elif auto_mute_threshold > 0:
                window_start = now_dt - timedelta(seconds=auto_mute_window)
                recent_violations = [t for t in entry.get("timestamps", []) if t > window_start]
                if len(recent_violations) >= auto_mute_threshold:
                    mute_min = self.cfg.get("ban_mute_duration", 10)
                    try:
                        await ev.bot.call_action(
                            "set_group_ban",
                            group_id=int(group_id),
                            user_id=int(sender_id),
                            duration=mute_min * 60,
                        )
                        muted = True
                        logger.info("[群管助手] ✅ warn模式自动禁言用户 %s %d分钟（%d秒内%d次违规）",
                                    sender_id, mute_min, auto_mute_window, len(recent_violations))
                    except Exception as e:
                        logger.warning("[群管助手] ⚠️ 自动禁言失败: %s", e)
                    if muted:
                        await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规\n🔇 {auto_mute_window}秒内连续违规 {auto_mute_threshold} 次，已禁言 {mute_min} 分钟！", sender_id)
                    else:
                        await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规\n❌ 自动禁言失败", sender_id)
                else:
                    await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规", sender_id)
            else:
                await self._send_warn(ev, f"{tiered_text}\n📊 今日第 {violation_count} 次违规", sender_id)

        # 阻止事件继续传播
        ev.stop_event()

    # ==================== 指令处理 ====================

    @filter.command("gmg")
    async def cmd_group_manager(self, ev: AstrMessageEvent):
        """群管助手主指令"""
        # 确保缓存bot（私信也能用）
        self._ensure_bot(ev)

        raw = ev.message_str.strip()
        parts = raw.split()
        args = parts[1:] if len(parts) > 1 else []

        if not args:
            yield ev.plain_result(self._help_text())
            return

        sub = args[0].lower()

        # ---- 总开关 ----
        if sub == "on":
            self.master_enable = True
            self._save_cfg()
            yield ev.plain_result("✅ 群管助手已开启")
            return

        if sub == "off":
            self.master_enable = False
            self._save_cfg()
            yield ev.plain_result("❌ 群管助手已关闭")
            return

        if sub == "status":
            yield ev.plain_result(self._status_text())
            return

        # ---- 禁发功能 ----
        if sub == "ban":
            if len(args) < 2:
                yield ev.plain_result("用法: /gmg ban on|off|list|add|del")
                return
            ban_sub = args[1].lower()
            if ban_sub == "on":
                self.ban_enable = True
                self._save_cfg()
                yield ev.plain_result("✅ 禁发功能已开启")
                return
            if ban_sub == "off":
                self.ban_enable = False
                self._save_cfg()
                yield ev.plain_result("❌ 禁发功能已关闭")
                return
            if ban_sub == "list":
                if not self.ban_keywords:
                    yield ev.plain_result("📋 禁发关键词列表为空")
                else:
                    lines = ["📋 禁发关键词列表："]
                    for i, kw in enumerate(self.ban_keywords, 1):
                        lines.append(f"  {i}. {kw}")
                    yield ev.plain_result("\n".join(lines))
                return
            if ban_sub == "add":
                if len(args) < 3:
                    yield ev.plain_result("用法: /gmg ban add 关键词")
                    return
                keyword = " ".join(args[2:])
                if keyword in self.ban_keywords:
                    yield ev.plain_result(f"⚠️ 关键词「{keyword}」已存在")
                    return
                self.ban_keywords.append(keyword)
                self._save_cfg()
                yield ev.plain_result(f"✅ 已添加禁发关键词「{keyword}」\n当前共 {len(self.ban_keywords)} 个关键词")
                return
            if ban_sub == "del":
                if len(args) < 3:
                    yield ev.plain_result("用法: /gmg ban del 关键词")
                    return
                keyword = " ".join(args[2:])
                if keyword not in self.ban_keywords:
                    yield ev.plain_result(f"⚠️ 关键词「{keyword}」不在列表中")
                    return
                self.ban_keywords.remove(keyword)
                self._save_cfg()
                yield ev.plain_result(f"✅ 已删除禁发关键词「{keyword}」\n当前共 {len(self.ban_keywords)} 个关键词")
                return
            if ban_sub == "violations":
                if not self._violation_counts:
                    yield ev.plain_result("📋 暂无违规记录")
                else:
                    today = datetime.now().strftime("%Y-%m-%d")
                    lines = ["📋 违规记录（今日）："]
                    sorted_vc = sorted(
                        self._violation_counts.items(),
                        key=lambda x: x[1].get("count", 0) if isinstance(x[1], dict) else 0,
                        reverse=True
                    )
                    has_today = False
                    for uid, entry in sorted_vc:
                        if isinstance(entry, dict) and entry.get("date") == today:
                            lines.append(f"  用户 {uid}: {entry.get('count', 0)} 次")
                            has_today = True
                    if not has_today:
                        lines.append("  今日暂无违规")
                    yield ev.plain_result("\n".join(lines))
                return
            if ban_sub == "resetviolations":
                self._violation_counts.clear()
                self._save_cfg()
                yield ev.plain_result("✅ 违规记录已清空")
                return
            yield ev.plain_result("未知子指令。用法: /gmg ban on|off|list|add|del|violations|resetviolations")
            return

        # ---- 定时消息功能 ----
        if sub == "timer":
            if len(args) < 2:
                yield ev.plain_result("用法: /gmg timer on|off|list|add|del")
                return
            timer_sub = args[1].lower()
            if timer_sub == "on":
                self.timer_enable = True
                self._save_cfg()
                yield ev.plain_result("✅ 定时消息功能已开启")
                return
            if timer_sub == "off":
                self.timer_enable = False
                self._save_cfg()
                yield ev.plain_result("❌ 定时消息功能已关闭")
                return
            if timer_sub == "list":
                if not self.timer_tasks:
                    yield ev.plain_result("📋 定时任务列表为空")
                else:
                    lines = ["📋 定时任务列表："]
                    for t in self.timer_tasks:
                        repeat = t.get("repeat", "daily")
                        if repeat == "once":
                            repeat_tag = "⚡一次性"
                        elif repeat == "daily":
                            repeat_tag = "🔄每日"
                        else:
                            repeat_tag = f"🔁每{t.get('interval', 60)}分钟"
                        time_display = t["time"] if repeat != "interval" else f"每{t.get('interval', 60)}分钟"
                        lines.append(f"  ID:{t['id']} | {time_display} | 群:{t['group']} | {repeat_tag} | {t['message'][:30]}")
                    yield ev.plain_result("\n".join(lines))
                return
            if timer_sub == "add":
                if len(args) < 4:
                    yield ev.plain_result(
                        "用法:\n"
                        "  /gmg timer add HH:MM 消息内容\n"
                        "  /gmg timer add HH:MM 消息内容 once|daily\n"
                        "  /gmg timer add HH:MM 群号 消息内容\n"
                        "  /gmg timer add HH:MM 群号 消息内容 once|daily\n"
                        "  /gmg timer add interval:N 群号 消息内容\n"
                        "  once=一次性(发完自动删除) daily=每日循环(默认)\n"
                        "  interval:N=每N分钟循环(如 interval:30)"
                    )
                    return
                time_str = args[2]

                # 判断是否为间隔循环模式
                interval_match = re.match(r"^interval:(\d+)$", time_str, re.IGNORECASE)
                if interval_match:
                    interval_min = int(interval_match.group(1))
                    if interval_min < 1:
                        yield ev.plain_result("⚠️ 间隔时间不能小于1分钟")
                        return
                    repeat = "interval"
                    remaining = args[3:]
                else:
                    if not re.match(r"^\d{1,2}:\d{2}$", time_str):
                        yield ev.plain_result(
                            "⚠️ 时间格式错误，请用 HH:MM 或 interval:N 格式\n"
                            "例如: 08:30 或 interval:30"
                        )
                        return
                    h, m = time_str.split(":")
                    time_str = f"{int(h):02d}:{int(m):02d}"

                    remaining = args[3:]
                    # 判断最后一个参数是否是重复类型
                    repeat = "daily"
                    if remaining and remaining[-1].lower() in ("once", "daily"):
                        repeat = remaining[-1].lower()
                        remaining = remaining[:-1]

                if not remaining:
                    yield ev.plain_result("⚠️ 消息内容不能为空")
                    return

                # 判断是否手动指定了群号（第一个参数为纯数字且后面还有消息内容）
                if remaining[0].isdigit() and len(remaining) > 1:
                    group_id = remaining[0]
                    message_content = " ".join(remaining[1:])
                else:
                    # 没指定群号，从当前事件获取
                    group_id = self._get_group_id(ev)
                    message_content = " ".join(remaining)
                    if not group_id:
                        yield ev.plain_result(
                            "⚠️ 无法获取群号。\n"
                            "请在群内使用此指令，或手动指定群号：\n"
                            "  /gmg timer add HH:MM 群号 消息内容\n"
                            "  /gmg timer add interval:N 群号 消息内容"
                        )
                        return

                # 构建新任务
                new_id = max((t["id"] for t in self.timer_tasks), default=0) + 1
                new_task = {
                    "id": new_id,
                    "time": time_str if repeat != "interval" else f"interval:{interval_min}",
                    "group": str(group_id),
                    "repeat": repeat,
                    "message": message_content
                }
                if repeat == "interval":
                    new_task["interval"] = interval_min
                    new_task["last_fired"] = datetime.now().isoformat()

                self.timer_tasks.append(new_task)
                self._save_cfg()

                if repeat == "once":
                    repeat_text = "⚡一次性(发完自动删除)"
                elif repeat == "daily":
                    repeat_text = "🔄每日循环"
                else:
                    repeat_text = f"🔁每{interval_min}分钟循环"

                time_display = time_str if repeat != "interval" else f"每{interval_min}分钟"

                yield ev.plain_result(
                    f"✅ 已添加定时消息\n"
                    f"  ID: {new_task['id']}\n"
                    f"  时间: {time_display}\n"
                    f"  群号: {group_id}\n"
                    f"  类型: {repeat_text}\n"
                    f"  内容: {message_content}"
                )
                return
            if timer_sub == "del":
                if len(args) < 3:
                    yield ev.plain_result("用法: /gmg timer del ID")
                    return
                try:
                    task_id = int(args[2])
                except ValueError:
                    yield ev.plain_result("⚠️ ID 必须是数字")
                    return
                found = None
                for t in self.timer_tasks:
                    if t["id"] == task_id:
                        found = t
                        break
                if not found:
                    yield ev.plain_result(f"⚠️ 找不到 ID 为 {task_id} 的定时任务")
                    return
                self.timer_tasks.remove(found)
                self._save_cfg()
                yield ev.plain_result(f"✅ 已删除定时任务 ID:{task_id}")
                return
            yield ev.plain_result("未知子指令。用法: /gmg timer on|off|list|add|del")
            return

        yield ev.plain_result(self._help_text())

    # ==================== 文本生成 ====================

    def _help_text(self) -> str:
        return (
            "===== 群管助手 =====\n"
            "/gmg on/off — 总开关\n"
            "/gmg status — 查看状态\n"
            "--- 禁发功能 ---\n"
            "/gmg ban on/off — 开关\n"
            "/gmg ban list — 查看关键词\n"
            "/gmg ban add 关键词 — 添加\n"
            "/gmg ban del 关键词 — 删除\n"
            "/gmg ban violations — 查看违规记录\n"
            "/gmg ban resetviolations — 清空违规记录\n"
            "--- 定时消息 ---\n"
            "/gmg timer on/off — 开关\n"
            "/gmg timer list — 查看任务\n"
            "/gmg timer add HH:MM [群号] 消息 [once|daily] — 添加\n"
            "/gmg timer add interval:N [群号] 消息 — 添加间隔循环\n"
            "  · 群内可省略群号，私信需指定群号\n"
            "  · once=一次性(发完删除) daily=每日循环(默认)\n"
            "  · interval:N=每N分钟循环(如 interval:30)\n"
            "/gmg timer del ID — 删除"
        )

    def _status_text(self) -> str:
        action = self.cfg.get("ban_action", "warn")
        lines = [
            "===== 群管助手状态 =====",
            f"总开关: {'✅ 开' if self.master_enable else '❌ 关'}",
            f"禁发功能: {'✅ 开' if self.ban_enable else '❌ 关'}",
            f"  关键词数: {len(self.ban_keywords)}",
            f"  惩罚操作: {action}",
        ]
        if action == "warn":
            amt = self.cfg.get("ban_warn_auto_mute_threshold", 3)
            amw = self.cfg.get("ban_warn_auto_mute_window", 60)
            akt = self.cfg.get("ban_warn_auto_kick_threshold", 10)
            if amt > 0:
                lines.append(f"  自动禁言: {amt}次/{amw}秒内 → 禁言{self.cfg.get('ban_mute_duration', 10)}分钟")
            if akt > 0:
                lines.append(f"  自动踢出: 当日累计{akt}次")
        lines.append(f"  违规用户数: {len(self._violation_counts)}")
        lines.append(f"定时消息: {'✅ 开' if self.timer_enable else '❌ 关'}")
        lines.append(f"  任务数: {len(self.timer_tasks)}")
        return "\n".join(lines)

    # ==================== 清理 ====================

    def __del__(self):
        if self._timer_task and not self._timer_task.done():
            self._timer_task.cancel()
