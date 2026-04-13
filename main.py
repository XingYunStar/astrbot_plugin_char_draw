import asyncio
import json
import random
import tempfile
import base64
import os
import re
from datetime import datetime, date, timedelta
from typing import Optional, Dict, List, Tuple
from collections import OrderedDict

import aiohttp

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
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
    
    # 会话记录最大保留数量（LRU缓存）
    MAX_SESSION_MESSAGES = 100
    # 会话记录过期时间（小时）
    SESSION_EXPIRE_HOURS = 24
    
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config or {}
        
        # 使用 OrderedDict 实现 LRU 缓存
        self.session_messages: OrderedDict[str, Dict] = OrderedDict()
        
        # 存储用户每日使用次数
        self.user_daily_usage: Dict[str, Dict] = {}
        
        # 每日最大使用次数
        self.daily_limit = self.get_config("daily_limit", 100)
        
        # 启动后台清理任务
        self.cleanup_task = asyncio.create_task(self._periodic_cleanup())
        
        # 定义所有命令的正则表达式模式（支持正则匹配）
        # draw.* 匹配 draw, draw_status, draw_help 等所有以 draw 开头的命令
        # start, help, reset, forget.* 等其他命令
        self.command_patterns = [
            r"^draw.*",      # 匹配所有以 draw 开头的命令
            r"^start$",      # 精确匹配 start
            r"^help$",       # 精确匹配 help
            r"^reset$",      # 精确匹配 reset
            r"^forget.*",    # 匹配所有以 forget 开头的命令
        ]
        
        # 编译正则表达式以提高性能
        self.compiled_patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.command_patterns]
        
        logger.info("AI绘画插件已加载")
        if self.get_config("enable_log", True):
            logger.info(f"插件配置: 每日限制={self.daily_limit}次/用户, 会话缓存={self.MAX_SESSION_MESSAGES}条")
            logger.info(f"命令模式: {self.command_patterns}")
    
    # ==================== 通用工具方法 ====================
    
    def _extract_command_arg(self, event: AstrMessageEvent, command_name: str) -> str:
        """提取命令参数（去掉命令名本身）
        
        Args:
            event: 消息事件
            command_name: 命令名称（不含 /），如 "draw"
        
        Returns:
            命令参数，如果没有参数则返回空字符串
        """
        full_str = event.message_str.strip()
        
        # 去掉命令名
        if full_str.startswith(command_name):
            args = full_str[len(command_name):].strip()
            return args
        
        # 如果命令名不匹配，返回空
        return ""
    
    def _is_command_message(self, event: AstrMessageEvent) -> bool:
        """判断消息是否为命令（支持正则匹配）"""
        # 方法1：从原始消息判断（最可靠）
        raw_message = getattr(event.message_obj, 'raw_message', None)
        if raw_message and isinstance(raw_message, dict):
            if 'message' in raw_message:
                msg = raw_message['message']
                if isinstance(msg, str) and msg.startswith('/'):
                    # 去掉 / 后检查是否匹配命令模式
                    cmd_part = msg[1:].strip().split()[0] if msg[1:].strip() else ""
                    for pattern in self.compiled_patterns:
                        if pattern.match(cmd_part):
                            return True
                elif isinstance(msg, list):
                    for seg in msg:
                        if isinstance(seg, dict) and seg.get('type') == 'text':
                            text = seg.get('data', {}).get('text', '')
                            if text.startswith('/'):
                                cmd_part = text[1:].strip().split()[0] if text[1:].strip() else ""
                                for pattern in self.compiled_patterns:
                                    if pattern.match(cmd_part):
                                        return True
        
        # 方法2：通过 event 属性判断
        if hasattr(event, 'is_command') and event.is_command:
            return True
        
        # 方法3：通过已知命令模式匹配
        user_message = event.message_str.strip()
        # 提取第一个单词作为命令名
        cmd_name = user_message.split()[0] if user_message else ""
        
        for pattern in self.compiled_patterns:
            if pattern.match(cmd_name):
                return True
        
        return False
    
    # ==================== 生命周期管理 ====================
    
    async def _periodic_cleanup(self):
        """定期清理过期数据"""
        while True:
            try:
                await asyncio.sleep(3600)  # 每小时执行一次
                self._cleanup_expired_sessions()
                self._cleanup_expired_usage_records()
                if self.get_config("enable_log", True):
                    logger.debug(f"清理完成: 会话数={len(self.session_messages)}, 用户记录数={len(self.user_daily_usage)}")
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"定期清理任务出错: {e}")
    
    def _cleanup_expired_sessions(self):
        """清理过期的会话记录"""
        expire_time = datetime.now() - timedelta(hours=self.SESSION_EXPIRE_HOURS)
        
        expired_keys = []
        for key, data in self.session_messages.items():
            timestamp_str = data.get("timestamp", "")
            try:
                if timestamp_str:
                    timestamp = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                    if timestamp < expire_time:
                        expired_keys.append(key)
            except (ValueError, TypeError):
                expired_keys.append(key)
        
        for key in expired_keys:
            del self.session_messages[key]
        
        while len(self.session_messages) > self.MAX_SESSION_MESSAGES:
            self.session_messages.popitem(last=False)
    
    def _cleanup_expired_usage_records(self):
        """清理过期的使用记录（超过3天未使用）"""
        three_days_ago = date.today() - timedelta(days=3)
        expired_users = []
        
        for user_id, data in self.user_daily_usage.items():
            last_date_str = data.get("date", "")
            try:
                if last_date_str:
                    last_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                    if last_date < three_days_ago:
                        expired_users.append(user_id)
            except (ValueError, TypeError):
                expired_users.append(user_id)
        
        for user_id in expired_users:
            del self.user_daily_usage[user_id]
    
    def _update_session_messages(self, key: str, value: Dict):
        """更新会话消息（LRU 自动维护）"""
        if key in self.session_messages:
            self.session_messages.move_to_end(key)
        self.session_messages[key] = value
    
    def _get_session_messages(self, key: str) -> Optional[Dict]:
        """获取会话消息（LRU 自动维护）"""
        if key in self.session_messages:
            self.session_messages.move_to_end(key)
            return self.session_messages[key]
        return None
    
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
    
    def _get_user_id(self, event: AstrMessageEvent) -> str:
        """获取用户唯一标识"""
        return str(event.get_sender_id())
    
    def _check_and_update_usage(self, user_id: str) -> Tuple[bool, int, int]:
        """检查并更新用户使用次数"""
        today = date.today().isoformat()
        
        user_data = self.user_daily_usage.get(user_id, {})
        last_date = user_data.get("date", "")
        used_count = user_data.get("count", 0)
        
        if last_date != today:
            used_count = 0
            user_data = {"date": today, "count": 0}
        
        remaining = self.daily_limit - used_count
        
        if used_count >= self.daily_limit:
            return False, 0, used_count
        
        user_data["count"] = used_count + 1
        self.user_daily_usage[user_id] = user_data
        
        return True, remaining - 1, used_count + 1
    
    def _get_remaining_count(self, user_id: str) -> int:
        """获取用户今日剩余次数"""
        today = date.today().isoformat()
        user_data = self.user_daily_usage.get(user_id, {})
        last_date = user_data.get("date", "")
        used_count = user_data.get("count", 0)
        
        if last_date != today:
            return self.daily_limit
        
        return max(0, self.daily_limit - used_count)
    
    # ==================== LLM 配置 ====================
    
    def get_llm_config(self) -> dict:
        """获取 LLM 配置"""
        return {
            "api_url": self.get_config("llm_api_url", "https://api.siliconflow.cn/v1/chat/completions"),
            "api_key": self.get_config("llm_api_key", ""),
            "model": self.get_config("llm_model", "deepseek-ai/DeepSeek-V3.2")
        }
    
    # ==================== 绘画提示词系统提示 ====================
    
    def get_draw_system_prompt(self) -> str:
        """获取绘画提示词生成系统提示"""
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
        """获取简化模式系统提示"""
        custom_simple = self.get_config("simple_system_prompt", "")
        if custom_simple and custom_simple.strip():
            return custom_simple.strip()
        
        return """将用户输入转换为英文AI绘画提示词，动漫风格，包含 firefly (honkai star rail), 1girl, best quality, masterpiece。
只输出英文tag串，不要其他内容。"""
    
    # ==================== LLM 调用 ====================
    
    async def call_llm(self, user_message: str, bot_reply: str) -> Optional[str]:
        """调用 LLM API 生成绘画提示词"""
        llm_config = self.get_llm_config()
        
        if not llm_config["api_key"]:
            logger.error("[LLM] API Key 未配置")
            return None
        
        use_simple = self.get_config("use_simple_prompt", False)
        system_prompt = self.get_simple_draw_prompt() if use_simple else self.get_draw_system_prompt()
        
        if use_simple:
            user_content = f"将以下内容转换为英文绘画提示词：\n用户说：{user_message}\n机器人说：{bot_reply}"
        else:
            user_content = f"用户说：【{user_message}】\n\n机器人说：【{bot_reply}】"
        
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]
        
        request_body = {
            "model": llm_config["model"],
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 8192
        }
        
        api_url = llm_config["api_url"]
        if not api_url.endswith("/chat/completions"):
            api_url = api_url.rstrip("/") + "/chat/completions"
        
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {llm_config['api_key']}"
        }
        
        logger.info("[LLM] " + "=" * 50)
        logger.info("[LLM] 开始调用 LLM 生成绘画提示词")
        logger.info(f"[LLM] API URL: {api_url}")
        logger.info(f"[LLM] 模型: {llm_config['model']}")
        logger.info(f"[LLM] 使用简化模式: {use_simple}")
        logger.info(f"[LLM] 用户消息长度: {len(user_message)} 字符")
        logger.info(f"[LLM] 机器人回复长度: {len(bot_reply)} 字符")
        
        try:
            start_time = datetime.now()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers=headers,
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=180)
                ) as response:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f"[LLM] 响应状态码: {response.status}, 耗时: {elapsed:.2f}秒")
                    
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"[LLM] API 错误: {response.status}")
                        logger.error(f"[LLM] 错误详情: {error_text[:500]}")
                        return None
                    
                    data = await response.json()
                    
                    usage = data.get("usage", {})
                    if usage:
                        logger.info(f"[LLM] Token 使用: prompt={usage.get('prompt_tokens', 0)}, "
                                   f"completion={usage.get('completion_tokens', 0)}, "
                                   f"total={usage.get('total_tokens', 0)}")
                    
                    if data.get("choices") and len(data["choices"]) > 0:
                        prompt = data["choices"][0]["message"]["content"].strip()
                        
                        if prompt.startswith("```"):
                            lines = prompt.split("\n")
                            prompt = "\n".join(lines[1:-1]) if len(lines) > 2 else prompt
                        prompt = prompt.replace("```", "").strip()
                        
                        logger.info(f"[LLM] 生成提示词长度: {len(prompt)} 字符")
                        logger.info(f"[LLM] 生成提示词内容:\n{prompt[:500]}{'...' if len(prompt) > 500 else ''}")
                        logger.info("[LLM] " + "=" * 50)
                        
                        return prompt
                    else:
                        logger.error(f"[LLM] 响应数据异常")
                        return None
        except asyncio.TimeoutError:
            logger.error("[LLM] 请求超时 (18秒)")
            return None
        except Exception as e:
            logger.error(f"[LLM] 调用失败: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[LLM] 堆栈:\n{traceback.format_exc()}")
            return None
    
    # ==================== 绘画 API 调用 ====================
    
    async def generate_drawing(self, prompt: str) -> Optional[str]:
        """调用绘画 API 生成图片，返回临时文件路径"""
        api_url = self.get_config("draw_api_url", "")
        if not api_url:
            logger.warning("[绘画] API URL 未配置")
            return None
        
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
        
        logger.info("[绘画] " + "=" * 50)
        logger.info(f"[绘画] API URL: {api_url}")
        logger.info(f"[绘画] 尺寸: {width}x{height}")
        logger.info(f"[绘画] 步数: {request_body['steps']}, CFG: {request_body['cfg_scale']}")
        
        try:
            start_time = datetime.now()
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    api_url,
                    headers={"Content-Type": "application/json"},
                    json=request_body,
                    timeout=aiohttp.ClientTimeout(total=600)
                ) as response:
                    elapsed = (datetime.now() - start_time).total_seconds()
                    logger.info(f"[绘画] 响应状态码: {response.status}, 耗时: {elapsed:.2f}秒")
                    
                    response_text = await response.text()
                    
                    if response.status != 200:
                        logger.error(f"[绘画] HTTP 错误: {response.status}")
                        logger.error(f"[绘画] 响应内容: {response_text[:500]}")
                        return None
                    
                    try:
                        result = json.loads(response_text)
                    except json.JSONDecodeError as e:
                        logger.error(f"[绘画] JSON 解析失败: {e}")
                        logger.error(f"[绘画] 响应内容: {response_text[:500]}")
                        return None
                    
                    if result.get("images") and len(result["images"]) > 0:
                        image_base64 = result["images"][0]
                        logger.info(f"[绘画] 获取到图片，Base64 长度: {len(image_base64)} 字符")
                        
                        try:
                            image_data = base64.b64decode(image_base64)
                            logger.info(f"[绘画] 图片解码成功，大小: {len(image_data)} 字节 ({len(image_data)/1024:.1f} KB)")
                            
                            temp_file = tempfile.NamedTemporaryFile(
                                suffix=".png", 
                                delete=False,
                                dir="/tmp"
                            )
                            temp_file.write(image_data)
                            temp_file.close()
                            
                            logger.info(f"[绘画] 图片已保存: {temp_file.name}")
                            logger.info("[绘画] " + "=" * 50)
                            return temp_file.name
                        except Exception as e:
                            logger.error(f"[绘画] 保存图片失败: {e}")
                            return None
                    else:
                        logger.error(f"[绘画] 响应中无图片数据")
                        return None
        except asyncio.TimeoutError:
            logger.error("[绘画] 请求超时 (600秒)")
            return None
        except Exception as e:
            logger.error(f"[绘画] 调用失败: {type(e).__name__}: {e}")
            import traceback
            logger.error(f"[绘画] 堆栈:\n{traceback.format_exc()}")
            return None
    
    def _cleanup_temp_file(self, file_path: str):
        """清理临时文件"""
        try:
            if file_path and os.path.exists(file_path):
                os.unlink(file_path)
                logger.debug(f"[清理] 已删除临时文件: {file_path}")
        except Exception as e:
            logger.debug(f"[清理] 删除失败: {e}")
    
    # ==================== 主动绘画命令 ====================
    
    @filter.command("draw")
    async def draw_command(self, event: AstrMessageEvent):
        """手动触发绘画
        
        使用方法:
        /draw - 根据上次对话生成绘画
        /draw <描述> - 根据描述生成绘画
        """
        # 正确提取用户输入（去掉命令名本身）
        user_input = self._extract_command_arg(event, "draw")
        
        session_key = self._get_session_key(event)
        session_data = self._get_session_messages(session_key) or {}
        user_id = self._get_user_id(event)
        
        # 检查每日使用次数限制
        can_use, remaining, today_used = self._check_and_update_usage(user_id)
        if not can_use:
            yield event.plain_result(
                f"❌ 您今日的绘画次数已用完！\n\n"
                f"📊 今日已使用: {today_used}/{self.daily_limit} 次\n"
                f"🕐 请明天再试"
            )
            return
        
        # 检查 LLM 配置
        llm_config = self.get_llm_config()
        if not llm_config["api_key"]:
            yield event.plain_result(
                "❌ LLM API Key 未配置！\n\n"
                "请在插件配置中填写 llm_api_key"
            )
            return
        
        # 检查绘画 API 配置
        if not self.get_config("draw_api_url", ""):
            yield event.plain_result(
                "❌ 绘画 API URL 未配置！\n\n"
                "请在插件配置中填写 draw_api_url"
            )
            return
        
        temp_file_path = None
        
        try:
            # 如果没有输入描述，使用上次对话
            if not user_input:
                user_msg = session_data.get("user_message", "")
                bot_reply = session_data.get("bot_reply", "")
                
                if not user_msg or not bot_reply:
                    yield event.plain_result(
                        f"📭 没有找到可用的对话记录。\n\n"
                        f"使用方法：\n"
                        f"1. 先与机器人对话，然后发送 /draw\n"
                        f"2. 或直接发送：/draw 描述文字\n\n"
                        f"📊 今日剩余次数: {remaining} 次"
                    )
                    return
                
                yield event.plain_result(f"🎨 正在根据上次对话生成绘画，请稍候...\n📊 今日剩余次数: {remaining} 次")
                
                prompt = await self.call_llm(user_msg, bot_reply)
                if not prompt:
                    yield event.plain_result("❌ 绘画提示词生成失败，请稍后重试")
                    return
                
                temp_file_path = await self.generate_drawing(prompt)
                if temp_file_path:
                    yield event.plain_result("🎨 根据对话生成的绘画：")
                    yield event.image_result(temp_file_path)
                else:
                    yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
            else:
                yield event.plain_result(f"🎨 正在生成绘画，请稍候...\n📊 今日剩余次数: {remaining} 次")
                
                use_simple = self.get_config("use_simple_prompt", False)
                if use_simple:
                    prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
                else:
                    prompt = await self.call_llm(user_input, "")
                    if not prompt:
                        prompt = f"{user_input}, firefly (honkai star rail), 1girl, best quality, masterpiece"
                
                temp_file_path = await self.generate_drawing(prompt)
                if temp_file_path:
                    yield event.plain_result("🎨 绘画生成成功")
                    yield event.image_result(temp_file_path)
                else:
                    yield event.plain_result("❌ 绘画生成失败，请检查绘画 API 配置")
        finally:
            if temp_file_path:
                asyncio.create_task(self._delayed_cleanup(temp_file_path, delay=30))
    
    async def _delayed_cleanup(self, file_path: str, delay: int = 30):
        """延迟删除临时文件"""
        await asyncio.sleep(delay)
        self._cleanup_temp_file(file_path)
    
    # ==================== 监听机器人回复 ====================
    
    @filter.on_decorating_result(priority=10)
    async def on_bot_reply(self, event: AstrMessageEvent):
        """监听机器人即将发送的消息，记录对话"""
        try:
            user_message = event.message_str
            if not user_message:
                return
            
            # 判断是否为命令（跳过命令）
            if self._is_command_message(event):
                if self.get_config("enable_log", True):
                    logger.debug(f"[会话] 跳过命令消息: {user_message[:30]}")
                return
            
            result = event.get_result()
            if not result or not result.chain:
                return
            
            bot_reply = ""
            for segment in result.chain:
                if hasattr(segment, 'text') and segment.text:
                    bot_reply += segment.text
            
            if not bot_reply:
                return
            
            # 过滤绘画命令的回复
            skip_patterns = [
                "🎨 正在根据上次对话生成绘画",
                "🎨 正在生成绘画",
                "🎨 根据对话生成的绘画",
                "🎨 绘画生成成功",
                "❌ 绘画",
                "📭 没有找到可用的对话记录",
                "🎨 根据对话自动生成的绘画"
            ]
            for pattern in skip_patterns:
                if pattern in bot_reply:
                    if self.get_config("enable_log", True):
                        logger.debug(f"[会话] 跳过绘画回复")
                    return
            
            session_key = self._get_session_key(event)
            
            self._update_session_messages(session_key, {
                "user_message": user_message,
                "bot_reply": bot_reply,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            })
            
            if self.get_config("enable_log", True):
                logger.debug(f"[会话] 已记录: {session_key}")
            
            # 自动绘画
            auto_draw = self.get_config("auto_draw", False)
            if auto_draw:
                asyncio.create_task(self._auto_draw_async(event, user_message, bot_reply, session_key))
                
        except Exception as e:
            logger.error(f"[会话] 记录失败: {e}")
    
    async def _auto_draw_async(self, event: AstrMessageEvent, user_msg: str, bot_reply: str, session_key: str):
        """异步自动生成绘画"""
        temp_file_path = None
        try:
            if not self.get_config("draw_api_url", ""):
                return
            if not self.get_config("llm_api_key", ""):
                return
            
            user_id = self._get_user_id(event)
            can_use, _, _ = self._check_and_update_usage(user_id)
            if not can_use:
                logger.info(f"[自动] 用户 {user_id} 今日次数已用完")
                return
            
            prompt = await self.call_llm(user_msg, bot_reply)
            if not prompt:
                return
            
            temp_file_path = await self.generate_drawing(prompt)
            if temp_file_path:
                await event.send(event.plain_result("🎨 根据对话自动生成的绘画："))
                await event.send(event.image_result(temp_file_path))
                logger.info("[自动] 绘画生成成功")
        except Exception as e:
            logger.error(f"[自动] 失败: {e}")
        finally:
            if temp_file_path:
                asyncio.create_task(self._delayed_cleanup(temp_file_path, delay=30))
    
    # ==================== 状态查询命令 ====================
    
    @filter.command("draw_status")
    async def status_command(self, event: AstrMessageEvent):
        """查看绘画插件状态"""
        session_key = self._get_session_key(event)
        session_data = self._get_session_messages(session_key) or {}
        user_id = self._get_user_id(event)
        remaining = self._get_remaining_count(user_id)
        
        auto_draw = self.get_config("auto_draw", False)
        use_simple = self.get_config("use_simple_prompt", False)
        api_url = self.get_config("draw_api_url", "")
        llm_api_key = self.get_config("llm_api_key", "")
        
        status_text = (
            f"🎨 **AI绘画插件状态**\n\n"
            f"✅ 插件状态: 运行中\n"
            f"🖼️ 绘画API: {'✅ 已配置' if api_url else '❌ 未配置'}\n"
            f"🤖 LLM API: {'✅ 已配置' if llm_api_key else '❌ 未配置'}\n"
            f"🎨 自动绘画: {'✅ 开启' if auto_draw else '❌ 关闭'}\n"
            f"📝 提示词模式: {'简化模式' if use_simple else '完整模式'}\n"
            f"📊 每日限制: {self.daily_limit} 次/用户\n"
            f"🎫 今日剩余: {remaining} 次\n"
            f"💾 缓存统计: {len(self.session_messages)} 个会话\n\n"
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
            "  • `llm_api_key` - LLM API Key（必需）\n"
            "  • `llm_api_url` - LLM API 地址\n"
            "  • `llm_model` - 模型名称\n"
            "  • `draw_api_url` - 绘画 API 地址（必需）\n"
            "  • `auto_draw` - 是否自动生成绘画\n"
            "  • `daily_limit` - 每日绘画次数限制\n\n"
            f"📊 **使用限制:**\n"
            f"  • 每个用户每天最多 {self.get_config('daily_limit', 100)} 次绘画\n"
            f"  • 次数每天零点重置\n\n"
            "💡 **示例:**\n"
            "  • 先与机器人对话，然后发送 `/draw`\n"
            "  • 直接发送 `/draw 流萤在花海中微笑`"
        )
        yield event.plain_result(help_text)
    
    async def terminate(self):
        """插件卸载时的清理工作"""
        if self.cleanup_task and not self.cleanup_task.done():
            self.cleanup_task.cancel()
            try:
                await self.cleanup_task
            except asyncio.CancelledError:
                pass
        
        if self.get_config("enable_log", True):
            logger.info("AI绘画插件已卸载")
