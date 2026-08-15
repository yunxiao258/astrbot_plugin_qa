# -*- coding: utf-8 -*-
"""AstrBot 自定义问答插件：关键词自动回复。

- 通过 `/问答 添加 <问题> = <答案>` 维护问答库（默认按会话/群隔离，可全局共享）
- 普通消息命中问题后自动回复对应答案（精确/包含匹配、first/random、冷却防刷）
- 数据持久化到 plugin_data 目录，损坏时自动重置，不影响运行
"""

import json
import os
import random
import re
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_qa"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "自定义问答：关键词自动回复"
PLUGIN_VERSION = "1.0.0"

# 消息监听装饰器：不同 AstrBot 版本 API 不同，统一为监听所有消息
if hasattr(filter, "on_message"):
    _on_message = filter.on_message()
else:
    _on_message = filter.event_message_type(filter.EventMessageType.ALL)

# 匹配指令本身的正则（兼容带/不带斜杠、全角斜杠）
CMD_PATTERN = re.compile(r"^[\\/／]?\s*问答")


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
        """从磁盘加载问答库。文件不存在返回空；格式损坏/结构异常时重置，不影响运行"""
        self._data = {}
        self._cooldown = {}
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
        except (json.JSONDecodeError, OSError, TypeError) as e:
            logger.warning("【%s】问答数据损坏（%s），已重置", PLUGIN_NAME, e)
            self._data = {}

    def _save(self):
        """保存问答库到磁盘，失败仅告警不影响运行"""
        try:
            os.makedirs(self.data_dir, exist_ok=True)
            with open(self._data_file(), "w", encoding="utf-8") as f:
                json.dump({"data": self._data}, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning("【%s】保存问答数据失败: %s", PLUGIN_NAME, e)

    # ========== 核心逻辑 ==========

    def _session_key(self, event) -> str:
        """会话 key：全局共享时为固定值，否则按会话隔离"""
        if self._cfg_bool("qa_global_shared", False):
            return "__global__"
        return getattr(event, "unified_msg_origin", None) or str(event.session)

    def _match(self, text: str, items: list) -> list:
        """按匹配模式筛选命中项。问题长度小于 qa_min_length 的不参与匹配（防误触发）"""
        mode = self._cfg_str("qa_match_mode", "contains")
        min_len = self._cfg_int("qa_min_length", 2)
        hits = []
        for it in items:
            q = it["q"]
            if len(q) < min_len:
                continue
            if mode == "exact":
                if text == q:
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
        ck = f"{self._session_key(event)}|{item['q']}"
        now = time.time()
        cd = self._cfg_int("qa_cooldown_seconds", 10)
        if now - self._cooldown.get(ck, 0) < cd:
            return None
        self._cooldown[ck] = now
        return event.chain_result([Plain(item["a"])])

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
        self._save()
        return event.chain_result([Plain(f"✅ 已清空当前会话 {count} 条问答")])

    @staticmethod
    def _help_text() -> str:
        return (
            "📚 自定义问答插件\n"
            "├ /问答 添加 <问题> = <答案>   添加问答\n"
            "├ /问答 删 <编号>            删除问答\n"
            "├ /问答 列表                查看当前会话问答\n"
            "└ /问答 清空                清空当前会话问答\n"
            "添加后，会话内消息命中问题将自动回复对应答案。"
        )
