import asyncio
import base64
import contextlib
import json
import re
import httpx
import time
import random
import hashlib
from collections import OrderedDict
from traceback import format_exc

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

SUBS_FILENAME = "user_subs.json"
PROCESSED_FILENAME = "processed_ids.json"
MAX_PROCESSED_PER_GROUP = 100
MAX_LLM_CONCURRENCY = 3

@register("astrbot_plugin_xyjs", "NACloud3", "校园集市自定义意图订阅推送插件，支持多学校", "2.3.0")
class ZanaoXYJSPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.interval = self.config.get("interval_minutes", 10)
        self.proxy = self.config.get("proxy", "")

        self.is_running = False
        self.task: asyncio.Task = None
        self._llm_semaphore = asyncio.Semaphore(MAX_LLM_CONCURRENCY)

        # 使用框架规范目录 data/plugin_data/astrbot_plugin_xyjs/
        self._data_dir = StarTools.get_data_dir("astrbot_plugin_xyjs")
        self._subs_path = self._data_dir / SUBS_FILENAME
        self._processed_path = self._data_dir / PROCESSED_FILENAME

        # 多用户数据: { session_str: { "alias": "...", "token_b64": "...", "subs": [...] } }
        self.user_data: dict[str, dict] = {}
        self._load_data()

        # 有序去重记录: { group_key: [id1, id2, ...] } — 列表保持插入顺序
        self.processed_post_ids: dict[str, list] = {}
        self._load_processed()

    # ──────────────── Token 脱敏存储 ────────────────

    @staticmethod
    def _encode_token(token: str) -> str:
        return base64.b64encode(token.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_token(token_b64: str) -> str:
        try:
            return base64.b64decode(token_b64.encode("ascii")).decode("utf-8")
        except Exception:
            return token_b64  # 兼容旧版明文

    # ──────────────── 数据持久化 ────────────────

    def _load_data(self):
        if self._subs_path.exists():
            try:
                raw = json.loads(self._subs_path.read_text("utf-8"))
                for k, v in raw.items():
                    if isinstance(v, list):
                        self.user_data[k] = {"alias": "", "token_b64": "", "subs": v}
                    elif isinstance(v, dict):
                        # 兼容旧版 "token" 字段 -> 迁移为 "token_b64"
                        if "token" in v and "token_b64" not in v:
                            v["token_b64"] = self._encode_token(v.pop("token"))
                        self.user_data[k] = v
                logger.info(f"[XYJS] 已加载 {len(self.user_data)} 个用户的数据。")
            except json.JSONDecodeError:
                logger.error("[XYJS] user_subs.json 格式损坏，已重置。")
                self.user_data = {}
            except PermissionError:
                logger.error("[XYJS] 无权读取 user_subs.json。")
                self.user_data = {}
            except Exception as e:
                logger.error(f"[XYJS] 加载数据文件失败 ({type(e).__name__}): {e}")
                self.user_data = {}

    def _save_data(self):
        try:
            self._subs_path.write_text(
                json.dumps(self.user_data, ensure_ascii=False, indent=2), "utf-8"
            )
        except Exception as e:
            logger.error(f"[XYJS] 保存数据文件失败: {e}")

    def _load_processed(self):
        if self._processed_path.exists():
            try:
                raw = json.loads(self._processed_path.read_text("utf-8"))
                # 加载为有序列表
                self.processed_post_ids = {k: list(v) for k, v in raw.items()}
                total = sum(len(v) for v in self.processed_post_ids.values())
                logger.info(f"[XYJS] 已加载 {total} 条已处理帖子记录。")
            except Exception as e:
                logger.error(f"[XYJS] 加载已处理记录失败: {e}")
                self.processed_post_ids = {}

    def _save_processed(self):
        try:
            # 每组仅保留最近 N 条（列表有序，直接取尾部）
            trimmed = {k: v[-MAX_PROCESSED_PER_GROUP:] for k, v in self.processed_post_ids.items()}
            self._processed_path.write_text(
                json.dumps(trimmed, ensure_ascii=False), "utf-8"
            )
        except Exception as e:
            logger.error(f"[XYJS] 保存已处理记录失败: {e}")

    # ──────────────── 生命周期 ────────────────

    async def initialize(self):
        self.is_running = True
        self.task = asyncio.create_task(self.check_loop())
        active = sum(1 for d in self.user_data.values() if d.get("token_b64") and d.get("subs"))
        logger.info(f"[XYJS] 校园集市监控已启动，间隔 {self.interval} 分钟，{active} 个活跃用户。")

    async def terminate(self):
        self.is_running = False
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
        self._save_processed()
        logger.info("[XYJS] 校园集市监控已停止。")

    # ──────────────── 签名与请求 ────────────────

    @staticmethod
    def _build_headers(alias: str, token: str) -> dict:
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

    @staticmethod
    def _make_group_key(alias: str, token: str) -> str:
        """用 alias + token 的 SHA256 前 16 位作为分组键，避免明文和冲突。"""
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return f"{alias}:{token_hash}"

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
        groups: dict[tuple[str, str], list[tuple[str, list[str]]]] = {}
        for session_str, data in self.user_data.items():
            alias = data.get("alias", "")
            token_b64 = data.get("token_b64", "")
            subs = data.get("subs", [])
            if not alias or not token_b64 or not subs:
                continue
            token = self._decode_token(token_b64)
            key = (alias, token)
            groups.setdefault(key, []).append((session_str, subs))

        if not groups:
            logger.info("[XYJS] 暂无活跃订阅用户，跳过本轮。")
            return

        logger.info(f"[XYJS] 本轮需拉取 {len(groups)} 个学校/Token 组合。")

        proxies = self.proxy if self.proxy else None
        async with httpx.AsyncClient(timeout=30.0, proxy=proxies) as client:
            for (alias, token), user_list in groups.items():
                # 单组异常隔离：一个学校出错不影响其他学校
                try:
                    await self.fetch_and_match(client, alias, token, user_list)
                except Exception as e:
                    logger.error(f"[XYJS] [{alias}] 处理时出错，已跳过: {e}")

        self._save_processed()

    async def fetch_and_match(self, client: httpx.AsyncClient, alias: str, token: str, user_list: list):
        url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        headers = self._build_headers(alias, token)

        # 网络重试：覆盖所有超时类型（ConnectTimeout、ReadTimeout 等）
        resp = None
        for attempt in range(2):
            try:
                resp = await client.post(url, headers=headers)
                break
            except httpx.TimeoutException:
                if attempt == 1:
                    logger.error(f"[XYJS] [{alias}] 连续 2 次超时，跳过。")
                    return
                logger.warning(f"[XYJS] [{alias}] 超时，5 秒后重试...")
                await asyncio.sleep(5)
                headers = self._build_headers(alias, token)
            except Exception as e:
                logger.error(f"[XYJS] [{alias}] 请求错误: {e}")
                return

        if resp is None or resp.status_code != 200:
            logger.error(f"[XYJS] [{alias}] HTTP {resp.status_code if resp else 'N/A'}")
            return

        # 解析 JSON，防止非 JSON 响应（如网关错误页）
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            logger.error(f"[XYJS] [{alias}] 响应非 JSON: {resp.text[:200]}")
            return

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

        group_key = self._make_group_key(alias, token)
        if group_key not in self.processed_post_ids:
            self.processed_post_ids[group_key] = []
        processed = self.processed_post_ids[group_key]
        processed_set = set(processed)  # 查询用集合

        new_count = 0
        llm_tasks = []

        for post in reversed(posts):
            post_id = post.get("thread_id")

            # post_id 校验：缺失时跳过
            if not post_id:
                logger.warning(f"[XYJS] [{alias}] 发现无 thread_id 的帖子，已跳过。")
                continue

            if post_id in processed_set:
                continue

            new_count += 1
            title = post.get("title", "")
            content = post.get("content", "")
            post_text = f"标题: {title}\n内容: {content}"

            logger.info(f"[XYJS] [{alias}] 新帖 ID={post_id}: {title[:30]}")

            for session_str, subs in user_list:
                matched = [s for s in subs if s in post_text]
                if matched:
                    logger.info(f"[XYJS] ✅ 命中用户 {session_str[:30]} 的订阅: {matched}")
                    # 受控并发调用 LLM
                    task = asyncio.create_task(
                        self._guarded_analyze(post_text, title, content, matched, session_str)
                    )
                    llm_tasks.append(task)

            # 有序追加（列表保持插入顺序 = "最近"概念）
            processed.append(post_id)
            processed_set.add(post_id)

        # 等待所有 LLM 分析完成
        if llm_tasks:
            await asyncio.gather(*llm_tasks, return_exceptions=True)

        if new_count == 0:
            logger.info(f"[XYJS] [{alias}] 本轮无新帖子。")
        else:
            logger.info(f"[XYJS] [{alias}] 本轮处理了 {new_count} 条新帖子。")

    # ──────────────── LLM 分析与推送 ────────────────

    async def _guarded_analyze(self, post_text: str, title: str, content: str, matched_subs: list, session_str: str):
        """带信号量限流的 LLM 分析入口。"""
        async with self._llm_semaphore:
            await self.analyze_and_push(post_text, title, content, matched_subs, session_str)

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

            # 非贪婪匹配首个 JSON 对象
            json_match = re.search(r'\{.*?\}', result_text, re.DOTALL)
            if not json_match:
                logger.warning(f"[XYJS] LLM 返回无法解析: {result_text[:200]}")
                return
            json_str = json_match.group(0)

            try:
                llm_decision = json.loads(json_str)
            except json.JSONDecodeError as je:
                logger.error(f"[XYJS] LLM JSON 解析失败: {je}\n原始内容: {result_text[:300]}")
                return

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
    async def cmd_xybind(self, event: AstrMessageEvent, alias: str, token: str):
        """绑定学校代码和 Token。用法: /xybind <学校代码> <Token>"""
        alias = alias.strip().lower()

        session_str = str(event.session)
        if session_str not in self.user_data:
            self.user_data[session_str] = {"alias": "", "token_b64": "", "subs": []}

        self.user_data[session_str]["alias"] = alias
        self.user_data[session_str]["token_b64"] = self._encode_token(token)
        self._save_data()

        yield event.plain_result(
            f"✅ 绑定成功！\n"
            f"学校代码: {alias}\n"
            f"Token: {token[:10]}***（已脱敏存储）\n\n"
            f"现在可以使用 /xysub <关键词> 订阅感兴趣的内容了。"
        )

    @filter.command("xysub")
    async def cmd_xysub(self, event: AstrMessageEvent, keyword: str):
        """订阅校园集市关键词。用法: /xysub 外卖"""
        # 校验关键词有效性
        keyword = keyword.strip()
        if not keyword:
            yield event.plain_result("❌ 关键词不能为空！")
            return
        if len(keyword) > 50:
            yield event.plain_result("❌ 关键词过长，请控制在 50 字以内。")
            return

        session_str = str(event.session)
        data = self.user_data.get(session_str)

        if not data or not data.get("alias") or not data.get("token_b64"):
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
        token_b64 = data.get("token_b64", "")
        subs = data.get("subs", [])

        msg = f"📋 您的校园集市信息：\n"
        msg += f"  🏫 学校: {alias}\n"
        msg += f"  🔑 Token: {'已绑定' if token_b64 else '未绑定'}\n"
        if subs:
            msg += f"  📌 订阅 ({len(subs)} 条)：\n"
            for idx, sub in enumerate(subs, 1):
                msg += f"    {idx}. {sub}\n"
        else:
            msg += f"  📌 订阅: 暂无\n"
        yield event.plain_result(msg.strip())
