import asyncio
import json
import httpx
from typing import List
from traceback import format_exc

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

@register("xyjs", "Soulter", "校园集市(Zanao)自定义意图订阅推送插件。", "1.0.0")
class ZanaoZshPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.token = self.config.get("token", "")
        self.interval = self.config.get("interval_minutes", 10)
        self.my_user_id = self.config.get("my_user_id", "")
        self.subscriptions = self.config.get("subscriptions", [])
        
        self.is_running = False
        self.task: asyncio.Task = None
        self.headers = {
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/4.10.0",
            "X-Requested-With": "XMLHttpRequest",
            "X-Sc-Platform": "Android",
            "X-Sc-Client": "app",
            "X-Sc-Version": "2.2.2",
            "X-Sc-Alias": "neu"
        }
        
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        if self.token:
            self.headers["X-Sc-Token"] = self.token
            
        self.is_running = True
        self.task = asyncio.create_task(self.check_loop())
        logger.info(f"[XYJS] 校园集市监控已启动，当前间隔为 {self.interval} 分钟。")

    async def check_loop(self):
        while self.is_running:
            try:
                await self.fetch_and_check()
            except Exception as e:
                logger.error(f"[XYJS] 拉取校园集市帖子失败: {e}\n{format_exc()}")
            
            # 等待设定的间隔时间
            interval = max(1, self.config.get("interval_minutes", 10))
            await asyncio.sleep(interval * 60)
            
    async def fetch_and_check(self):
        if not self.token:
            return

        url = "https://api.app.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=self.headers)
            if resp.status_code != 200:
                logger.error(f"[XYJS] 网络请求失败: HTTP {resp.status_code}")
                return
            
            data = resp.json()
            if data.get("code") != 200:
                logger.error(f"[XYJS] API 业务报错: {data.get('msg')}")
                return
            
            posts = data.get("data", {}).get("list", [])
            logger.info(f"[XYJS] 成功拉取最新 {len(posts)} 条帖子数据。")
            if not posts:
                return
            
            # 使用集合保存已经处理过的帖子 ID 以免重复推送
            if not hasattr(self, "processed_post_ids"):
                self.processed_post_ids = set()

            for post in reversed(posts):
                post_id = post.get("thread_id")
                if post_id in self.processed_post_ids:
                    continue
                
                # 开始检查订阅匹配
                await self.process_post(post)
                self.processed_post_ids.add(post_id)
                
    async def process_post(self, post: dict):
        if not self.my_user_id or not self.subscriptions:
            return  # 用户没有配置接收 id 或订阅词，跳过
        
        # 将帖子的核心内容拼接起来，便于检索和发给 LLM
        thread_info = post.get("thread", {})
        title = thread_info.get("title", "")
        content = thread_info.get("content", "")
        post_text = f"标题: {title}\n内容: {content}"
        
        # 我们先进行一次粗筛：如果没有任何订阅关键词出现在文本中，直接忽略以节省 LLM 算力
        matched_subs = []
        for sub in self.subscriptions:
            # 简单粗暴的字面量包含检查（如果有更高级的需求也可以改完全交给 LLM）
            if any(keyword in post_text for keyword in sub):
                matched_subs.append(sub)
                
        if not matched_subs:
            return
            
        # 如果命中了关键词，调用 LLM 进行深度意图分析
        try:
            prompt = (
                f"你是一个校园集市监控助手。有一个新帖子引起了用户的注意。\n"
                f"【帖子详情】\n{post_text}\n"
                f"【用户的订阅意图列表】\n{json.dumps(matched_subs, ensure_ascii=False)}\n\n"
                f"请你仔细阅读上述帖子内容，并判断该帖子（出东西或是求购）是否真的符合用户上述任意一个订阅意图的实际目的。\n"
                f"注意：如果用户是买东西（比如订阅手机），别人求购手机就不符合。\n"
                f"请直接输出一个合法的 JSON，不要输出 Markdown 代码块，格式严格如下：\n"
                f'{{"match": true或false, "reason": "匹配或不匹配的原因", "summary": "如果匹配，请给出一句话概括这篇帖子的核心交易/咨询内容，否则留空"}}'
            )
            
            # 获取用户所在平台的聊天提供商
            umo = self.my_user_id  # 这里粗略地把 userid 作为 umo 使用。正常可能还需要 platform_id。如果是纯指令测试，可以在下文的 command 中拿
            # 为了能在后台运行且不用 UMO，我们直接从 context 中拿默认的 LLM
            llm_resp = await self.context.llm_generate(
                prompt=prompt,
            )
            
            result_text = llm_resp.completion_text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:-3]
            elif result_text.startswith("```"):
                result_text = result_text[3:-3]
                
            llm_decision = json.loads(result_text)
            
            if llm_decision.get("match") is True:
                # 触发推送
                summary = llm_decision.get("summary", "无概括")
                msg_str = f"🔔 [校园集市新帖提醒]\n{summary}\n\n👉 原文标题: {title[:20]}...\n💬 原文内容: {content[:50]}..."
                
                # 此处因为我们只有用户 ID，而没有完整上下文的 UMO，我们需要构造一个或者发送到群。
                # 由于 astrbot 需要明确发送目的地，如果在只知道 qq号 的情况下主动推送给对方。
                from astrbot.api.message_components import Plain
                from astrbot.api.event import MessageChain
                chain = MessageChain().message(msg_str)
                # 这往往依赖于用户发送过消息才有一个有效 umo。下面我们先粗暴发送，实际建议通过指令绑定得到一个真实的 umo
                await self.context.send_message(self.my_user_id, chain)
                logger.info(f"[XYJS] 已向 {self.my_user_id} 发送推送通知。")
                
        except Exception as e:
            logger.error(f"[XYJS] 帖子 LLM 分析或推送出错: {e}")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("[XYJS] 校园集市监控已停止。")

    @filter.command("xy_sub")
    async def cmd_xy_sub(self, event: AstrMessageEvent, keyword: str):
        """订阅校园集市关键词。用法: /xy_sub 想要捡漏一台二手手机"""
        if keyword in self.subscriptions:
            yield event.plain_result(f"您已经订阅过 '{keyword}' 啦！")
            return
        
        self.subscriptions.append(keyword)
        self.config["subscriptions"] = self.subscriptions
        self.config.save_config()
        # 顺便自动将当前用户的 ID 设为接收 ID
        if not self.my_user_id:
            self.my_user_id = event.get_sender_id()
            self.config["my_user_id"] = self.my_user_id
            self.config.save_config()
            
        yield event.plain_result(f"成功订阅校园集市意图：'{keyword}'！\n目前如果接收到相关的帖子，会向 ID: {self.my_user_id} 发送推送。")

    @filter.command("xy_unsub")
    async def cmd_xy_unsub(self, event: AstrMessageEvent, keyword: str):
        """取消订阅校园集市关键词。用法: /xy_unsub 想要捡漏一台二手手机"""
        if keyword not in self.subscriptions:
            yield event.plain_result(f"未找到关于 '{keyword}' 的订阅。")
            return
        
        self.subscriptions.remove(keyword)
        self.config["subscriptions"] = self.subscriptions
        self.config.save_config()
        yield event.plain_result(f"成功取消订阅意图：'{keyword}'。")

    @filter.command("xy_list")
    async def cmd_xy_list(self, event: AstrMessageEvent):
        """列出所有的订阅。"""
        if not self.subscriptions:
            yield event.plain_result("当前没有任何订阅。您可以使用 /xy_sub 添加订阅词。")
            return
            
        msg = "目前的校园集市订阅意图如下：\n"
        for idx, sub in enumerate(self.subscriptions, 1):
            msg += f"{idx}. {sub}\n"
        yield event.plain_result(msg.strip())
