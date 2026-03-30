import asyncio
from typing import Optional
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


@register(
    "astrbot_plugin_last_reply_logger",
    "星陨",
    "获取上次对话回复并在控制台打印",
    "1.0.3",
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
            "timestamp": None,
            "sender_name": None
        }
        
        logger.info("上次回复记录插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将记录机器人发送的最后一条消息")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def _extract_message_content(self, event: AstrMessageEvent) -> str:
        """提取消息内容"""
        try:
            # 获取回复结果
            result = event.get_result()
            if result and result.chain:
                content_parts = []
                for segment in result.chain:
                    if hasattr(segment, 'text') and segment.text:
                        content_parts.append(segment.text)
                    elif hasattr(segment, 'type'):
                        content_parts.append(f"[{segment.type}]")
                if content_parts:
                    return "".join(content_parts)
            
            # 尝试从 message_obj 获取
            if hasattr(event, 'message_obj') and event.message_obj:
                if hasattr(event.message_obj, 'message'):
                    message = event.message_obj.message
                    if message:
                        content_parts = []
                        for seg in message:
                            if hasattr(seg, 'data') and seg.data:
                                if 'text' in seg.data:
                                    content_parts.append(seg.data['text'])
                        if content_parts:
                            return "".join(content_parts)
            
            return "[无文本内容]"
        except Exception as e:
            logger.debug(f"提取消息内容失败: {e}")
            return "[解析失败]"
    
    def _print_reply_info(self, content: str, session_info: dict):
        """打印回复信息到控制台"""
        log_lines = [
            "=" * 60,
            f"📨 [回复记录] {session_info.get('type', '未知')} | 会话ID: {session_info.get('id', '未知')}",
            f"👤 发送者: {session_info.get('sender_name', '未知')}",
            f"🕐 时间: {session_info.get('timestamp', '未知')}",
            f"📝 内容:"
        ]
        
        if self.get_config("print_full_message", True):
            log_lines.append(content)
        else:
            summary = content[:100] + ("..." if len(content) > 100 else "")
            log_lines.append(summary)
        
        log_lines.append("=" * 60)
        
        logger.info("\n".join(log_lines))
    
    @filter.on_decorating_result(priority=1)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人发送的消息"""
        try:
            # 获取消息内容
            content = self._extract_message_content(event)
            
            # 获取会话信息
            group_id = event.get_group_id()
            session_id = group_id if group_id else event.get_sender_id()
            session_type = "群聊" if group_id else "私聊"
            
            # 获取发送者名称
            sender_name = event.get_sender_name()
            
            # 获取时间戳
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 构建会话信息
            session_info = {
                "id": session_id,
                "type": session_type,
                "sender_name": sender_name,
                "timestamp": timestamp
            }
            
            # 更新最后回复记录
            self.last_reply = {
                "message_id": getattr(event, 'get_message_id', lambda: None)(),
                "content": content,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": timestamp,
                "sender_name": sender_name
            }
            
            # 根据配置决定是否打印到控制台
            if self.get_config("enable_log", True):
                # 根据会话类型过滤
                if group_id and not self.get_config("include_group", True):
                    return
                if not group_id and not self.get_config("include_private", True):
                    return
                
                self._print_reply_info(content, session_info)
            
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
        
        content = self.last_reply["content"]
        
        # 截断显示
        if len(content) > 200:
            content_display = content[:200] + "..."
        else:
            content_display = content
        
        result = (
            f"📋 **最后一条回复消息**\n\n"
            f"📱 会话类型: {self.last_reply['session_type']}\n"
            f"🆔 会话ID: {self.last_reply['session_id']}\n"
            f"👤 发送者: {self.last_reply['sender_name']}\n"
            f"🕐 发送时间: {self.last_reply['timestamp']}\n"
            f"🔢 消息ID: {self.last_reply['message_id']}\n\n"
            f"📝 消息内容:\n```\n{content_display}\n```\n\n"
            f"💡 提示: 完整内容已输出到控制台日志"
        )
        
        yield event.plain_result(result)
    
    @filter.command("clear_last_reply")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_last_reply_command(self, event: AstrMessageEvent):
        """清除最后一条回复记录（仅管理员）
        
        使用方法:
        /clear_last_reply - 清除存储的最后一条回复记录
        """
        self.last_reply = {
            "message_id": None,
            "content": None,
            "session_id": None,
            "session_type": None,
            "timestamp": None,
            "sender_name": None
        }
        
        if self.get_config("enable_log", True):
            logger.info("最后回复记录已被清除")
        
        yield event.plain_result("✅ 最后回复记录已清除。")
    
    @filter.command("reply_logger_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看插件状态
        
        使用方法:
        /reply_logger_status - 查看插件运行状态和配置
        """
        status_text = (
            f"📊 **回复记录插件状态**\n\n"
            f"✅ 插件状态: 运行中\n"
            f"📝 最后回复: {'已记录' if self.last_reply['content'] else '无记录'}\n"
            f"🔧 当前配置:\n"
            f"  - 日志输出: {'开启' if self.get_config('enable_log', True) else '关闭'}\n"
            f"  - 完整消息: {'开启' if self.get_config('print_full_message', True) else '关闭'}\n"
            f"  - 记录私聊: {'开启' if self.get_config('include_private', True) else '关闭'}\n"
            f"  - 记录群聊: {'开启' if self.get_config('include_group', True) else '关闭'}\n\n"
            f"💡 命令:\n"
            f"  - /get_last_reply: 查看最后回复\n"
            f"  - /clear_last_reply: 清除记录\n"
            f"  - /reply_logger_status: 查看状态"
        )
        yield event.plain_result(status_text)
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.get_config("enable_log", True):
            logger.info("上次回复记录插件已卸载")
