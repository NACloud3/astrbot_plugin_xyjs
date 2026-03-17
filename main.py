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

@register("xyjs", "NACloud3", "校园集市(Zanao)自定义意图订阅推送插件。", "2.1.0")
class ZanaoXYJSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.interval = self.config.get("interval_minutes", 10)
        self.proxy = self.config.get("proxy", "")

        self.is_running = False
        self.task: asyncio.Task = None
        self.processed_post_ids: dict[str, set] = {}  # 每个 alias+token 组合各自的已处理集

        # 多用户数据: { session_str: { "alias": "neu", "token": "xxx", "subs": [...] } }
        self.user_data: dict[str, dict] = {}
        self._subs_path = os.path.join(os.path.dirname(__file__), SUBS_FILENAME)
        self._load_data()

    # ──────────────── 数据持久化 ────────────────

    def _load_data(self):
        if os.path.exists(self._subs_path):
            try:
                with open(self._subs_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 兼容 v2.0.0 旧格式 { session: ["kw1",...] }
                for k, v in raw.items():
                    if isinstance(v, list):
                        # 旧格式：迁移为新格式，alias/token 留空等用户重新绑定
                        self.user_data[k] = {"alias": "", "token": "", "subs": v}
                    elif isinstance(v, dict):
                        self.user_data[k] = v
                logger.info(f"[XYJS] 已加载 {len(self.user_data)} 个用户的数据。")
            except Exception as e:
                logger.error(f"[XYJS] 加载数据文件失败: {e}")
                self.user_data = {}
        else:
            self.user_data = {}

    def _save_data(self):
        try:
            with open(self._subs_path, "w", encoding="utf-8") as f:
                json.dump(self.user_data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[XYJS] 保存数据文件失败: {e}")

    # ──────────────── 生命周期 ────────────────

    async def initialize(self):
        self.is_running = True
        self.task = asyncio.create_task(self.check_loop())
        active = sum(1 for d in self.user_data.values() if d.get("token") and d.get("subs"))
        logger.info(f"[XYJS] 校园集市监控已启动，间隔 {self.interval} 分钟，{active} 个活跃用户。")

    async def terminate(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
        logger.info("[XYJS] 校园集市监控已停止。")

    # ──────────────── 签名与请求 ────────────────

    @staticmethod
    def _build_headers(alias: str, token: str) -> dict:
        """为指定学校和 Token 构建完整的请求头（含动态签名）。"""
        m = "".join(str(random.randint(0, 9)) for _ in range(20))
        td = int(time.time())
        sign_string = f"{alias}_{m}_{td}_1b6d2514354bc407afdd935f45521a8c"
        sign_md5 = hashlib.md5(sign_string.encode("utf-8")).hexdigest()

        return {
            "Accept-Encoding": "gzip, deflate, br",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI MiniProgramEnv/Windows WindowsWechat/WMPF WindowsWechat(0x63090a13) XWEB/18151",
            "X-Requested-With": "XMLHttpRequest",
            "X-Sc-Platform": "windows",
            "X-Sc-Cloud": "0",
            "X-Sc-Appid": "wxa16ce35c0ad1a203",
            "xweb_xhr": "1",
            "X-Sc-Version": "4.1.2",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "*/*",
            "X-Sc-Alias": alias,
            "X-Sc-Od": token,
            "X-Sc-Nd": m,
            "X-Sc-Td": str(td),
            "X-Sc-Ah": sign_md5,
        }

    # ──────────────── 定时拉取循环 ────────────────

    async def check_loop(self):
        await asyncio.sleep(10)
        while self.is_running:
            try:
                await self.fetch_all_schools()
            except Exception as e:
                logger.error(f"[XYJS] 拉取帖子失败: {e}\n{format_exc()}")

            interval = max(1, self.config.get("interval_minutes", 10))
            await asyncio.sleep(interval * 60)

    async def fetch_all_schools(self):
        """按学校+Token 分组拉取帖子，然后分别匹配对应用户的订阅。"""
        # 分组: key=(alias, token), value=[(session_str, subs), ...]
        groups: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
        for session_str, data in self.user_data.items():
            alias = data.get("alias", "")
            token = data.get("token", "")
            subs = data.get("subs", [])
            if not alias or not token or not subs:
                continue
            key = (alias, token)
            groups.setdefault(key, []).append((session_str, subs))

        if not groups:
            logger.info("[XYJS] 暂无活跃订阅用户，跳过本轮。")
            return

        logger.info(f"[XYJS] 本轮需拉取 {len(groups)} 个学校/Token 组合的帖子。")

        proxies = self.proxy if self.proxy else None
        async with httpx.AsyncClient(timeout=30.0, proxy=proxies) as client:
            for (alias, token), user_list in groups.items():
                await self.fetch_and_match(client, alias, token, user_list)

    async def fetch_and_match(self, client: httpx.AsyncClient, alias: str, token: str, user_list: list):
        """拉取某个学校的帖子并匹配用户订阅。"""
        url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        headers = self._build_headers(alias, token)

        try:
            resp = await client.post(url, headers=headers)
        except httpx.ConnectTimeout:
            logger.error(f"[XYJS] [{alias}] 请求超时。")
            return
        except Exception as e:
            logger.error(f"[XYJS] [{alias}] 请求错误: {e}")
            return

        if resp.status_code != 200:
            logger.error(f"[XYJS] [{alias}] HTTP {resp.status_code}")
            return

        data = resp.json()
        if data.get("errno", 0) != 0 or data.get("code", 200) != 200:
            err_msg = data.get("errmsg") or data.get("msg") or str(data)
            logger.error(f"[XYJS] [{alias}] API 报错: {err_msg}")
            return

        posts_data = data.get("data", {})
        posts = posts_data.get("list", []) if isinstance(posts_data, dict) else (posts_data if isinstance(posts_data, list) else [])

        if not posts:
            logger.warning(f"[XYJS] [{alias}] 帖子列表为空，Token 可能已过期。")
            return

        logger.info(f"[XYJS] [{alias}] 成功拉取 {len(posts)} 条帖子。")

        # 每个 alias+token 组合有独立的已处理集合
        group_key = f"{alias}:{token[:8]}"
        if group_key not in self.processed_post_ids:
            self.processed_post_ids[group_key] = set()
        processed = self.processed_post_ids[group_key]

        new_count = 0
        for post in reversed(posts):
            post_id = post.get("thread_id")
            if post_id in processed:
                continue

            new_count += 1
            title = post.get("title", "")
            content = post.get("content", "")
            post_text = f"标题: {title}\n内容: {content}"

            logger.info(f"[XYJS] [{alias}] 新帖 ID={post_id}: {title[:30]}")

            # 遍历此组下每个用户的订阅
            for session_str, subs in user_list:
                matched = [s for s in subs if s in post_text]
                if matched:
                    logger.info(f"[XYJS] ✅ 命中用户 {session_str[:30]} 的订阅: {matched}")
                    await self.analyze_and_push(post_text, title, content, matched, session_str)

            processed.add(post_id)

        if new_count == 0:
            logger.info(f"[XYJS] [{alias}] 本轮无新帖子。")
        else:
            logger.info(f"[XYJS] [{alias}] 本轮处理了 {new_count} 条新帖子。")

    # ──────────────── LLM 分析与推送 ────────────────

    async def analyze_and_push(self, post_text: str, title: str, content: str, matched_subs: list, session_str: str):
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
                logger.info(f"[XYJS] 已向 {session_str[:30]} 推送。")

        except Exception as e:
            logger.error(f"[XYJS] LLM 分析或推送出错: {e}")

    # ──────────────── 用户指令 ────────────────

    @filter.command("xy")
    async def cmd_xy_help(self, event: AstrMessageEvent):
        """校园集市插件帮助。"""
        yield event.plain_result(
            "📚 校园集市插件 (XYJS) 帮助\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📌 指令列表：\n"
            "  /xybind <学校代码> <Token>\n"
            "    绑定您的学校和 Token\n"
            "  /xysub <关键词>\n"
            "    订阅关键词（需先绑定）\n"
            "  /xyunsub <关键词>\n"
            "    取消订阅\n"
            "  /xylist\n"
            "    查看绑定信息和订阅列表\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🔑 Token 获取方式：\n"
            "  1. 在电脑上打开微信，搜索并打开「校园集市」小程序\n"
            "  2. 使用抓包工具（如 Charles / Fiddler）\n"
            "  3. 在小程序中随意浏览，找到发往 api.x.zanao.com 的请求\n"
            "  4. 复制请求头中 X-Sc-Od 的值 → 这就是 Token\n"
            "  5. 复制请求头中 X-Sc-Alias 的值 → 这就是学校代码\n"
            "\n"
            "💡 示例：/xybind neu ZjdmVWs3Vm1n..."
        )

    @filter.command("xybind")
    async def cmd_xybind(self, event: AstrMessageEvent, args: str):
        """绑定学校代码和 Token。用法: /xybind <学校代码> <Token>
        
        学校代码示例: neu(东北大学)、scu(四川大学)、pku(北京大学) 等。
        Token: 微信小程序版校园集市抓包获取的 X-Sc-Od 值。"""
        parts = args.strip().split(None, 1)
        if len(parts) < 2:
            yield event.plain_result(
                "❌ 格式错误！\n"
                "用法: /xybind <学校代码> <Token>\n"
                "示例: /xybind neu ZjdmVWs3Vm1n...\n\n"
                "学校代码就是抓包时 X-Sc-Alias 的值（如 neu、scu 等）。\n"
                "Token 是 X-Sc-Od 的值。"
            )
            return

        alias = parts[0].strip().lower()
        token = parts[1].strip()

        session_str = str(event.session)
        if session_str not in self.user_data:
            self.user_data[session_str] = {"alias": "", "token": "", "subs": []}
        
        self.user_data[session_str]["alias"] = alias
        self.user_data[session_str]["token"] = token
        self._save_data()

        yield event.plain_result(
            f"✅ 绑定成功！\n"
            f"学校代码: {alias}\n"
            f"Token: {token[:15]}...\n\n"
            f"现在可以使用 /xysub <关键词> 订阅感兴趣的内容了。"
        )

    @filter.command("xysub")
    async def cmd_xysub(self, event: AstrMessageEvent, keyword: str):
        """订阅校园集市关键词。用法: /xysub 外卖"""
        session_str = str(event.session)
        data = self.user_data.get(session_str)

        if not data or not data.get("alias") or not data.get("token"):
            yield event.plain_result(
                "❌ 您还没有绑定学校和 Token！\n"
                "请先使用: /xybind <学校代码> <Token>\n"
                "示例: /xybind neu ZjdmVWs3Vm1n..."
            )
            return

        subs = data.setdefault("subs", [])
        if keyword in subs:
            yield event.plain_result(f"您已经订阅过 '{keyword}' 啦！")
            return

        subs.append(keyword)
        self._save_data()
        yield event.plain_result(
            f"✅ 成功订阅：'{keyword}'\n"
            f"学校: {data['alias']} | 共 {len(subs)} 条订阅\n"
            f"有匹配帖子时会自动推送到本会话。"
        )

    @filter.command("xyunsub")
    async def cmd_xyunsub(self, event: AstrMessageEvent, keyword: str):
        """取消订阅。用法: /xyunsub 外卖"""
        session_str = str(event.session)
        data = self.user_data.get(session_str)
        subs = data.get("subs", []) if data else []

        if keyword not in subs:
            yield event.plain_result(f"未找到您的订阅 '{keyword}'。")
            return

        subs.remove(keyword)
        self._save_data()
        yield event.plain_result(f"✅ 已取消订阅：'{keyword}'")

    @filter.command("xylist")
    async def cmd_xylist(self, event: AstrMessageEvent):
        """查看自己的绑定信息和订阅列表。"""
        session_str = str(event.session)
        data = self.user_data.get(session_str)

        if not data or (not data.get("alias") and not data.get("subs")):
            yield event.plain_result(
                "您当前没有任何绑定或订阅。\n"
                "使用 /xybind <学校代码> <Token> 绑定。\n"
                "使用 /xysub <关键词> 添加订阅。"
            )
            return

        alias = data.get("alias", "未绑定")
        token = data.get("token", "")
        subs = data.get("subs", [])

        msg = f"📋 您的校园集市信息：\n"
        msg += f"  🏫 学校: {alias}\n"
        msg += f"  🔑 Token: {'已绑定 (' + token[:10] + '...)' if token else '未绑定'}\n"
        if subs:
            msg += f"  📌 订阅 ({len(subs)} 条)：\n"
            for idx, sub in enumerate(subs, 1):
                msg += f"    {idx}. {sub}\n"
        else:
            msg += f"  📌 订阅: 暂无\n"
        yield event.plain_result(msg.strip())
