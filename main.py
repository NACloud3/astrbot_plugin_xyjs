import asyncio
import json
import httpx
import time
import random
import hashlib
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
        self.proxy = self.config.get("proxy", "")
        
        self.is_running = False
        self.task: asyncio.Task = None
        
        # 严格伪装成微信小程序 PC 端
        self.headers = {
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/18151",
            "X-Requested-With": "XMLHttpRequest",
            "X-Sc-Platform": "windows",
            "X-Sc-Cloud": "0",
            "X-Sc-Appid": "wxa16ce35c0ad1a203",
            "xweb_xhr": "1",
            "X-Sc-Version": "4.1.2",
            "X-Sc-Alias": "neu",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
        }
        
    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        if self.token:
            # api.x.zanao.com 需要由 X-Sc-Od 承载 Token
            self.headers["X-Sc-Od"] = self.token
            
        self.is_running = True
        self.task = asyncio.create_task(self.check_loop())
        logger.info(f"[XYJS] 校园集市监控已启动，当前间隔为 {self.interval} 分钟。")

    async def check_loop(self):
        # 首次启动等待 10 秒再开始第一次拉取，避免启动时服务尚未就绪
        await asyncio.sleep(10)
        while self.is_running:
            try:
                logger.info(f"[XYJS] 开始本轮校园集市帖子检查。当前订阅: {self.subscriptions}, 接收ID: {self.my_user_id}")
                await self.fetch_and_check()
            except Exception as e:
                logger.error(f"[XYJS] 拉取校园集市帖子失败: {e}\n{format_exc()}")
            
            # 等待设定的间隔时间
            interval = max(1, self.config.get("interval_minutes", 10))
            await asyncio.sleep(interval * 60)
            
    def update_dynamic_headers(self):
        alias = self.headers.get("X-Sc-Alias", "neu")
        m = "".join(str(random.randint(0, 9)) for _ in range(20))
        td = int(time.time())
        # X-Sc-Ah 的签名算法特征串
        sign_string = f"{alias}_{m}_{td}_1b6d2514354bc407afdd935f45521a8c"
        sign_md5 = hashlib.md5(sign_string.encode('utf-8')).hexdigest()
        
        self.headers["X-Sc-Nd"] = m
        self.headers["X-Sc-Td"] = str(td)
        self.headers["X-Sc-Ah"] = sign_md5

    async def fetch_and_check(self):
        if not self.token:
            return

        # 换用突破封锁的 x 域名，并且改用 POST
        url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        self.update_dynamic_headers()
        
        # 支持用户配置代理
        proxies = self.proxy if self.proxy else None

        # 增加超时时间到 30 秒，防止校园网或者弱网环境导致 ConnectTimeout
        async with httpx.AsyncClient(timeout=30.0, proxy=proxies) as client:
            try:
                # 微信小程序接口普遍走 POST
                resp = await client.post(url, headers=self.headers)
            except httpx.ConnectTimeout:
                logger.error("[XYJS] 请求校园集市接口超时 (ConnectTimeout)。请检查是否有海外 IP 被屏蔽，或者配置 HTTP 代理。")
                return
            except Exception as e:
                logger.error(f"[XYJS] 网络请求发生未捕获错误: {e}")
                return
                
            if resp.status_code != 200:
                logger.error(f"[XYJS] 网络请求失败: HTTP {resp.status_code}")
                return
            
            data = resp.json()
            # zanao x API 使用 errno, errcode 或直接判断
            if data.get("errno", 0) != 0 or data.get("code", 200) != 200:
                err_msg = data.get('errmsg') or data.get('msg') or str(data)
                logger.error(f"[XYJS] API 业务报错: {err_msg}")
                return
            
            # 数据结构可能是 dict({"list": []}) 或者其他
            posts_data = data.get("data", {})
            if isinstance(posts_data, dict):
                posts = posts_data.get("list", [])
            elif isinstance(posts_data, list):
                posts = posts_data
            else:
                posts = []

            if not posts:
                logger.warning("[XYJS] 获取到的帖子列表为空 (返回了空数组)。如果您看到此提示，极有可能是您的 Token (原为 App Token) 在小程序接口上无效，或者您的 Token 已过期。请立刻前往【校园集市-微信小程序版】重新抓包获取 `X-Sc-Od` 的值，并在配置中替换原来的 Token！")
                return
                
            logger.info(f"[XYJS] 成功拉取最新 {len(posts)} 条帖子数据。")
            
            # 使用集合保存已经处理过的帖子 ID 以免重复推送
            if not hasattr(self, "processed_post_ids"):
                self.processed_post_ids = set()

            new_count = 0
            for post in reversed(posts):
                post_id = post.get("thread_id")
                if post_id in self.processed_post_ids:
                    continue
                
                new_count += 1
                title_preview = post.get("title", "")[:30]
                logger.info(f"[XYJS] 发现新帖子 ID={post_id}, 标题: {title_preview}")
                
                # 开始检查订阅匹配
                await self.process_post(post)
                self.processed_post_ids.add(post_id)
            
            if new_count == 0:
                logger.info("[XYJS] 本轮没有新帖子，全部已处理过。")
            else:
                logger.info(f"[XYJS] 本轮处理了 {new_count} 条新帖子。")
                
    async def process_post(self, post: dict):
        if not self.my_user_id or not self.subscriptions:
            logger.debug("[XYJS] 跳过: 未配置 my_user_id 或无订阅。")
            return
        
        # 根据开源项目的数据结构，标题和内容直接在帖子顶层对象中
        title = post.get("title", "")
        content = post.get("content", "")
        nickname = post.get("nickname", "")
        post_text = f"标题: {title}\n内容: {content}"
        
        # 粗筛：将每个订阅词作为整体进行包含检查
        matched_subs = []
        for sub in self.subscriptions:
            # 将订阅意图整体作为关键词匹配（比如 "外卖" 匹配包含"外卖"的帖子）
            if sub in post_text:
                matched_subs.append(sub)
                
        if not matched_subs:
            logger.debug(f"[XYJS] 帖子 '{title[:20]}' 未命中任何订阅词，跳过。")
            return
        
        logger.info(f"[XYJS] ✅ 帖子 '{title[:20]}' 命中订阅词: {matched_subs}，开始调用 LLM 分析...")
            
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
            logger.info(f"[XYJS] LLM 判定结果: match={llm_decision.get('match')}, reason={llm_decision.get('reason', 'N/A')}")
            
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
