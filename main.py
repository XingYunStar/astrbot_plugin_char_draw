import asyncio
import json
import random
from datetime import datetime
from typing import Optional, Dict

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
from astrbot.core.message.components import Plain, Image
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


@register(
    "astrbot_plugin_ai_draw",
    "星陨",
    "根据对话内容自动生成AI绘画",
    "1.0.0",
    "https://github.com/XingYunStar/astrbot_plugin_ai_draw"
)
class AIDrawPlugin(Star):
    """AI绘画插件：根据对话回复生成绘画"""
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        
        # 存储每个会话的最后一条消息
        self.session_messages: Dict[str, Dict] = {}
        
        logger.info("AI绘画插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将记录对话并支持 AI 绘画")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """获取会话唯一标识"""
        group_id = event.get_group_id()
        if group_id:
            return f"group_{group_id}"
        else:
            return f"private_{event.get_sender_id()}"
    
    # ==================== 绘画提示词系统提示 ====================
    
    def get_draw_system_prompt(self) -> str:
        """获取绘画提示词生成系统提示（从配置读取）"""
        custom_prompt = self.get_config("draw_system_prompt", "")
        if custom_prompt and custom_prompt.strip():
            return custom_prompt.strip()
        
        return """请将我给出的文字转换为可用于AI绘画的正面提示词。

要求：
1. 动漫风格，质量极好且细节丰富
2. 整体的tag串尽量为正面
3. 若提供的转换文字不是正面，则帮我改成正面
4. 若提供的转换文字元素过少，则帮我添加丰富的tag
5. 人物tag全部使用1girl
6. 人物模型tag为: firefly (honkai star rail)
7. 不能出现多个人物

请回复英文tag串，不需要其他多余内容。

参考格式: firefly (honkai star rail), 1girl, best quality, masterpiece, highly detailed, illustration"""
    
    def get_simple_draw_prompt(self) -> str:
        """获取简化模式系统提示（从配置读取）"""
        custom_simple = self.get_config("simple_system_prompt", "")
        if custom_simple and custom_simple.strip():
            return custom_simple.strip()
        
        return """将用户输入转换为英文AI绘画提示词，动漫风格，包含 firefly (honkai star rail), 1girl, best quality, masterpiece。
只输出英文tag串，不要其他内容。"""
    
    # ==================== LLM 调用 ====================
    
    async def optimize_prompt_with_llm(self, user_message: str, bot_reply: str) -> Optional[str]:
        """使用当前配置的 LLM 优化绘画提示词"""
        try:
            provider = self.context.get_curr_provider()
            if not provider:
                logger.error("无法获取 LLM Provider")
                return None
            
            use_simple = self.get_config("use_simple_prompt", False)
            system_prompt = self.get_simple_draw_prompt() if use_simple else self.get_draw_system_prompt()
            
            if use_simple:
                user_content = f"将以下内容转换为英文绘画提示词：\n{user_message}\n{bot_reply}"
            else:
                user_content = f"""用户说：【{user_message}】

机器人说：【{bot_reply}】"""
            
            request = ProviderRequest(
                prompt=system_prompt,
                messages=[{"role": "user", "content": user_content}]
            )
            
            if self.get_config("enable_log", True):
                logger.info("正在调用 LLM 优化绘画提示词...")
            
            response = await provider.text_chat(request)
            
            if response and response.choices and len(response.choices) > 0:
                prompt = response.choices[0].message.content.strip()
                # 移除可能的 markdown 代码块标记
                if prompt.startswith("```"):
                    lines = prompt.split("\n")
                    prompt = "\n".join(lines[1:-1]) if len(lines) > 2 else prompt
                prompt = prompt.replace("```", "").strip()
                
                if self.get_config("enable_log", True):
                    logger.info(f"生成绘画提示词长度: {len(prompt)}")
                return prompt
            return None
                
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None
    
    # ==================== 绘画 API 调用 ====================
    
    async def generate_drawing(self, prompt: str) -> Optional[str]:
        """调用绘画 API 生成图片，返回 base64"""
        api_url = self.get_config("draw_api_url", "")
        if not api_url:
            logger.warning("绘画 API URL 未配置")
            return None
        
        # 随机选择图片尺寸
        if random.choice([True, False]):
            width, height = 896, 1296
        else:
            width, height = 1296, 896
        
        negative_prompt = self.get_config("negative_prompt", "")
        if not negative_prompt:
            negative_prompt = "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry, ugly"
        
        request_body = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": self.get_config("draw_steps", 42),
            "cfg_scale": self.get_config("draw_cfg_scale", 8),
            "sampler_name": self.get_config("draw_sampler", "DPM++ SDE Karras"),
            "enable_hr": False
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers={"Content-Type": "application/json"},
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    if response.status != 200:
                        logger.error(f"绘画 API 错误: {response.status}")
                        return None
                    
                    result = await response.json()
                    if result.get("images") and len(result["images"]) > 0:
                        return result["images"][0]
                    return None
        except Exception as e:
            logger.error(f"绘画 API 调用失败: {e}")
            return None
    
    # ==================== 主动绘画命令 ====================
    
    @filter.command("draw")
    async def draw_command(self, event: AstrMessageEvent):
        """手动触发绘画
        
        使用方法:
        /draw - 使用上次对话内容生成绘画
        /draw <描述> - 根据描述生成绘画
        """
        user_input = event.message_str.strip()
        if user_input.startswith("/draw"):
            user_input = user_input[5:].strip()
        
        session_key = self._get_session_key(event)
        session_data = self.session_messages.get(session_key, {})
        
        # 如果没有输入描述，使用上次对话
        if not user_input:
            user_msg = session_data.get("user_message", "")
            bot_reply = session_data.get("bot_reply", "")
            
            if not user_msg or not bot_reply:
                yield event.plain_result(
                    "📭 没有找到可用的对话记录。\n\n"
                    "使用方法：\n"
                    "1. 先与机器人对话，然后发送 /draw\n"
                    "2. 或直接发送：/draw 一只猫在阳光下睡觉"
                )
                return
            
            yield event.plain_result("🎨 正在根据上次对话生成绘画，请稍候...")
            
            prompt = await self.optimize_prompt_with_llm(user_msg, bot_reply)
            if not prompt:
                yield event.plain_result("❌ 绘画提示词生成失败，请稍后重试")
                return
            
            image_base64 = await self.generate_drawing(prompt)
            if image_base64:
                yield event.plain_result("🎨 根据对话生成的绘画：")
                yield event.image_result(f"base64://{image_base64}")
            else:
                yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
        else:
            yield event.plain_result("🎨 正在生成绘画，请稍候...")
            
            use_simple = self.get_config("use_simple_prompt", False)
            if use_simple:
                prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
            else:
                prompt = await self.optimize_prompt_with_llm(user_input, "")
                if not prompt:
                    prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
            
            image_base64 = await self.generate_drawing(prompt)
            if image_base64:
                yield event.plain_result("🎨 绘画生成成功")
                yield event.image_result(f"base64://{image_base64}")
            else:
                yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
    
    # ==================== 监听机器人回复 ====================
    
    @filter.on_decorating_result(priority=10)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人即将发送的消息，记录对话"""
        try:
            # 获取用户消息
            user_message = event.message_str
            if not user_message:
                return
            
            # 获取机器人即将发送的回复
            result = event.get_result()
            if not result or not result.chain:
                return
            
            # 提取机器人回复内容
            bot_reply = ""
            for segment in result.chain:
                if hasattr(segment, 'text') and segment.text:
                    bot_reply += segment.text
            
            if not bot_reply:
                return
            
            session_key = self._get_session_key(event)
            
            # 保存对话记录
            self.session_messages[session_key] = {
                "user_message": user_message,
                "bot_reply": bot_reply,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            if self.get_config("enable_log", True):
                logger.debug(f"已记录对话: {session_key}")
            
            # 自动绘画
            auto_draw = self.get_config("auto_draw", False)
            if auto_draw:
                asyncio.create_task(self._auto_draw_async(event, user_message, bot_reply, session_key))
                
        except Exception as e:
            logger.error(f"记录对话失败: {e}")
    
    async def _auto_draw_async(self, event: AstrMessageEvent, user_msg: str, bot_reply: str, session_key: str):
        """异步自动生成绘画"""
        try:
            prompt = await self.optimize_prompt_with_llm(user_msg, bot_reply)
            if not prompt:
                return
            
            image_base64 = await self.generate_drawing(prompt)
            if image_base64:
                # 使用 event.send() 发送消息（不能在 on_decorating_result 中用 yield）
                await event.send(event.plain_result("🎨 根据对话自动生成的绘画："))
                await event.send(event.image_result(f"base64://{image_base64}"))
                logger.info("自动绘画生成成功")
        except Exception as e:
            logger.error(f"自动绘画失败: {e}")
    
    # ==================== 状态查询命令 ====================
    
    @filter.command("draw_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看绘画插件状态"""
        session_key = self._get_session_key(event)
        session_data = self.session_messages.get(session_key, {})
        
        auto_draw = self.get_config("auto_draw", False)
        use_simple = self.get_config("use_simple_prompt", False)
        api_url = self.get_config("draw_api_url", "")
        
        status_text = (
            f"🎨 **AI绘画插件状态**\n\n"
            f"✅ 插件状态: 运行中\n"
            f"🖼️ 绘画API: {'✅ 已配置' if api_url else '❌ 未配置'}\n"
            f"🎨 自动绘画: {'✅ 开启' if auto_draw else '❌ 关闭'}\n"
            f"📝 提示词模式: {'简化模式' if use_simple else '完整模式'}\n\n"
            f"📊 当前会话:\n"
            f"  - 最后用户消息: {session_data.get('user_message', '无')[:50] if session_data.get('user_message') else '无'}\n"
            f"  - 最后机器人回复: {session_data.get('bot_reply', '无')[:50] if session_data.get('bot_reply') else '无'}\n\n"
            f"💡 命令:\n"
            f"  - /draw: 根据上次对话生成绘画\n"
            f"  - /draw <描述>: 根据描述生成绘画\n"
            f"  - /draw_status: 查看状态"
        )
        yield event.plain_result(status_text)
    
    @filter.command("draw_help")
    async def help_command(self, event: AstrMessageEvent):
        """查看绘画插件帮助"""
        help_text = (
            "🎨 **AI绘画插件使用帮助**\n\n"
            "📋 **基础命令:**\n"
            "  • `/draw` - 根据上次对话内容生成绘画\n"
            "  • `/draw <描述>` - 根据文字描述生成绘画\n"
            "  • `/draw_status` - 查看插件状态\n"
            "  • `/draw_help` - 显示此帮助\n\n"
            "⚙️ **配置说明:**\n"
            "  • `auto_draw` - 是否自动生成绘画\n"
            "  • `draw_api_url` - 绘画 API 地址\n"
            "  • `draw_steps` - 绘画步数\n"
            "  • `draw_cfg_scale` - 提示词相关性\n"
            "  • `draw_system_prompt` - 自定义提示词规则\n\n"
            "💡 **示例:**\n"
            "  • 先与机器人对话，然后发送 `/draw`\n"
            "  • 直接发送 `/draw 流萤在花海中微笑`"
        )
        yield event.plain_result(help_text)
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.get_config("enable_log", True):
            logger.info("AI绘画插件已卸载")
