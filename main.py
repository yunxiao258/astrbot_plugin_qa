# -*- coding: utf-8 -*-
"""AstrBot 自定义问答插件：关键词自动回复。

- 通过 `/问答 添加 <问题> = <答案>` 维护问答库（默认按会话/群隔离，可全局共享）
- 普通消息命中问题后自动回复对应答案（精确/包含/正则/模糊匹配、first/random、冷却防刷）
- 答案支持 [img]URL[/img] 图片标记，回复时自动发送图片
- 命中统计持久化，`/问答 统计` 查看热门问答排行
- 支持 `/问答 导出` 与 `/问答 导入 <JSON>` 批量迁移问答库
- 数据持久化到 plugin_data 目录，损坏时自动重置，不影响运行
"""

import difflib
import json
import os
import random
import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_qa"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "自定义问答：关键词自动回复（正则/模糊匹配、图片回复、命中统计、导入导出）"
PLUGIN_VERSION = "1.1.0"

# 消息监听装饰器：不同 AstrBot 版本 API 不同，统一为监听所有消息
if hasattr(filter, "on_message"):
    _on_message = filter.on_message()
else:
    _on_message = filter.event_message_type(filter.EventMessageType.ALL)

# 匹配指令本身的正则（兼容带/不带斜杠、全角斜杠）
CMD_PATTERN = re.compile(r"^[\\/／]?\s*问答")

# 答案中的图片标记 [img]URL[/img]
IMG_TAG = re.compile(r"\[img\](https?://[^\s\[\]]+)\[/img\]", re.IGNORECASE)

# 导入导出 JSON 大小上限（字符数），防止刷屏
EXPORT_MAX_CHARS = 3000


@register(PLUGIN_NAME, PLUGIN_AUTHOR, PLUGIN_DESC, PLUGIN_VERSION)
class QaPlugin(Star):
    """自定义问答：关键词自动回复"""

    def __init__(self, context: Context, config: AstrBotConfig = None, data_dir: str = None):
        super().__init__(context)
        self.config = config or {}
        # 数据目录：默认 plugin_data/<插件名>，测试可注入临时目录
        self.data_dir = data_dir or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # 会话 key -> 问答列表（[{id, q, a}]）
        self._data: dict[str, list[dict]] = {}
        # 冷却记录：f"{session}|{question}" -> 上次回复时间戳
        self._cooldown: dict[str, float] = {}
        # 命中统计：f"{session}|{id}" -> 次数
        self._stats: dict[str, int] = {}
        self._load()
        logger.info(
            f"【{PLUGIN_NAME}】插件初始化完成，共加载 "
            f"{sum(len(v) for v in self._data.values())} 条问答"
        )

    # ========== 配置读取（脏值防御） ==========

    def _cfg_bool(self, key: str, default: bool) -> bool:
        """读取布尔配置，非 bool 值按字符串解析，无法解析时回退默认值"""
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return default

    def _cfg_int(self, key: str, default: int) -> int:
        """读取整数配置，脏值（None/字符串/非法）回退默认值"""
        v = self.config.get(key, default)
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        """读取字符串配置，脏值回退默认值"""
        v = self.config.get(key, default)
        if isinstance(v, str) and v.strip():
            return v.strip()
        return default

    # ========== 管理员判断 ==========

    def _admin_umos(self) -> list:
        """解析 admin_umos 白名单为列表"""
        v = self.config.get("admin_umos", "")
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return list(v or [])

    def _is_admin(self, event) -> bool:
        """管理员：event.role == admin，或会话 UMO 在 admin_umos 白名单内"""
        if str(getattr(event, "role", "")).lower() == "admin":
            return True
        umos = self._admin_umos()
        if not umos:
            return False
        umo = getattr(event, "unified_msg_origin", None) or str(event.session)
        return umo in umos

    def _deny(self) -> str:
        return "❌ 你没有权限执行此操作（仅管理员可管理问答库）"

    # ========== 持久化 ==========

    def _data_file(self) -> str:
        return os.path.join(self.data_dir, "qa_data.json")

    def _load(self):
        """从磁盘加载问答库与命中统计。文件不存在返回空；格式损坏/结构异常时重置，不影响运行"""
        self._data = {}
        self._cooldown = {}
        self._stats = {}
        try:
            path = self._data_file()
            if not os.path.exists(path):
                return
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                logger.warning("【%s】问答数据格式异常，已重置", PLUGIN_NAME)
                return
            data = raw.get("data", {})
            if not isinstance(data, dict):
                logger.warning("【%s】问答数据结构异常，已重置", PLUGIN_NAME)
                return
            for key, items in data.items():
                if not isinstance(items, list):
                    continue
                cleaned = []
                for it in items:
                    # 校验条目结构，丢弃坏数据
                    if (
                        isinstance(it, dict)
                        and isinstance(it.get("q"), str)
                        and isinstance(it.get("a"), str)
                    ):
                        cleaned.append({
                            "id": it.get("id", 0),
                            "q": it["q"],
                            "a": it["a"],
                        })
                if cleaned:
                    self._data[str(key)] = cleaned
            stats = raw.get("stats", {})
            if isinstance(stats, dict):
                self._stats = {
                    str(k): int(v) for k, v in stats.items()
                    if isinstance(v, (int, float)) and v >= 0
                }
        except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
            logger.warning("【%s】问答数据损坏（%s），已重置", PLUGIN_NAME, e)
            self._data = {}
            self._stats = {}

    def _save(self):
        """保存问答库与命中统计到磁盘，失败仅告警不影响运行"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            tmp = self._data_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(
                    {"data": self._data, "stats": self._stats},
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
            os.replace(tmp, self._data_file())
        except OSError as e:
            logger.warning("【%s】保存问答数据失败: %s", PLUGIN_NAME, e)

    def _record_hit(self, session_key: str, item_id: int):
        """记录一次问答命中（统计开关开启时）并立即持久化，失败不影响回复"""
        if not self._cfg_bool("qa_stats_enabled", True):
            return
        ck = f"{session_key}|{item_id}"
        self._stats[ck] = self._stats.get(ck, 0) + 1
        self._save()

    # ========== 核心逻辑 ==========

    def _session_key(self, event) -> str:
        """会话 key：全局共享时为固定值，否则按会话隔离"""
        if self._cfg_bool("qa_global_shared", False):
            return "__global__"
        return getattr(event, "unified_msg_origin", None) or str(event.session)

    def _fuzzy_threshold(self) -> float:
        """模糊匹配相似度阈值（0.1~1.0），脏值回退默认 0.85"""
        try:
            v = float(self.config.get("qa_fuzzy_threshold", 0.85))
        except (TypeError, ValueError):
            return 0.85
        return min(1.0, max(0.1, v))

    def _match(self, text: str, items: list) -> list:
        """按匹配模式筛选命中项。问题长度小于 qa_min_length 的不参与匹配（防误触发）

        模式：exact 精确 / contains 包含 / regex 正则 / fuzzy 相似度
        """
        mode = self._cfg_str("qa_match_mode", "contains")
        min_len = self._cfg_int("qa_min_length", 2)
        hits = []
        for it in items:
            q = it["q"]
            if len(q) < min_len and mode != "regex":
                continue
            if mode == "exact":
                if text == q:
                    hits.append(it)
            elif mode == "regex":
                # 问题作为正则表达式；非法正则跳过不崩溃
                try:
                    if q and re.search(q, text):
                        hits.append(it)
                except re.error:
                    continue
            elif mode == "fuzzy":
                # 相似度匹配：消息与问题相似度达到阈值即命中
                if not text or not q:
                    continue
                ratio = difflib.SequenceMatcher(None, text, q).ratio()
                if ratio >= self._fuzzy_threshold():
                    hits.append(it)
            else:
                # 默认 contains 包含匹配
                if q in text:
                    hits.append(it)
        return hits

    def _pick(self, hits: list) -> dict:
        """多条命中时按 qa_reply_mode 选择：first 取第一条，random 随机取一条"""
        if self._cfg_str("qa_reply_mode", "random") == "first":
            return hits[0]
        return random.choice(hits)

    @_on_message
    async def on_msg(self, event) -> None:
        """消息监听：普通消息命中问答库则自动回复（每收到消息触发）"""
        # 总开关
        if not self._cfg_bool("qa_enable", True):
            return None
        # 机器人自己的消息不触发
        self_id = event.get_self_id()
        sender_id = event.get_sender_id()
        if self_id and sender_id and self_id == sender_id:
            return None
        text = event.message_str or ""
        text = text.strip()
        if not text:
            return None
        # 指令本身（以唤醒前缀如 / 开头或以注册指令名开头）不触发问答
        if CMD_PATTERN.match(text):
            return None

        items = self._data.get(self._session_key(event), [])
        if not items:
            return None
        hits = self._match(text, items)
        if not hits:
            return None

        item = self._pick(hits)
        # 冷却防刷：同会话命中同一问题在间隔内不重复回复
        session_key = self._session_key(event)
        ck = f"{session_key}|{item['q']}"
        now = time.time()
        cd = self._cfg_int("qa_cooldown_seconds", 10)
        if now - self._cooldown.get(ck, 0) < cd:
            return None
        self._cooldown[ck] = now
        # 命中统计
        self._record_hit(session_key, item["id"])
        return event.chain_result(self._build_reply(item["a"]))

    @staticmethod
    def _build_reply(answer: str) -> list:
        """把答案构造为消息组件列表：支持 [img]URL[/img] 图片标记，其余为文本"""
        components = []
        last = 0
        for m in IMG_TAG.finditer(answer):
            text_before = answer[last:m.start()]
            if text_before.strip():
                components.append(Plain(text_before))
            try:
                components.append(Image.fromURL(m.group(1)))
            except Exception:  # noqa: BLE001
                # URL 非法时降级为原文文本
                components.append(Plain(m.group(0)))
            last = m.end()
        rest = answer[last:]
        if rest.strip() or not components:
            components.append(Plain(rest))
        return components

    # ========== 指令处理 ==========

    @filter.command("问答")
    async def qa_command(self, event) -> None:
        """问答管理指令：/问答 添加/删/列表/清空"""
        text = event.message_str.strip()
        m = re.match(r"^[\\/／]?\s*问答\s*(.*)$", text)
        rest = (m.group(1) or "").strip() if m else ""
        if not rest:
            return event.chain_result([Plain(self._help_text())])
        # 子命令分发
        if rest.startswith("添加"):
            return await self._cmd_add(event, rest[2:].strip())
        if rest.startswith("删"):
            return await self._cmd_del(event, rest[1:].strip())
        if rest.startswith("列表"):
            return await self._cmd_list(event)
        if rest.startswith("清空"):
            return await self._cmd_clear(event)
        if rest.startswith("统计"):
            return await self._cmd_stats(event)
        if rest.startswith("导出"):
            return await self._cmd_export(event)
        if rest.startswith("导入"):
            return await self._cmd_import(event, rest[2:].strip())
        return event.chain_result([Plain(self._help_text())])

    def _check_manage_permission(self, event):
        """管理操作权限检查：qa_admin_only_manage 开启时仅管理员"""
        if not self._cfg_bool("qa_admin_only_manage", True):
            return True
        return self._is_admin(event)

    async def _cmd_add(self, event, args: str) -> None:
        """添加问答：<问题> = <答案>"""
        if not self._check_manage_permission(event):
            return event.chain_result([Plain(self._deny())])
        if "=" not in args:
            return event.chain_result([Plain("❌ 用法: /问答 添加 <问题> = <答案>")])
        q, _, a = args.partition("=")
        q = q.strip()
        a = a.strip()
        if not q or not a:
            return event.chain_result([Plain("❌ 问题和答案都不能为空，用法: /问答 添加 <问题> = <答案>")])
        min_len = self._cfg_int("qa_min_length", 2)
        if len(q) < min_len:
            return event.chain_result([Plain(f"❌ 问题太短，至少 {min_len} 个字符")])
        key = self._session_key(event)
        items = self._data.setdefault(key, [])
        # 每会话问答上限
        max_items = self._cfg_int("qa_max_per_session", 100)
        if len(items) >= max_items:
            return event.chain_result([Plain(f"❌ 当前会话问答已达上限（{max_items} 条），请先删除再添加")])
        new_id = max([it["id"] for it in items], default=0) + 1
        items.append({"id": new_id, "q": q, "a": a})
        self._save()
        return event.chain_result([Plain(f"✅ 已添加问答 #{new_id}：{q} => {a}")])

    async def _cmd_del(self, event, args: str) -> None:
        """删除问答：<编号>"""
        if not self._check_manage_permission(event):
            return event.chain_result([Plain(self._deny())])
        if not args.isdigit():
            return event.chain_result([Plain("❌ 用法: /问答 删 <编号>（编号可通过 /问答 列表 查看）")])
        idx = int(args)
        key = self._session_key(event)
        items = self._data.get(key, [])
        for it in items:
            if it["id"] == idx:
                items.remove(it)
                self._save()
                return event.chain_result([Plain(f"✅ 已删除问答 #{idx}")])
        return event.chain_result([Plain(f"❌ 未找到编号为 {idx} 的问答")])

    async def _cmd_list(self, event) -> None:
        """列出当前会话问答"""
        key = self._session_key(event)
        items = self._data.get(key, [])
        if not items:
            return event.chain_result([Plain("📭 当前会话还没有任何问答，用 /问答 添加 <问题> = <答案> 添加吧")])
        lines = [f"📋 当前会话问答（共 {len(items)} 条）"]
        for it in items:
            lines.append(f"{it['id']}. {it['q']} => {it['a']}")
        return event.chain_result([Plain("\n".join(lines))])

    async def _cmd_clear(self, event) -> None:
        """清空当前会话问答"""
        if not self._check_manage_permission(event):
            return event.chain_result([Plain(self._deny())])
        key = self._session_key(event)
        count = len(self._data.get(key, []))
        self._data.pop(key, None)
        # 同步清理该会话的命中统计
        prefix = f"{key}|"
        self._stats = {k: v for k, v in self._stats.items() if not k.startswith(prefix)}
        self._save()
        return event.chain_result([Plain(f"✅ 已清空当前会话 {count} 条问答")])

    async def _cmd_stats(self, event) -> None:
        """查看当前会话问答命中排行（Top 10）"""
        key = self._session_key(event)
        items = self._data.get(key, [])
        id_map = {it["id"]: it for it in items}
        rows = []
        prefix = f"{key}|"
        for ck, cnt in self._stats.items():
            if not ck.startswith(prefix):
                continue
            try:
                qid = int(ck[len(prefix):])
            except ValueError:
                continue
            it = id_map.get(qid)
            if it:
                rows.append((cnt, it))
        if not rows:
            return event.chain_result(
                [Plain("📊 当前会话还没有命中记录。问答被触发后会自动统计，稍后再来看看吧")]
            )
        rows.sort(key=lambda x: -x[0])
        lines = [f"📊 当前会话问答命中排行（共 {len(rows)} 条有记录）"]
        medals = ["🥇", "🥈", "🥉"]
        for i, (cnt, it) in enumerate(rows[:10]):
            icon = medals[i] if i < 3 else f"{i + 1}."
            q_short = it["q"] if len(it["q"]) <= 20 else it["q"][:19] + "…"
            lines.append(f"{icon} [{cnt} 次] {q_short}")
        total = sum(cnt for cnt, _ in rows)
        lines.append(f"累计命中 {total} 次")
        return event.chain_result([Plain("\n".join(lines))])

    async def _cmd_export(self, event) -> None:
        """导出当前会话问答为 JSON 文本（可直接用于导入）"""
        key = self._session_key(event)
        items = self._data.get(key, [])
        if not items:
            return event.chain_result([Plain("📭 当前会话没有任何问答可导出")])
        payload = json.dumps(
            [{"q": it["q"], "a": it["a"]} for it in items],
            ensure_ascii=False,
        )
        if len(payload) > EXPORT_MAX_CHARS:
            return event.chain_result([
                Plain(
                    f"❌ 问答库过大（{len(items)} 条，{len(payload)} 字符），"
                    "无法直接发送文本。请减少条数后重试"
                )
            ])
        text = f"📤 导出成功（{len(items)} 条）。复制下方内容，用 /问答 导入 <JSON> 迁移：\n{payload}"
        return event.chain_result([Plain(text)])

    async def _cmd_import(self, event, args: str) -> None:
        """从 JSON 文本导入问答（合并到当前会话，重复问题跳过）"""
        if not self._check_manage_permission(event):
            return event.chain_result([Plain(self._deny())])
        raw = args.strip()
        if not raw:
            return event.chain_result([Plain("❌ 用法: /问答 导入 <JSON>（JSON 可通过 /问答 导出 获取）")])
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return event.chain_result([Plain(f"❌ JSON 解析失败: {e}")])
        if not isinstance(parsed, list):
            return event.chain_result([Plain("❌ 格式错误：应为数组，如 [{\"q\": \"问题\", \"a\": \"答案\"}]")])
        min_len = self._cfg_int("qa_min_length", 2)
        key = self._session_key(event)
        items = self._data.setdefault(key, [])
        existing_qs = {it["q"] for it in items}
        added, skipped_dup, skipped_bad = 0, 0, 0
        for row in parsed:
            if not isinstance(row, dict):
                skipped_bad += 1
                continue
            q = str(row.get("q", "")).strip()
            a = str(row.get("a", "")).strip()
            if not q or not a or len(q) < min_len:
                skipped_bad += 1
                continue
            if q in existing_qs:
                skipped_dup += 1
                continue
            max_items = self._cfg_int("qa_max_per_session", 100)
            if len(items) >= max_items:
                break
            new_id = max([it["id"] for it in items], default=0) + 1
            items.append({"id": new_id, "q": q, "a": a})
            existing_qs.add(q)
            added += 1
        self._save()
        msg = f"✅ 导入完成：新增 {added} 条"
        if skipped_dup:
            msg += f"，重复跳过 {skipped_dup} 条"
        if skipped_bad:
            msg += f"，无效跳过 {skipped_bad} 条"
        return event.chain_result([Plain(msg)])

    @staticmethod
    def _help_text() -> str:
        return (
            "📚 自定义问答插件\n"
            "├ /问答 添加 <问题> = <答案>   添加问答（答案支持 [img]图片URL[/img]）\n"
            "├ /问答 删 <编号>            删除问答\n"
            "├ /问答 列表                查看当前会话问答\n"
            "├ /问答 统计                查看命中排行 Top10\n"
            "├ /问答 导出                导出当前会话问答 JSON\n"
            "├ /问答 导入 <JSON>         批量导入问答\n"
            "└ /问答 清空                清空当前会话问答\n"
            "添加后，会话内消息命中问题将自动回复对应答案。"
        )
