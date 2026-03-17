import asyncio
import json
import os
import httpx
import time
import random
import hashlib
from traceback import format_exc

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

SUBS_FILENAME = "user_subs.json"

@register("xyjs", "NACloud3", "校园集市(Zanao)自定义意图订阅推送插件。", "2.0.0")
class ZanaoXYJSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.token = self.config.get("token", "")
        self.interval = self.config.get("interval_minutes", 10)
        self.proxy = self.config.get("proxy", "")

        self.is_running = False
        self.task: asyncio.Task = None
        self.processed_post_ids: set = set()

        # 多用户订阅字典: { "session_str": ["关键词1", ...] }
        self.user_subs: dict[str, list[str]] = {}
        self._subs_path = os.path.join(os.path.dirname(__file__), SUBS_FILENAME)
        self._load_subs()

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

    # ──────────────── 订阅数据持久化 ────────────────

    def _load_subs(self):
        """从 JSON 文件加载多用户订阅数据。"""
        if os.path.exists(self._subs_path):
            try:
                with open(self._subs_path, "r", encoding="utf-8") as f:
                    self.user_subs = json.load(f)
                logger.info(f"[XYJS] 已加载 {len(self.user_subs)} 个用户的订阅数据。")
            except Exception as e:
                logger.error(f"[XYJS] 加载订阅文件失败: {e}")
                self.user_subs = {}
        else:
            self.user_subs = {}

    def _save_subs(self):
        """将多用户订阅数据写入 JSON 文件。"""
        try:
            with open(self._subs_path, "w", encoding="utf-8") as f:
                json.dump(self.user_subs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[XYJS] 保存订阅文件失败: {e}")

    # ──────────────── 生命周期 ────────────────

    async def initialize(self):
        if self.token:
            self.headers["X-Sc-Od"] = self.token

        self.is_running = True
        self.task = asyncio.create_task(self.check_loop())
        logger.info(f"[XYJS] 校园集市监控已启动，间隔 {self.interval} 分钟，当前 {len(self.user_subs)} 个用户有订阅。")

    async def terminate(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("[XYJS] 校园集市监控已停止。")

    # ──────────────── 定时拉取循环 ────────────────

    async def check_loop(self):
        await asyncio.sleep(10)
        while self.is_running:
            try:
                total_subs = sum(len(v) for v in self.user_subs.values())
                logger.info(f"[XYJS] 开始本轮帖子检查。{len(self.user_subs)} 个用户共 {total_subs} 条订阅。")
                await self.fetch_and_check()
            except Exception as e:
                logger.error(f"[XYJS] 拉取校园集市帖子失败: {e}\n{format_exc()}")

            interval = max(1, self.config.get("interval_minutes", 10))
            await asyncio.sleep(interval * 60)

    def update_dynamic_headers(self):
        alias = self.headers.get("X-Sc-Alias", "neu")
        m = "".join(str(random.randint(0, 9)) for _ in range(20))
        td = int(time.time())
        sign_string = f"{alias}_{m}_{td}_1b6d2514354bc407afdd935f45521a8c"
        sign_md5 = hashlib.md5(sign_string.encode("utf-8")).hexdigest()
        self.headers["X-Sc-Nd"] = m
        self.headers["X-Sc-Td"] = str(td)
        self.headers["X-Sc-Ah"] = sign_md5

    async def fetch_and_check(self):
        if not self.token:
            logger.warning("[XYJS] 未配置 Token，跳过本轮检查。")
            return
        if not self.user_subs:
            logger.info("[XYJS] 暂无任何用户订阅，跳过本轮检查。")
            return

        url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        self.update_dynamic_headers()

        proxies = self.proxy if self.proxy else None
        async with httpx.AsyncClient(timeout=30.0, proxy=proxies) as client:
            try:
                resp = await client.post(url, headers=self.headers)
            except httpx.ConnectTimeout:
                logger.error("[XYJS] 请求校园集市接口超时。请检查网络或配置代理。")
                return
            except Exception as e:
                logger.error(f"[XYJS] 网络请求错误: {e}")
                return

            if resp.status_code != 200:
                logger.error(f"[XYJS] HTTP {resp.status_code}")
                return

            data = resp.json()
            if data.get("errno", 0) != 0 or data.get("code", 200) != 200:
                err_msg = data.get("errmsg") or data.get("msg") or str(data)
                logger.error(f"[XYJS] API 报错: {err_msg}")
                return

            posts_data = data.get("data", {})
            posts = posts_data.get("list", []) if isinstance(posts_data, dict) else (posts_data if isinstance(posts_data, list) else [])

            if not posts:
                logger.warning("[XYJS] 帖子列表为空。Token 可能已过期，请重新抓包获取。")
                return

            logger.info(f"[XYJS] 成功拉取 {len(posts)} 条帖子。")

            new_count = 0
            for post in reversed(posts):
                post_id = post.get("thread_id")
                if post_id in self.processed_post_ids:
                    continue

                new_count += 1
                title_preview = post.get("title", "")[:30]
                logger.info(f"[XYJS] 新帖子 ID={post_id}: {title_preview}")
                await self.process_post(post)
                self.processed_post_ids.add(post_id)

            if new_count == 0:
                logger.info("[XYJS] 本轮无新帖子。")
            else:
                logger.info(f"[XYJS] 本轮处理了 {new_count} 条新帖子。")

    # ──────────────── 帖子处理与多用户匹配 ────────────────

    async def process_post(self, post: dict):
        title = post.get("title", "")
        content = post.get("content", "")
        post_text = f"标题: {title}\n内容: {content}"

        # 遍历所有用户的订阅，分别匹配
        for session_str, subs in self.user_subs.items():
            matched_subs = [sub for sub in subs if sub in post_text]
            if not matched_subs:
                continue

            logger.info(f"[XYJS] ✅ 帖子 '{title[:20]}' 命中用户 {session_str[:30]} 的订阅: {matched_subs}")
            await self.analyze_and_push(post, post_text, title, content, matched_subs, session_str)

    async def analyze_and_push(self, post: dict, post_text: str, title: str, content: str, matched_subs: list, session_str: str):
        try:
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("[XYJS] 无法获取默认 LLM Provider。")
                return
            provider_id = provider.meta().id

            prompt = (
                f"你是一个校园集市监控助手。有一个新帖子引起了用户的注意。\n"
                f"【帖子详情】\n{post_text}\n"
                f"【用户的订阅意图列表】\n{json.dumps(matched_subs, ensure_ascii=False)}\n\n"
                f"请你仔细阅读上述帖子内容，并判断该帖子（出东西或是求购）是否真的符合用户上述任意一个订阅意图的实际目的。\n"
                f"注意：如果用户是买东西（比如订阅手机），别人求购手机就不符合。\n"
                f"请直接输出一个合法的 JSON，不要输出 Markdown 代码块，格式严格如下：\n"
                f'{{"match": true或false, "reason": "匹配或不匹配的原因", "summary": "如果匹配，请给出一句话概括这篇帖子的核心交易/咨询内容，否则留空"}}'
            )

            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )

            result_text = llm_resp.completion_text.strip()
            if result_text.startswith("```json"):
                result_text = result_text[7:]
            if result_text.startswith("```"):
                result_text = result_text[3:]
            if result_text.endswith("```"):
                result_text = result_text[:-3]
            result_text = result_text.strip()

            llm_decision = json.loads(result_text)
            logger.info(f"[XYJS] LLM 判定: match={llm_decision.get('match')}, reason={llm_decision.get('reason', 'N/A')}")

            if llm_decision.get("match") is True:
                summary = llm_decision.get("summary", "无概括")
                msg_str = (
                    f"🔔 [校园集市新帖提醒]\n"
                    f"{summary}\n\n"
                    f"👉 标题: {title}\n"
                    f"💬 内容: {content[:100]}{'...' if len(content) > 100 else ''}"
                )
                from astrbot.api.event import MessageChain
                chain = MessageChain().message(msg_str)
                await self.context.send_message(session_str, chain)
                logger.info(f"[XYJS] 已向 {session_str[:30]} 推送通知。")

        except Exception as e:
            logger.error(f"[XYJS] LLM 分析或推送出错: {e}")

    # ──────────────── 用户指令 ────────────────

    @filter.command("xysub")
    async def cmd_xysub(self, event: AstrMessageEvent, keyword: str):
        """订阅校园集市关键词。用法: /xysub 外卖"""
        session_str = str(event.session)
        user_list = self.user_subs.setdefault(session_str, [])

        if keyword in user_list:
            yield event.plain_result(f"您已经订阅过 '{keyword}' 啦！")
            return

        user_list.append(keyword)
        self._save_subs()
        yield event.plain_result(
            f"✅ 成功订阅：'{keyword}'\n"
            f"您当前共有 {len(user_list)} 条订阅。\n"
            f"有新的匹配帖子时会自动推送到本会话。"
        )

    @filter.command("xyunsub")
    async def cmd_xyunsub(self, event: AstrMessageEvent, keyword: str):
        """取消订阅校园集市关键词。用法: /xyunsub 外卖"""
        session_str = str(event.session)
        user_list = self.user_subs.get(session_str, [])

        if keyword not in user_list:
            yield event.plain_result(f"未找到您的订阅 '{keyword}'。")
            return

        user_list.remove(keyword)
        if not user_list:
            self.user_subs.pop(session_str, None)
        self._save_subs()
        yield event.plain_result(f"✅ 已取消订阅：'{keyword}'")

    @filter.command("xylist")
    async def cmd_xylist(self, event: AstrMessageEvent):
        """查看自己的订阅列表。"""
        session_str = str(event.session)
        user_list = self.user_subs.get(session_str, [])

        if not user_list:
            yield event.plain_result("您当前没有任何订阅。\n使用 /xysub <关键词> 添加订阅。")
            return

        msg = "📋 您的校园集市订阅：\n"
        for idx, sub in enumerate(user_list, 1):
            msg += f"  {idx}. {sub}\n"
        yield event.plain_result(msg.strip())
