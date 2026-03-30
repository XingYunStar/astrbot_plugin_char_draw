import asyncio
import json
import os
import random
import base64
from datetime import datetime
from typing import Optional, Dict, List, Tuple

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import ProviderRequest
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
        
        # 存储最后一条回复消息
        self.last_reply = {
            "content": None,
            "session_id": None,
            "session_type": None,
            "timestamp": None,
            "user_message": None
        }
        
        # 存储用户最后一条消息（用于绘画时获取上下文）
        self.last_user_message: Dict[str, Dict] = {}
        
        logger.info("AI绘画插件已加载")
        if self.get_config("enable_log", True):
            logger.info("插件将监听对话并自动生成绘画")
            # 打印提示词配置状态
            if self.get_config("draw_system_prompt"):
                logger.info("已加载自定义绘画提示词系统提示")
            else:
                logger.info("使用默认绘画提示词系统提示")
    
    def get_config(self, key: str, default=None):
        """获取配置值"""
        return self.config.get(key, default)
    
    # ==================== 绘画提示词系统提示（从配置读取） ====================
    
    def get_draw_system_prompt(self) -> str:
        """获取绘画提示词生成系统提示（从配置读取）"""
        # 优先使用用户配置的提示词
        custom_prompt = self.get_config("draw_system_prompt", "")
        if custom_prompt and custom_prompt.strip():
            return custom_prompt.strip()
        
        # 如果没有配置，使用默认提示词
        return """请将我给出的文字转换为可用于AI绘画的正面提示词。

要求：
1. 动漫风格，质量极好且细节丰富
2. 整体的tag串尽量为正面
3. 若提供的转换文字不是正面，则帮我改成正面，并且将视图限制在人物内，不要让人物过小而显出全身
4. 若提供的转换文字元素过少，则帮我添加丰富的tag（如人物表情、穿着、身姿、环境等）
5. 人物tag全部使用1girl，禁止使用其他的
6. 人物模型tag为: firefly (honkai star rail)
7. 使用固定画师tag: artist:torino_aqua, [{artist:azuuru}]
8. 不能出现多个人物，有且仅有 firefly (honkai star rail)
9. 不要出现眼睛颜色的tag

请确保以上要求全部实现，并回复英文tag串，不需要其他多余内容。

参考tag串:
firefly (honkai star rail), artist:torino_aqua, [{artist:azuuru}], 1girl, solo, long_hair, breasts, looking_at_viewer, blush, bangs, skirt, shirt, long_sleeves, navel, holding, very_long_hair, collarbone, swimsuit, white_shirt, heart, bikini, outdoors, frills, parted_lips, open_clothes, sky, choker, day, cloud, water, off_shoulder, stomach, blue_sky, wet, see-through, open_shirt, black_bikini, ocean, black_choker, white_bikini, white_skirt, wading, frilled_bikini, bikini_under_clothes, bikini_skirt, best quality, highly detailed, masterpiece, ultra-detailed, illustration"""
    
    def get_simple_draw_prompt(self) -> str:
        """获取简化的绘画提示词系统提示（从配置读取）"""
        # 优先使用用户配置的简化提示词
        custom_simple = self.get_config("simple_system_prompt", "")
        if custom_simple and custom_simple.strip():
            return custom_simple.strip()
        
        # 如果没有配置，使用默认简化提示词
        return """请将用户输入转换为英文AI绘画提示词，动漫风格，包含 firefly (honkai star rail), 1girl, best quality, masterpiece。
只输出英文tag串，不要其他内容。"""
    
    # ==================== 消息记录 ====================
    
    def _extract_message_content(self, event: AstrMessageEvent) -> str:
        """提取消息内容"""
        try:
            result = event.get_result()
            if result and result.chain:
                content_parts = []
                for segment in result.chain:
                    if hasattr(segment, 'text') and segment.text:
                        content_parts.append(segment.text)
                return "".join(content_parts) if content_parts else ""
            return ""
        except Exception as e:
            logger.debug(f"提取消息内容失败: {e}")
            return ""
    
    def _get_user_message(self, event: AstrMessageEvent) -> str:
        """获取用户原始消息"""
        try:
            if hasattr(event, 'message_str') and event.message_str:
                return event.message_str
            return ""
        except Exception:
            return ""
    
    def _get_session_key(self, event: AstrMessageEvent) -> str:
        """获取会话唯一标识"""
        group_id = event.get_group_id()
        if group_id:
            return f"group_{group_id}"
        else:
            return f"private_{event.get_sender_id()}"
    
    # ==================== LLM 调用（使用当前 Provider） ====================
    
    async def optimize_prompt_with_llm(self, user_message: str, bot_reply: str) -> Optional[str]:
        """使用当前配置的 LLM 优化绘画提示词"""
        try:
            # 获取当前使用的 Provider
            provider = self.context.get_curr_provider()
            if not provider:
                logger.error("无法获取 LLM Provider，请检查 AstrBot LLM 配置")
                return None
            
            # 选择使用完整提示词还是简化版
            use_simple = self.get_config("use_simple_prompt", False)
            system_prompt = self.get_simple_draw_prompt() if use_simple else self.get_draw_system_prompt()
            
            # 构建用户内容
            if use_simple:
                # 简化模式：直接要求转换
                user_content = f"将以下内容转换为英文绘画提示词：\n用户说：{user_message}\n机器人说：{bot_reply}"
            else:
                # 完整模式：使用标准格式
                user_content = f"""进行转换的文字：
我说：【{user_message}】

机器人说：【{bot_reply}】"""
            
            # 构建 Provider 请求
            request = ProviderRequest(
                prompt=system_prompt,
                messages=[
                    {"role": "user", "content": user_content}
                ]
            )
            
            if self.get_config("enable_log", True):
                logger.info(f"正在调用 LLM 优化绘画提示词...")
                logger.debug(f"使用提示词模式: {'简化模式' if use_simple else '完整模式'}")
            
            # 调用 LLM
            response = await provider.text_chat(request)
            
            if response and response.choices and len(response.choices) > 0:
                prompt = response.choices[0].message.content
                # 清理提示词
                prompt = prompt.strip()
                # 移除可能的 markdown 代码块标记
                if prompt.startswith("```"):
                    lines = prompt.split("\n")
                    prompt = "\n".join(lines[1:-1]) if len(lines) > 2 else prompt
                prompt = prompt.replace("```", "").strip()
                
                if self.get_config("enable_log", True):
                    logger.info(f"LLM 生成的绘画提示词长度: {len(prompt)} 字符")
                    if len(prompt) <= 500:
                        logger.debug(f"绘画提示词: {prompt}")
                    else:
                        logger.debug(f"绘画提示词: {prompt[:500]}...")
                
                return prompt
            else:
                logger.error("LLM 返回数据异常")
                return None
                
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    # ==================== 绘画 API 调用 ====================
    
    async def generate_drawing(self, prompt: str, session_key: str) -> Optional[str]:
        """调用绘画 API 生成图片"""
        draw_config = self.get_draw_api_config()
        
        if not draw_config["api_url"]:
            logger.warning("绘画 API URL 未配置")
            return None
        
        # 随机选择图片尺寸
        use_portrait = random.choice([True, False])
        if use_portrait:
            width, height = 896, 1296  # 竖屏
        else:
            width, height = 1296, 896  # 横屏
        
        # 构建负面提示词
        negative_prompt = draw_config.get("negative_prompt", "")
        if not negative_prompt:
            negative_prompt = "lowres, bad anatomy, bad hands, text, error, missing fingers, extra digit, fewer digits, cropped, worst quality, low quality, normal quality, jpeg artifacts, signature, watermark, username, blurry, ugly, duplicate, morbid, mutilated, extra fingers, mutated hands, poorly drawn hands, poorly drawn face, mutation, deformed, blurry, bad proportions, extra limbs, cloned face, disfigured, gross proportions, malformed limbs, dark, gloomy, winter"
        
        # 构建请求体
        request_body = {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "width": width,
            "height": height,
            "steps": draw_config.get("steps", 42),
            "cfg_scale": draw_config.get("cfg_scale", 8),
            "sampler_name": draw_config.get("sampler", "DPM++ SDE Karras"),
            "enable_hr": False
        }
        
        if self.get_config("enable_log", True):
            logger.info(f"正在调用绘画 API，尺寸: {width}x{height}")
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    draw_config["api_url"],
                    headers={"Content-Type": "application/json"},
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=120)
                ) as response:
                    if response.status != 200:
                        logger.error(f"绘画 API 错误: {response.status}")
                        return None
                    
                    result = await response.json()
                    
                    if result.get("images") and len(result["images"]) > 0:
                        image_base64 = result["images"][0]
                        if self.get_config("enable_log", True):
                            logger.info(f"绘画生成成功，图片大小: {len(image_base64)} 字符")
                        return image_base64
                    else:
                        logger.error(f"绘画 API 返回无图片数据")
                        return None
                        
        except asyncio.TimeoutError:
            logger.error("绘画 API 请求超时")
            return None
        except Exception as e:
            logger.error(f"绘画 API 调用失败: {e}")
            return None
    
    def get_draw_api_config(self) -> dict:
        """获取绘画 API 配置"""
        return {
            "api_url": self.get_config("draw_api_url", ""),
            "negative_prompt": self.get_config("negative_prompt", ""),
            "steps": self.get_config("draw_steps", 42),
            "cfg_scale": self.get_config("draw_cfg_scale", 8),
            "sampler": self.get_config("draw_sampler", "DPM++ SDE Karras")
        }
    
    # ==================== 主动绘画命令 ====================
    
    @filter.command("draw")
    async def draw_command(self, event: AstrMessageEvent):
        """手动触发绘画
        
        使用方法:
        /draw <描述> - 根据描述生成绘画
        /draw - 使用上次对话内容生成绘画
        """
        # 获取用户输入
        user_input = event.message_str.strip()
        # 移除命令前缀
        if user_input.startswith("/draw"):
            user_input = user_input[5:].strip()
        
        session_key = self._get_session_key(event)
        
        # 如果没有输入描述，尝试使用上次对话
        if not user_input:
            # 检查是否有上次对话记录
            session_data = self.last_user_message.get(session_key, {})
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
            
            # 发送处理中提示
            yield event.plain_result("🎨 正在根据上次对话生成绘画，请稍候...")
            
            # 优化提示词
            prompt = await self.optimize_prompt_with_llm(user_msg, bot_reply)
            if not prompt:
                yield event.plain_result("❌ 绘画提示词生成失败，请稍后重试")
                return
            
            # 生成绘画
            image_base64 = await self.generate_drawing(prompt, session_key)
            if image_base64:
                yield event.plain_result(f"🎨 根据对话生成的绘画：")
                yield event.image_result(f"base64://{image_base64}")
            else:
                yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
        else:
            # 使用用户直接输入的描述
            yield event.plain_result("🎨 正在生成绘画，请稍候...")
            
            # 使用简化的提示词生成
            use_simple = self.get_config("use_simple_prompt", False)
            if use_simple:
                # 直接使用用户输入作为提示词的基础
                prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
            else:
                # 使用 LLM 优化
                prompt = await self.optimize_prompt_with_llm(user_input, "")
                if not prompt:
                    prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
            
            image_base64 = await self.generate_drawing(prompt, session_key)
            if image_base64:
                yield event.plain_result(f"🎨 绘画生成成功")
                yield event.image_result(f"base64://{image_base64}")
            else:
                yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
    
    # ==================== 监听机器人回复 ====================
    
    @filter.on_decorating_result(priority=10)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人发送的消息，记录最后回复"""
        try:
            # 只处理 AIOCQHTTP 平台
            if not isinstance(event, AiocqhttpMessageEvent):
                return
            
            # 获取消息内容
            content = self._extract_message_content(event)
            if not content:
                return
            
            # 获取会话信息
            group_id = event.get_group_id()
            session_id = group_id if group_id else event.get_sender_id()
            session_type = "群聊" if group_id else "私聊"
            session_key = self._get_session_key(event)
            
            # 获取用户最后一条消息
            user_msg = self.last_user_message.get(session_key, {}).get("user_message", "")
            
            # 更新最后回复记录
            self.last_reply = {
                "content": content,
                "session_id": session_id,
                "session_type": session_type,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "user_message": user_msg
            }
            
            # 更新会话数据
            if session_key not in self.last_user_message:
                self.last_user_message[session_key] = {}
            self.last_user_message[session_key]["bot_reply"] = content
            
            if self.get_config("enable_log", True):
                logger.debug(f"已记录机器人回复: {session_type} {session_id}")
            
            # 检查是否自动生成绘画
            auto_draw = self.get_config("auto_draw", False)
            if auto_draw and user_msg:
                if self.get_config("enable_log", True):
                    logger.info(f"自动绘画已启用，正在生成...")
                
                # 异步生成绘画，不阻塞回复
                asyncio.create_task(self._auto_draw_async(event, user_msg, content, session_key))
                
        except Exception as e:
            logger.error(f"记录回复消息时发生错误: {e}")
    
    async def _auto_draw_async(self, event: AstrMessageEvent, user_msg: str, bot_reply: str, session_key: str):
        """异步自动生成绘画"""
        try:
            # 优化提示词
            prompt = await self.optimize_prompt_with_llm(user_msg, bot_reply)
            if not prompt:
                logger.warning("自动绘画：提示词生成失败")
                return
            
            # 生成绘画
            image_base64 = await self.generate_drawing(prompt, session_key)
            if image_base64:
                # 发送绘画结果
                await event.send_result(
                    event.plain_result("🎨 根据对话自动生成的绘画：")
                )
                await event.send_result(
                    event.image_result(f"base64://{image_base64}")
                )
                logger.info("自动绘画生成成功")
            else:
                logger.warning("自动绘画生成失败")
        except Exception as e:
            logger.error(f"自动绘画失败: {e}")
    
    # ==================== 监听用户消息 ====================
    
    @filter.event(AstrMessageEvent)
    async def on_user_message(self, event: AstrMessageEvent):
        """监听用户消息，记录用户最后一条消息"""
        try:
            # 只处理 AIOCQHTTP 平台
            if not isinstance(event, AiocqhttpMessageEvent):
                return
            
            # 检查是否是机器人自己的消息
            if hasattr(event, 'is_from_self') and event.is_from_self():
                return
            
            # 获取用户消息
            user_msg = self._get_user_message(event)
            if not user_msg:
                return
            
            # 获取会话标识
            session_key = self._get_session_key(event)
            
            # 更新用户消息记录
            if session_key not in self.last_user_message:
                self.last_user_message[session_key] = {}
            self.last_user_message[session_key]["user_message"] = user_msg
            
            if self.get_config("enable_log", True):
                logger.debug(f"已记录用户消息: {session_key}")
                
        except Exception as e:
            logger.debug(f"记录用户消息失败: {e}")
    
    # ==================== 状态查询命令 ====================
    
    @filter.command("draw_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看绘画插件状态"""
        session_key = self._get_session_key(event)
        session_data = self.last_user_message.get(session_key, {})
        
        auto_draw = self.get_config("auto_draw", False)
        use_simple = self.get_config("use_simple_prompt", False)
        draw_api = self.get_draw_api_config()
        
        # 获取提示词配置状态
        has_custom_prompt = bool(self.get_config("draw_system_prompt", ""))
        has_custom_simple = bool(self.get_config("simple_system_prompt", ""))
        
        status_text = (
            f"🎨 **AI绘画插件状态**\n\n"
            f"✅ 插件状态: 运行中\n"
            f"🖼️ 绘画API: {'✅ 已配置' if draw_api['api_url'] else '❌ 未配置'}\n"
            f"🎨 自动绘画: {'✅ 开启' if auto_draw else '❌ 关闭'}\n"
            f"📝 提示词模式: {'简化模式' if use_simple else '完整模式'}\n"
            f"📋 自定义提示词: {'✅ 已配置' if has_custom_prompt else '使用默认'}\n\n"
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
            "⚙️ **配置说明（在插件管理页面配置）:**\n"
            "  • `auto_draw` - 是否自动生成绘画（默认关闭）\n"
            "  • `draw_api_url` - 绘画 API 地址\n"
            "  • `draw_steps` - 绘画步数（默认42）\n"
            "  • `draw_cfg_scale` - 提示词相关性（默认8）\n"
            "  • `draw_system_prompt` - 自定义绘画提示词生成规则\n"
            "  • `simple_system_prompt` - 简化模式提示词\n\n"
            "📌 **注意事项:**\n"
            "  • 绘画功能需要配置绘画 API 服务\n"
            "  • 提示词优化使用当前 AstrBot 配置的 LLM\n"
            "  • 自动绘画模式可能消耗较多资源\n\n"
            "💡 **示例:**\n"
            "  • 先与机器人对话，然后发送 `/draw`\n"
            "  • 直接发送 `/draw 流萤在花海中微笑`"
        )
        yield event.plain_result(help_text)
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.get_config("enable_log", True):
            logger.info("AI绘画插件已卸载")
