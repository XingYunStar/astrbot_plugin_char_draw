import asyncio
import json
from datetime import datetime
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


@register(
    "astrbot_plugin_last_reply_logger",
    "星陨",
    "获取上次对话回复并在控制台打印",
    "1.0.4",
    "https://github.com/XingYunStar/astrbot_plugin_last_reply_logger"
)
class LastReplyLogger(Star):
    """记录机器人最后回复消息的插件"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        
        # 存储最后一条回复消息（完整版）
        self.last_reply = {
            "message_id": None,
            "content": None,
            "content_full": None,      # 完整内容
            "session_id": None,
            "session_type": None,
            "timestamp": None,
            "sender_name": None,
            "raw_data": None           # 原始数据
        }
        
        logger.info("上次回复记录插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将记录机器人发送的最后一条消息")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def _extract_message_content(self, event: AstrMessageEvent) -> tuple:
        """提取消息内容，返回 (显示内容, 完整内容)"""
        try:
            result = event.get_result()
            if result and result.chain:
                content_parts = []
                for segment in result.chain:
                    if hasattr(segment, 'text') and segment.text:
                        content_parts.append(segment.text)
                    elif hasattr(segment, 'type'):
                        content_parts.append(f"[{segment.type}]")
                
                full_content = "".join(content_parts) if content_parts else "[无文本内容]"
                
                # 生成显示内容（前500字符）
                if len(full_content) > 500:
                    display_content = full_content[:500] + f"\n... (共 {len(full_content)} 字符，已截断)"
                else:
                    display_content = full_content
                
                return display_content, full_content
            
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
                        full_content = "".join(content_parts) if content_parts else "[无文本内容]"
                        display_content = full_content[:500] + ("..." if len(full_content) > 500 else "")
                        return display_content, full_content
            
            return "[无文本内容]", "[无文本内容]"
        except Exception as e:
            logger.debug(f"提取消息内容失败: {e}")
            return "[解析失败]", "[解析失败]"
    
    def _get_message_id(self, event: AstrMessageEvent) -> Optional[str]:
        """获取消息ID（多种方式尝试）"""
        try:
            # 方法1：直接获取
            if hasattr(event, 'get_message_id'):
                return event.get_message_id()
            
            # 方法2：从 message_obj 获取
            if hasattr(event, 'message_obj') and event.message_obj:
                if hasattr(event.message_obj, 'message_id'):
                    return event.message_obj.message_id
                if hasattr(event.message_obj, 'id'):
                    return event.message_obj.id
            
            # 方法3：从 raw_message 获取
            raw_message = getattr(event.message_obj, 'raw_message', None)
            if raw_message and isinstance(raw_message, dict):
                if 'message_id' in raw_message:
                    return raw_message['message_id']
                if 'msg_id' in raw_message:
                    return raw_message['msg_id']
            
            return None
        except Exception:
            return None
    
    def _get_raw_data(self, event: AstrMessageEvent) -> Optional[dict]:
        """获取原始数据"""
        try:
            if hasattr(event, 'message_obj') and event.message_obj:
                if hasattr(event.message_obj, 'raw_message'):
                    return event.message_obj.raw_message
                if hasattr(event.message_obj, '__dict__'):
                    return {k: str(v) for k, v in event.message_obj.__dict__.items() if not k.startswith('_')}
            return None
        except Exception:
            return None
    
    def _print_reply_info(self, content_display: str, content_full: str, session_info: dict, message_id: str):
        """打印回复信息到控制台"""
        log_lines = [
            "=" * 70,
            f"📨 [回复记录] {session_info.get('type', '未知')} | 会话ID: {session_info.get('id', '未知')}",
            f"👤 发送者: {session_info.get('sender_name', '未知')}",
            f"🕐 时间: {session_info.get('timestamp', '未知')}",
            f"🔢 消息ID: {message_id if message_id else '未获取到'}",
            f"📏 内容长度: {len(content_full)} 字符",
            f"📝 内容预览 (前500字符):",
            "-" * 70,
            content_display,
            "-" * 70,
            f"💾 完整内容已保存，可通过 /get_last_reply full 查看完整内容",
            "=" * 70
        ]
        
        logger.info("\n".join(log_lines))
    
    @filter.on_decorating_result(priority=1)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人发送的消息"""
        try:
            # 提取消息内容
            content_display, content_full = self._extract_message_content(event)
            
            # 获取消息ID
            message_id = self._get_message_id(event)
            
            # 获取会话信息
            group_id = event.get_group_id()
            session_id = group_id if group_id else event.get_sender_id()
            session_type = "群聊" if group_id else "私聊"
            sender_name = event.get_sender_name()
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取原始数据
            raw_data = self._get_raw_data(event)
            
            # 构建会话信息
            session_info = {
                "id": session_id,
                "type": session_type,
                "sender_name": sender_name,
                "timestamp": timestamp
            }
            
            # 更新最后回复记录
            self.last_reply = {
                "message_id": message_id,
                "content": content_display,
                "content_full": content_full,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": timestamp,
                "sender_name": sender_name,
                "raw_data": raw_data
            }
            
            # 根据配置决定是否打印到控制台
            if self.get_config("enable_log", True):
                if group_id and not self.get_config("include_group", True):
                    return
                if not group_id and not self.get_config("include_private", True):
                    return
                
                self._print_reply_info(content_display, content_full, session_info, message_id)
            
        except Exception as e:
            logger.error(f"记录回复消息时发生错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
    
    @filter.command("get_last_reply")
    async def get_last_reply_command(self, event: AstrMessageEvent):
        """获取最后一条回复消息
        
        使用方法:
        /get_last_reply - 查看最后一条回复的摘要
        /get_last_reply full - 查看完整内容
        /get_last_reply raw - 查看原始数据（JSON格式）
        """
        # 解析参数
        args = event.message_str.strip().split()
        mode = args[1].lower() if len(args) > 1 else "summary"
        
        if self.last_reply["content"] is None:
            yield event.plain_result("📭 暂无回复记录，等待机器人发送消息后重试。")
            return
        
        if mode == "full":
            # 显示完整内容
            result = (
                f"📋 **最后一条回复消息（完整版）**\n\n"
                f"📱 会话类型: {self.last_reply['session_type']}\n"
                f"🆔 会话ID: {self.last_reply['session_id']}\n"
                f"👤 发送者: {self.last_reply['sender_name']}\n"
                f"🕐 发送时间: {self.last_reply['timestamp']}\n"
                f"🔢 消息ID: {self.last_reply['message_id'] if self.last_reply['message_id'] else '未获取到'}\n"
                f"📏 内容长度: {len(self.last_reply['content_full'])} 字符\n\n"
                f"📝 完整内容:\n```\n{self.last_reply['content_full']}\n```"
            )
        elif mode == "raw":
            # 显示原始数据
            raw_json = json.dumps(self.last_reply.get("raw_data", {}), ensure_ascii=False, indent=2)[:1900]
            result = (
                f"📋 **最后一条回复消息（原始数据）**\n\n"
                f"📱 会话类型: {self.last_reply['session_type']}\n"
                f"🆔 会话ID: {self.last_reply['session_id']}\n\n"
                f"```json\n{raw_json}\n```\n\n"
                f"💡 提示: 完整原始数据已输出到控制台日志"
            )
            # 同时输出到日志
            logger.info(f"原始数据: {json.dumps(self.last_reply.get('raw_data', {}), ensure_ascii=False, indent=2)}")
        else:
            # 默认显示摘要
            content = self.last_reply["content"]
            result = (
                f"📋 **最后一条回复消息**\n\n"
                f"📱 会话类型: {self.last_reply['session_type']}\n"
                f"🆔 会话ID: {self.last_reply['session_id']}\n"
                f"👤 发送者: {self.last_reply['sender_name']}\n"
                f"🕐 发送时间: {self.last_reply['timestamp']}\n"
                f"🔢 消息ID: {self.last_reply['message_id'] if self.last_reply['message_id'] else '未获取到'}\n"
                f"📏 内容长度: {len(self.last_reply['content_full'])} 字符\n\n"
                f"📝 消息内容:\n```\n{content}\n```\n\n"
                f"💡 提示: 发送 `/get_last_reply full` 查看完整内容\n"
                f"💡 提示: 发送 `/get_last_reply raw` 查看原始数据"
            )
        
        yield event.plain_result(result)
    
    @filter.command("clear_last_reply")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def clear_last_reply_command(self, event: AstrMessageEvent):
        """清除最后一条回复记录（仅管理员）"""
        self.last_reply = {
            "message_id": None,
            "content": None,
            "content_full": None,
            "session_id": None,
            "session_type": None,
            "timestamp": None,
            "sender_name": None,
            "raw_data": None
        }
        
        if self.get_config("enable_log", True):
            logger.info("最后回复记录已被清除")
        
        yield event.plain_result("✅ 最后回复记录已清除。")
    
    @filter.command("reply_logger_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看插件状态"""
        status_text = (
            f"📊 **回复记录插件状态**\n\n"
            f"✅ 插件状态: 运行中\n"
            f"📝 最后回复: {'已记录' if self.last_reply['content'] else '无记录'}\n"
            f"📏 最后回复长度: {len(self.last_reply.get('content_full', '')) if self.last_reply['content'] else 0} 字符\n"
            f"🔧 当前配置:\n"
            f"  - 日志输出: {'开启' if self.get_config('enable_log', True) else '关闭'}\n"
            f"  - 完整消息: {'开启' if self.get_config('print_full_message', True) else '关闭'}\n"
            f"  - 记录私聊: {'开启' if self.get_config('include_private', True) else '关闭'}\n"
            f"  - 记录群聊: {'开启' if self.get_config('include_group', True) else '关闭'}\n\n"
            f"💡 命令:\n"
            f"  - /get_last_reply: 查看摘要\n"
            f"  - /get_last_reply full: 查看完整内容\n"
            f"  - /get_last_reply raw: 查看原始数据\n"
            f"  - /clear_last_reply: 清除记录\n"
            f"  - /reply_logger_status: 查看状态"
        )
        yield event.plain_result(status_text)
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.get_config("enable_log", True):
            logger.info("上次回复记录插件已卸载")
