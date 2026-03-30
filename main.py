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
    "1.0.2",
    "https://github.com/XingYunStar/astrbot_plugin_last_reply_logger"
)
class LastReplyLogger(Star):
    """记录机器人最后回复消息的插件"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        
        # 存储最后一条回复消息
        self.last_reply = {
            "message_id": None,
            "content": None,
            "session_id": None,
            "session_type": None,
            "timestamp": None
        }
        
        logger.info("上次回复记录插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将记录机器人发送的最后一条消息")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def _is_from_self(self, event: AstrMessageEvent) -> bool:
        """判断消息是否来自机器人自己"""
        try:
            # 获取自身ID
            self_id = event.get_self_id()
            
            # 方法1：通过 raw_message 中的 user_id 判断
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if raw_message and isinstance(raw_message, dict):
                user_id = raw_message.get("user_id")
                if user_id and str(user_id) == str(self_id):
                    return True
            
            # 方法2：通过 sender 信息判断
            sender = getattr(event.message_obj, 'sender', None)
            if sender and isinstance(sender, dict):
                sender_id = sender.get("user_id")
                if sender_id and str(sender_id) == str(self_id):
                    return True
            
            # 方法3：通过消息对象的 self_id 属性
            if hasattr(event.message_obj, 'self_id'):
                if str(event.message_obj.self_id) == str(self_id):
                    return True
            
            # 方法4：通过 event 的直接属性
            if hasattr(event, 'user_id'):
                if str(event.user_id) == str(self_id):
                    return True
            
            return False
        except Exception as e:
            logger.debug(f"判断消息来源失败: {e}")
            return False
    
    def _should_record(self, event: AstrMessageEvent) -> bool:
        """判断是否应该记录此消息"""
        # 检查消息是否由机器人发送
        if not self._is_from_self(event):
            return False
        
        # 检查会话类型
        if event.get_group_id():
            if not self.get_config("include_group", True):
                return False
        else:
            if not self.get_config("include_private", True):
                return False
        
        return True
    
    def _get_message_content(self, event: AstrMessageEvent) -> str:
        """获取消息内容"""
        try:
            # 方法1：从 message_obj 获取
            if hasattr(event, 'message_obj') and event.message_obj:
                # 尝试获取消息链
                if hasattr(event.message_obj, 'message'):
                    message = event.message_obj.message
                    if message:
                        content_parts = []
                        for segment in message:
                            if hasattr(segment, 'data'):
                                data = segment.data
                                if isinstance(data, dict):
                                    if 'text' in data:
                                        content_parts.append(data['text'])
                                    elif 'file' in data:
                                        content_parts.append(f"[文件: {data.get('file', 'unknown')}]")
                            elif hasattr(segment, 'text'):
                                content_parts.append(segment.text)
                        if content_parts:
                            return "".join(content_parts)
            
            # 方法2：从 raw_message 获取
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if raw_message and isinstance(raw_message, dict):
                # 尝试获取 message 字段
                if 'message' in raw_message:
                    msg = raw_message['message']
                    if isinstance(msg, str):
                        return msg
                    elif isinstance(msg, list):
                        content_parts = []
                        for seg in msg:
                            if isinstance(seg, dict) and 'data' in seg:
                                data = seg.get('data', {})
                                if 'text' in data:
                                    content_parts.append(data['text'])
                        if content_parts:
                            return "".join(content_parts)
            
            # 方法3：从 event 的 message_str 获取
            if hasattr(event, 'message_str') and event.message_str:
                return event.message_str
            
            return "[无法解析消息内容]"
        except Exception as e:
            logger.debug(f"获取消息内容失败: {e}")
            return "[解析失败]"
    
    def _print_reply_info(self, event: AstrMessageEvent, content: str):
        """打印回复信息到控制台"""
        session_type = "群聊" if event.get_group_id() else "私聊"
        session_id = event.get_group_id() or event.get_sender_id()
        
        # 获取时间
        timestamp = getattr(event, 'get_time', lambda: "未知")()
        
        # 构建日志信息
        log_lines = [
            "=" * 60,
            f"📨 [回复记录] {session_type} | 会话ID: {session_id}",
            f"🕐 时间: {timestamp}",
            f"📝 内容:"
        ]
        
        if self.get_config("print_full_message", True):
            log_lines.append(content)
        else:
            summary = content[:100] + ("..." if len(content) > 100 else "")
            log_lines.append(summary)
        
        log_lines.append("=" * 60)
        
        logger.info("\n".join(log_lines))
    
    # 方式1：监听消息事件（推荐）
    @filter.event(AstrMessageEvent)
    async def on_message(self, event: AstrMessageEvent):
        """监听所有消息事件"""
        try:
            if not self._should_record(event):
                return
            
            content = self._get_message_content(event)
            session_id = event.get_group_id() or event.get_sender_id()
            session_type = "group" if event.get_group_id() else "private"
            timestamp = getattr(event, 'get_time', lambda: "未知")()
            message_id = getattr(event, 'get_message_id', lambda: None)()
            
            self.last_reply = {
                "message_id": message_id,
                "content": content,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": timestamp
            }
            
            if self.get_config("enable_log", True):
                self._print_reply_info(event, content)
                
        except Exception as e:
            logger.error(f"记录回复消息时发生错误: {e}")
    
    # 方式2：监听消息发送结果（备用）
    @filter.on_decorating_result(priority=10)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人发送的消息（备用方式）"""
        try:
            # 检查是否是回复消息
            result = event.get_result()
            if not result or not result.chain:
                return
            
            # 获取消息内容
            content = self._get_message_content(event)
            if not content or content == "[无法解析消息内容]":
                # 尝试从 result 中获取
                content_parts = []
                for segment in result.chain:
                    if hasattr(segment, 'text') and segment.text:
                        content_parts.append(segment.text)
                if content_parts:
                    content = "".join(content_parts)
            
            if not content or content == "[无法解析消息内容]":
                return
            
            session_id = event.get_group_id() or event.get_sender_id()
            session_type = "group" if event.get_group_id() else "private"
            timestamp = getattr(event, 'get_time', lambda: "未知")()
            message_id = getattr(event, 'get_message_id', lambda: None)()
            
            self.last_reply = {
                "message_id": message_id,
                "content": content,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": timestamp
            }
            
            if self.get_config("enable_log", True):
                self._print_reply_info(event, content)
                
        except Exception as e:
            logger.debug(f"备用方式记录失败: {e}")
    
    @filter.command("get_last_reply")
    async def get_last_reply_command(self, event: AstrMessageEvent):
        """获取最后一条回复消息"""
        if self.last_reply["content"] is None:
            yield event.plain_result("📭 暂无回复记录，等待机器人发送消息后重试。")
            return
        
        session_type = "群聊" if self.last_reply["session_type"] == "group" else "私聊"
        content = self.last_reply["content"]
        
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
        """清除最后一条回复记录（仅管理员）"""
        self.last_reply = {
            "message_id": None,
            "content": None,
            "session_id": None,
            "session_type": None,
            "timestamp": None
        }
        
        if self.get_config("enable_log", True):
            logger.info("最后回复记录已被清除")
        
        yield event.plain_result("✅ 最后回复记录已清除。")
    
    async def terminate(self):
        if self.get_config("enable_log", True):
            logger.info("上次回复记录插件已卸载")
