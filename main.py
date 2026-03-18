import asyncio
import base64
import contextlib
import json
import re
import httpx
import time
import random
import hashlib
from traceback import format_exc

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.api import logger
from astrbot.api.all import AstrBotConfig

SUBS_FILENAME = "user_subs.json"
PROCESSED_FILENAME = "processed_ids.json"
MAX_PROCESSED_PER_GROUP = 100
MAX_LLM_CONCURRENCY = 3
LLM_BATCH_SIZE = 5  # 每批最多并发的 LLM 任务数

@register("astrbot_plugin_xyjs", "NACloud3", "校园集市自定义意图订阅推送插件，支持多学校", "2.4.0")
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

        # 有序去重记录: { group_key: [id1, id2, ...] }
        self.processed_post_ids: dict[str, list] = {}
        self._load_processed()

    # ──────────────── Token 编码存储 ────────────────

    @staticmethod
    def _encode_token(token: str) -> str:
        return base64.b64encode(token.encode("utf-8")).decode("ascii")

    @staticmethod
    def _decode_token(token_b64: str) -> str:
        try:
            return base64.b64decode(token_b64.encode("ascii")).decode("utf-8")
        except Exception as e:
            logger.warning(f"[XYJS] Token 解码失败（可能是旧版明文格式），将原样使用: {e}")
            return token_b64

    # ──────────────── 数据持久化 ────────────────

    def _load_data(self):
        if self._subs_path.exists():
            try:
                raw = json.loads(self._subs_path.read_text("utf-8"))
                for k, v in raw.items():
                    if isinstance(v, list):
                        self.user_data[k] = {"alias": "", "token_b64": "", "subs": v}
                    elif isinstance(v, dict):
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
                self.processed_post_ids = {k: list(v) for k, v in raw.items()}
                total = sum(len(v) for v in self.processed_post_ids.values())
                logger.info(f"[XYJS] 已加载 {total} 条已处理帖子记录。")
            except Exception as e:
                logger.error(f"[XYJS] 加载已处理记录失败: {e}")
                self.processed_post_ids = {}

    def _save_processed(self):
        """保存并同步裁剪内存中的已处理记录。"""
        try:
            # 同步裁剪内存，防止长时间运行后不受控增长
            self.processed_post_ids = {
                k: v[-MAX_PROCESSED_PER_GROUP:] for k, v in self.processed_post_ids.items()
            }
            self._processed_path.write_text(
                json.dumps(self.processed_post_ids, ensure_ascii=False), "utf-8"
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
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        return f"{alias}:{token_hash}"

    @staticmethod
    def _extract_json(text: str) -> str | None:
        """从文本中提取首个平衡括号的 JSON 对象。"""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

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
                try:
                    await self.fetch_and_match(client, alias, token, user_list)
                except Exception as e:
                    logger.error(f"[XYJS] [{alias}] 处理出错，已跳过: {e}")

        self._save_processed()

    async def fetch_and_match(self, client: httpx.AsyncClient, alias: str, token: str, user_list: list):
        url = "https://api.x.zanao.com/thread/v2/list?with_reply=true&from_time=0&with_comment=true"
        headers = self._build_headers(alias, token)

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
        is_first_run = group_key not in self.processed_post_ids

        if is_first_run:
            # 首次遇到该组：仅建立基线，记录所有 ID 但不触发匹配推送
            self.processed_post_ids[group_key] = [
                p.get("thread_id") for p in posts if p.get("thread_id")
            ]
            logger.info(f"[XYJS] [{alias}] 首次建立基线，记录 {len(self.processed_post_ids[group_key])} 条已有帖子，下轮开始推送新帖。")
            return

        processed = self.processed_post_ids[group_key]
        processed_set = set(processed)

        # 收集需要 LLM 分析的任务参数
        pending_analyses: list[tuple[str, str, str, list, str]] = []

        new_count = 0
        for post in reversed(posts):
            post_id = post.get("thread_id")
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
                    logger.info(f"[XYJS] ✅ 命中用户 {session_str[:30]}: {matched}")
                    pending_analyses.append((post_text, title, content, matched, session_str))

            processed.append(post_id)
            processed_set.add(post_id)

        # 分批执行 LLM 分析，避免瞬时创建过多任务
        for i in range(0, len(pending_analyses), LLM_BATCH_SIZE):
            batch = pending_analyses[i:i + LLM_BATCH_SIZE]
            tasks = [
                asyncio.create_task(self._guarded_analyze(*args))
                for args in batch
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, result in enumerate(results):
                if isinstance(result, Exception):
                    logger.error(f"[XYJS] 批次 LLM 任务 {i + idx} 异常: {type(result).__name__}: {result}")

        if new_count == 0:
            logger.info(f"[XYJS] [{alias}] 本轮无新帖子。")
        else:
            logger.info(f"[XYJS] [{alias}] 本轮处理了 {new_count} 条新帖子，触发 {len(pending_analyses)} 次 LLM 分析。")

    # ──────────────── LLM 分析与推送 ────────────────

    async def _guarded_analyze(self, post_text: str, title: str, content: str, matched_subs: list, session_str: str):
        async with self._llm_semaphore:
            await self.analyze_and_push(post_text, title, content, matched_subs, session_str)

    async def analyze_and_push(self, post_text: str, title: str, content: str, matched_subs: list, session_str: str):
        # 阶段 1: 获取 LLM Provider
        try:
            provider = self.context.get_using_provider()
            if not provider:
                logger.error("[XYJS] 无法获取默认 LLM Provider。")
                return
            provider_id = provider.meta().id
        except Exception as e:
            logger.error(f"[XYJS] 获取 LLM Provider 失败: {type(e).__name__}: {e}")
            return

        # 阶段 2: 调用 LLM 生成
        try:
            prompt = (
                f"你是一个校园集市监控助手。有一个新帖子引起了用户的注意。\n"
                f"【帖子详情】\n{post_text}\n"
                f"【用户的订阅意图列表】\n{json.dumps(matched_subs, ensure_ascii=False)}\n\n"
                f"请判断该帖子是否真的符合用户的订阅意图。\n"
                f"注意：如果用户想买某物，别人求购就不符合。\n"
                f"只输出 JSON，格式：\n"
                f'{{"match": true或false, "reason": "原因", "summary": "匹配时一句话概括内容，否则留空"}}'
            )
            llm_resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
            )
            result_text = llm_resp.completion_text.strip()
        except Exception as e:
            logger.error(f"[XYJS] LLM 生成调用失败: {type(e).__name__}: {e}")
            return

        # 阶段 3: 解析 JSON（平衡括号提取）
        json_str = self._extract_json(result_text)
        if not json_str:
            logger.warning(f"[XYJS] LLM 返回无法提取 JSON: {result_text[:200]}")
            return

        try:
            llm_decision = json.loads(json_str)
        except json.JSONDecodeError as je:
            logger.error(f"[XYJS] JSON 解析失败: {je}\n提取片段: {json_str[:200]}\n原始: {result_text[:300]}")
            return

        logger.info(f"[XYJS] LLM 判定: match={llm_decision.get('match')}, reason={llm_decision.get('reason', 'N/A')}")

        # 阶段 4: 推送通知
        if llm_decision.get("match") is True:
            try:
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
                logger.error(f"[XYJS] 推送消息失败: {type(e).__name__}: {e}\n{format_exc()}")

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
            f"Token: {token[:10]}***（已编码存储）\n\n"
            f"现在可以使用 /xysub <关键词> 订阅感兴趣的内容了。"
        )

    @filter.command("xysub")
    async def cmd_xysub(self, event: AstrMessageEvent, keyword: str):
        """订阅校园集市关键词。用法: /xysub 外卖"""
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
