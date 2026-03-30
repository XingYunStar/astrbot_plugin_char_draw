import asyncio
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


@register(
    "astrbot_plugin_last_reply_logger",
    "星陨",
    "获取上次对话回复并在控制台打印",
    "1.0.0",
    "https://github.com/XingYunStar/astrbot_plugin_last_reply_logger"
)
class LastReplyLogger(Star):
    """记录机器人最后回复消息的插件"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = self._validate_config(config)
        # 存储最后一条回复消息
        self.last_reply = {
            "message_id": None,
            "content": None,
            "session_id": None,
            "timestamp": None
        }
        
        logger.info("上次回复记录插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将记录机器人发送的最后一条消息")
    
    def _validate_config(self, config: dict) -> dict:
        """验证并规范化配置"""
        normalized = config.copy()
        
        # 确保 enable_log 是布尔值
        if "enable_log" in normalized:
            val = normalized["enable_log"]
            if isinstance(val, str):
                normalized["enable_log"] = val.lower() in ("true", "yes", "1", "on")
            elif not isinstance(val, bool):
                normalized["enable_log"] = bool(val)
        else:
            normalized["enable_log"] = True
        
        # 确保 print_full_message 是布尔值
        if "print_full_message" in normalized:
            val = normalized["print_full_message"]
            if isinstance(val, str):
                normalized["print_full_message"] = val.lower() in ("true", "yes", "1", "on")
            elif not isinstance(val, bool):
                normalized["print_full_message"] = bool(val)
        else:
            normalized["print_full_message"] = True
        
        # 确保 include_private 是布尔值
        if "include_private" in normalized:
            val = normalized["include_private"]
            if isinstance(val, str):
                normalized["include_private"] = val.lower() in ("true", "yes", "1", "on")
            elif not isinstance(val, bool):
                normalized["include_private"] = bool(val)
        else:
            normalized["include_private"] = True
        
        # 确保 include_group 是布尔值
        if "include_group" in normalized:
            val = normalized["include_group"]
            if isinstance(val, str):
                normalized["include_group"] = val.lower() in ("true", "yes", "1", "on")
            elif not isinstance(val, bool):
                normalized["include_group"] = bool(val)
        else:
            normalized["include_group"] = True
        
        return normalized
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def _should_record(self, event: AstrMessageEvent) -> bool:
        """判断是否应该记录此消息"""
        # 检查消息是否由机器人发送
        if not event.is_from_self():
            return False
        
        # 检查会话类型
        if event.get_group_id():
            # 群聊
            if not self.get_config("include_group", True):
                return False
        else:
            # 私聊
            if not self.get_config("include_private", True):
                return False
        
        return True
    
    def _format_message_content(self, event: AstrMessageEvent) -> str:
        """格式化消息内容为字符串"""
        try:
            # 获取消息链
            result = event.get_result()
            if not result or not result.chain:
                return "[空消息]"
            
            # 提取文本内容
            content_parts = []
            for segment in result.chain:
                if hasattr(segment, 'text') and segment.text:
                    content_parts.append(segment.text)
                elif hasattr(segment, 'type'):
                    # 其他类型消息（图片、语音等）
                    content_parts.append(f"[{segment.type}]")
            
            return "".join(content_parts) if content_parts else "[无文本内容]"
        except Exception as e:
            logger.debug(f"格式化消息内容失败: {e}")
            return "[解析失败]"
    
    def _print_reply_info(self, event: AstrMessageEvent, content: str):
        """打印回复信息到控制台"""
        session_type = "群聊" if event.get_group_id() else "私聊"
        session_id = event.get_group_id() or event.get_sender_id()
        
        # 构建日志信息
        log_lines = [
            "=" * 60,
            f"📨 [回复记录] {session_type} | 会话ID: {session_id}",
            f"🕐 时间: {event.get_time()}",
            f"📝 内容:"
        ]
        
        if self.get_config("print_full_message", True):
            # 打印完整内容，支持多行
            log_lines.append(content)
        else:
            # 只打印摘要（前100字符）
            summary = content[:100] + ("..." if len(content) > 100 else "")
            log_lines.append(summary)
        
        log_lines.append("=" * 60)
        
        # 输出到日志
        logger.info("\n".join(log_lines))
    
    @filter.on_decorating_result(priority=10)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人发送的消息"""
        try:
            # 判断是否应该记录
            if not self._should_record(event):
                return
            
            # 获取消息内容
            content = self._format_message_content(event)
            
            # 获取会话信息
            session_id = event.get_group_id() or event.get_sender_id()
            session_type = "group" if event.get_group_id() else "private"
            
            # 更新最后回复记录
            self.last_reply = {
                "message_id": event.get_message_id(),
                "content": content,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": event.get_time(),
                "raw_event": event
            }
            
            # 打印到控制台
            if self.get_config("enable_log", True):
                self._print_reply_info(event, content)
            
        except Exception as e:
            logger.error(f"记录回复消息时发生错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    @filter.command("get_last_reply")
    async def get_last_reply_command(self, event: AstrMessageEvent):
        """获取最后一条回复消息
        
        使用方法:
        /get_last_reply - 查看最后一条回复的详细信息
        """
        if self.last_reply["content"] is None:
            yield event.plain_result("📭 暂无回复记录，等待机器人发送消息后重试。")
            return
        
        # 格式化输出
        session_type = "群聊" if self.last_reply["session_type"] == "group" else "私聊"
        content = self.last_reply["content"]
        
        # 如果内容过长，截断显示
        if len(content) > 200:
            content_display = content[:200] + "..."
        else:
            content_display = content
        
        result = (
            f"📋 **最后一条回复消息**\n\n"
            f"📱 会话类型: {session_type}\n"
            f"🆔 会话ID: {self.last_reply['session_id']}\n"
            f"🕐 发送时间: {self.last_reply['timestamp']}\n"
            f"🔢 消息ID: {self.last_reply['message_id']}\n\n"
            f"📝 消息内容:\n```\n{content_display}\n```\n\n"
            f"💡 提示: 完整内容已输出到控制台日志"
        )
        
        yield event.plain_result(result)
    
    @filter.command("clear_last_reply")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_last_reply_command(self, event: AstrMessageEvent):
        """清除最后一条回复记录
        
        使用方法:
        /clear_last_reply - 清除存储的最后一条回复记录
        """
        self.last_reply = {
            "message_id": None,
            "content": None,
            "session_id": None,
            "timestamp": None
        }
        
        if self.get_config("enable_log", True):
            logger.info("最后回复记录已被清除")
        
        yield event.plain_result("✅ 最后回复记录已清除。")
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.get_config("enable_log", True):
            logger.info("上次回复记录插件已卸载")
