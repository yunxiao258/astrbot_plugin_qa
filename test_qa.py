# -*- coding: utf-8 -*-
"""astrbot_plugin_qa 插件单元测试：添加/删除/列表/清空、会话隔离、匹配模式、
多条命中 first/random、冷却防刷、指令自身不触发、机器人消息不触发、
管理员权限、上限拒绝、配置脏值防御、数据损坏重置。"""

import asyncio
import json
import os
import random
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_qa.main import QaPlugin  # noqa: E402


class FakeSession:
    """会话替身：可转字符串（umo）"""

    def __init__(self, umo="default:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    """最小事件替身：支持消息文本、发送者、会话、角色、消息链结果"""

    def __init__(
        self,
        message_str="",
        umo="default:GroupMessage:123",
        role="member",
        sender_id="user1",
        sender_name="小明",
        self_id="",
    ):
        self.message_str = message_str
        self.session = FakeSession(umo)
        self.unified_msg_origin = umo
        self.role = role
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self.sent = []

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def chain_result(self, chain):
        return chain

    async def send(self, chain):
        self.sent.append(chain)
        return None


def make_plugin(config=None, **kwargs):
    """构造插件实例，数据目录重定向到临时目录，避免污染真实 plugin_data"""
    tmp = tempfile.mkdtemp(prefix="qa_test_")
    plugin = QaPlugin(None, config or {}, data_dir=tmp, **kwargs)
    return plugin, tmp


def run(coro):
    return asyncio.run(coro)


async def add_qa(plugin, ev, q, a):
    """向指定事件会话添加一条问答，返回回复结果"""
    ev.message_str = f"/问答 添加 {q} = {a}"
    return await plugin.qa_command(ev)


class TestAddListDelClear(unittest.TestCase):
    def setUp(self):
        self.plugin, self.tmp = make_plugin()
        self.ev = FakeEvent(umo="default:GroupMessage:123", role="admin")

    def test_add_and_list(self):
        r = run(add_qa(self.plugin, self.ev, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)
        r = run(self.plugin._cmd_list(self.ev))
        self.assertIn("1.", r[0].text)
        self.assertIn("你好", r[0].text)
        self.assertIn("你好呀", r[0].text)

    def test_add_requires_equals(self):
        r = run(self.plugin.qa_command(FakeEvent("/问答 添加 没有等号", role="admin")))
        self.assertIn("用法", r[0].text)

    def test_add_empty_parts(self):
        r = run(self.plugin.qa_command(FakeEvent("/问答 添加 = 答案", role="admin")))
        self.assertIn("不能为空", r[0].text)
        r = run(self.plugin.qa_command(FakeEvent("/问答 添加 问题 =", role="admin")))
        self.assertIn("不能为空", r[0].text)

    def test_add_too_short_question(self):
        # 默认最短长度 2，单字问题被拒绝
        r = run(add_qa(self.plugin, self.ev, "好", "好的"))
        self.assertIn("太短", r[0].text)

    def test_del(self):
        run(add_qa(self.plugin, self.ev, "问题一", "答案一"))
        run(add_qa(self.plugin, self.ev, "问题二", "答案二"))
        r = run(self.plugin.qa_command(FakeEvent("/问答 删 1", role="admin")))
        self.assertIn("已删除", r[0].text)
        lst = run(self.plugin._cmd_list(self.ev))
        self.assertNotIn("问题一", lst[0].text)
        self.assertIn("问题二", lst[0].text)

    def test_del_not_found(self):
        r = run(self.plugin.qa_command(FakeEvent("/问答 删 999", role="admin")))
        self.assertIn("未找到", r[0].text)

    def test_del_invalid_index(self):
        r = run(self.plugin.qa_command(FakeEvent("/问答 删 abc", role="admin")))
        self.assertIn("用法", r[0].text)

    def test_clear(self):
        run(add_qa(self.plugin, self.ev, "问题一", "答案一"))
        run(add_qa(self.plugin, self.ev, "问题二", "答案二"))
        r = run(self.plugin.qa_command(FakeEvent("/问答 清空", role="admin")))
        self.assertIn("已清空", r[0].text)
        lst = run(self.plugin._cmd_list(self.ev))
        self.assertIn("还没有任何问答", lst[0].text)

    def test_list_empty(self):
        r = run(self.plugin._cmd_list(self.ev))
        self.assertIn("还没有任何问答", r[0].text)

    def test_help(self):
        r = run(self.plugin.qa_command(FakeEvent("/问答", role="admin")))
        self.assertIn("自定义问答", r[0].text)
        r = run(self.plugin.qa_command(FakeEvent("/问答 未知子命令", role="admin")))
        self.assertIn("自定义问答", r[0].text)

    def test_persistence_reload(self):
        # 添加后重建插件实例（同一数据目录），数据应仍在
        run(add_qa(self.plugin, self.ev, "问题一", "答案一"))
        plugin2 = QaPlugin(None, {}, data_dir=self.tmp)
        r = run(plugin2._cmd_list(FakeEvent(umo="default:GroupMessage:123")))
        self.assertIn("问题一", r[0].text)


class TestSessionIsolation(unittest.TestCase):
    def test_session_isolated_by_default(self):
        plugin, tmp = make_plugin()
        ev_a = FakeEvent(umo="default:GroupMessage:111", role="admin")
        ev_b = FakeEvent(umo="default:GroupMessage:222", role="admin")
        run(add_qa(plugin, ev_a, "问题A", "答案A"))
        lst_b = run(plugin._cmd_list(ev_b))
        self.assertIn("还没有任何问答", lst_b[0].text)
        lst_a = run(plugin._cmd_list(ev_a))
        self.assertIn("问题A", lst_a[0].text)

    def test_global_shared(self):
        plugin, tmp = make_plugin({"qa_global_shared": True})
        ev_a = FakeEvent(umo="default:GroupMessage:111", role="admin")
        ev_b = FakeEvent(umo="default:GroupMessage:222", role="admin")
        run(add_qa(plugin, ev_a, "问题A", "答案A"))
        lst_b = run(plugin._cmd_list(ev_b))
        self.assertIn("问题A", lst_b[0].text)


class TestMatchMode(unittest.TestCase):
    def setUp(self):
        self.plugin, self.tmp = make_plugin()
        self.admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(self.plugin, self.admin, "你好", "你好呀"))
        run(add_qa(self.plugin, self.admin, "今晚吃什么", "吃火锅"))

    def test_contains_mode_default(self):
        # 默认 contains：包含即可命中
        r = run(self.plugin.on_msg(FakeEvent("呀你好吗")))
        self.assertEqual(r[0].text, "你好呀")

    def test_exact_mode(self):
        plugin, tmp = make_plugin({"qa_match_mode": "exact"})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        r = run(plugin.on_msg(FakeEvent("你好")))
        self.assertEqual(r[0].text, "你好呀")
        # 包含但不完全相等则不命中
        r = run(plugin.on_msg(FakeEvent("呀你好吗")))
        self.assertIsNone(r)

    def test_min_length_filter(self):
        plugin, tmp = make_plugin({"qa_match_mode": "contains", "qa_min_length": 3})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        # 问题长度 2 < 最短长度 3，不参与匹配
        r = run(plugin.on_msg(FakeEvent("你好呀")))
        self.assertIsNone(r)

    def test_no_hit(self):
        r = run(self.plugin.on_msg(FakeEvent("随便聊聊")))
        self.assertIsNone(r)


class TestReplyMode(unittest.TestCase):
    def setUp(self):
        # 关闭冷却，避免干扰随机性验证
        self.plugin, self.tmp = make_plugin({"qa_cooldown_seconds": 0})
        self.admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        # 两条均命中 "你好"
        run(add_qa(self.plugin, self.admin, "你好", "答案一"))
        run(add_qa(self.plugin, self.admin, "你好啊", "答案二"))

    def test_first_mode(self):
        plugin, tmp = make_plugin({"qa_reply_mode": "first"})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "答案一"))
        run(add_qa(plugin, admin, "你好啊", "答案二"))
        r = run(plugin.on_msg(FakeEvent("你好啊朋友")))
        self.assertEqual(r[0].text, "答案一")

    def test_random_mode_with_seed(self):
        # 固定 seed 下多次运行结果确定且属于命中集
        random.seed(42)
        results = set()
        for _ in range(20):
            r = run(self.plugin.on_msg(FakeEvent("你好啊朋友")))
            self.assertIsNotNone(r)
            results.add(r[0].text)
        self.assertTrue(results <= {"答案一", "答案二"})
        # 不同 seed 可能得到不同结果，证明随机性
        seeds_hit = set()
        for seed in range(30):
            random.seed(seed)
            r = run(self.plugin.on_msg(FakeEvent("你好啊朋友")))
            seeds_hit.add(r[0].text)
        self.assertEqual(len(seeds_hit), 2)


class TestCooldown(unittest.TestCase):
    def test_cooldown_suppresses_repeat(self):
        plugin, tmp = make_plugin({"qa_cooldown_seconds": 10})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        ev = FakeEvent("你好")
        with mock.patch("astrbot_plugin_qa.main.time.time", return_value=100.0):
            r1 = run(plugin.on_msg(ev))
            self.assertEqual(r1[0].text, "你好呀")
            # 同一会话同一问题，冷却期内不重复回复
            r2 = run(plugin.on_msg(ev))
            self.assertIsNone(r2)
        # 冷却期过后恢复
        with mock.patch("astrbot_plugin_qa.main.time.time", return_value=111.0):
            r3 = run(plugin.on_msg(ev))
            self.assertEqual(r3[0].text, "你好呀")

    def test_cooldown_zero_disabled(self):
        plugin, tmp = make_plugin({"qa_cooldown_seconds": 0})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        with mock.patch("astrbot_plugin_qa.main.time.time", return_value=100.0):
            r1 = run(plugin.on_msg(FakeEvent("你好")))
            self.assertEqual(r1[0].text, "你好呀")
            r2 = run(plugin.on_msg(FakeEvent("你好")))
            self.assertEqual(r2[0].text, "你好呀")


class TestNoSelfTrigger(unittest.TestCase):
    def setUp(self):
        self.plugin, self.tmp = make_plugin()
        self.admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(self.plugin, self.admin, "你好", "你好呀"))

    def test_command_itself_not_trigger(self):
        # 指令消息本身不触发自动问答
        r = run(self.plugin.on_msg(FakeEvent("/问答 列表")))
        self.assertIsNone(r)
        r = run(self.plugin.on_msg(FakeEvent("问答 添加 新问题 = 新答案")))
        self.assertIsNone(r)

    def test_bot_self_message_not_trigger(self):
        # 机器人自己的消息不触发（sender == self）
        r = run(self.plugin.on_msg(FakeEvent("你好", sender_id="bot1", self_id="bot1")))
        self.assertIsNone(r)
        # 普通用户消息正常触发
        r = run(self.plugin.on_msg(FakeEvent("你好", sender_id="user1", self_id="bot1")))
        self.assertEqual(r[0].text, "你好呀")

    def test_qa_enable_off(self):
        plugin, tmp = make_plugin({"qa_enable": False})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        r = run(plugin.on_msg(FakeEvent("你好")))
        self.assertIsNone(r)


class TestAdminPermission(unittest.TestCase):
    def test_default_admin_only_manage(self):
        # 默认 qa_admin_only_manage=True，非管理员不能管理
        plugin, tmp = make_plugin()
        member = FakeEvent(umo="default:GroupMessage:123", role="member")
        r = run(add_qa(plugin, member, "你好", "你好呀"))
        self.assertIn("权限", r[0].text)
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        r = run(add_qa(plugin, admin, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)

    def test_admin_umos_whitelist(self):
        # admin_umos 白名单内的非 admin 角色也可管理
        plugin, tmp = make_plugin({"admin_umos": "default:GroupMessage:999"})
        member_ok = FakeEvent(umo="default:GroupMessage:999", role="member")
        r = run(add_qa(plugin, member_ok, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)
        member_no = FakeEvent(umo="default:GroupMessage:888", role="member")
        r = run(add_qa(plugin, member_no, "你好", "你好呀"))
        self.assertIn("权限", r[0].text)

    def test_admin_only_manage_off(self):
        plugin, tmp = make_plugin({"qa_admin_only_manage": False})
        member = FakeEvent(umo="default:GroupMessage:123", role="member")
        r = run(add_qa(plugin, member, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)

    def test_list_free_for_all(self):
        # 列表不受管理员限制
        plugin, tmp = make_plugin()
        member = FakeEvent(umo="default:GroupMessage:123", role="member")
        r = run(plugin._cmd_list(member))
        self.assertIsNotNone(r)


class TestMaxPerSession(unittest.TestCase):
    def test_reject_when_over_limit(self):
        plugin, tmp = make_plugin({"qa_max_per_session": 2})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        r = run(add_qa(plugin, admin, "问题一", "答案一"))
        self.assertIn("已添加", r[0].text)
        r = run(add_qa(plugin, admin, "问题二", "答案二"))
        self.assertIn("已添加", r[0].text)
        r = run(add_qa(plugin, admin, "问题三", "答案三"))
        self.assertIn("上限", r[0].text)


class TestDirtyConfig(unittest.TestCase):
    def test_dirty_min_length(self):
        # 非法整数配置回退默认值，不崩溃
        plugin, tmp = make_plugin({"qa_min_length": "abc", "qa_match_mode": "contains"})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        r = run(add_qa(plugin, admin, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)
        r = run(plugin.on_msg(FakeEvent("你好呀")))
        self.assertEqual(r[0].text, "你好呀")

    def test_dirty_match_mode_fallback(self):
        # 非法匹配模式回退 contains
        plugin, tmp = make_plugin({"qa_match_mode": "garbage"})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        r = run(plugin.on_msg(FakeEvent("呀你好吗")))
        self.assertEqual(r[0].text, "你好呀")

    def test_dirty_reply_mode_fallback(self):
        # 非法回复模式回退 random
        plugin, tmp = make_plugin({"qa_reply_mode": "garbage"})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "答案一"))
        run(add_qa(plugin, admin, "你好啊", "答案二"))
        r = run(plugin.on_msg(FakeEvent("你好啊朋友")))
        self.assertIn(r[0].text, {"答案一", "答案二"})

    def test_dirty_cooldown(self):
        plugin, tmp = make_plugin({"qa_cooldown_seconds": None})
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        run(add_qa(plugin, admin, "你好", "你好呀"))
        r = run(plugin.on_msg(FakeEvent("你好")))
        self.assertEqual(r[0].text, "你好呀")

    def test_config_none(self):
        plugin, tmp = make_plugin(None)
        r = run(plugin.on_msg(FakeEvent("任意消息")))
        self.assertIsNone(r)


class TestCorruptedData(unittest.TestCase):
    def test_corrupted_json_reset(self):
        tmp = tempfile.mkdtemp(prefix="qa_test_corrupt_")
        with open(os.path.join(tmp, "qa_data.json"), "w", encoding="utf-8") as f:
            f.write("{ not valid json !!!")
        plugin = QaPlugin(None, {}, data_dir=tmp)
        self.assertEqual(plugin._data, {})
        # 重置后仍可正常使用
        admin = FakeEvent(umo="default:GroupMessage:123", role="admin")
        r = run(add_qa(plugin, admin, "你好", "你好呀"))
        self.assertIn("已添加", r[0].text)

    def test_wrong_structure_reset(self):
        tmp = tempfile.mkdtemp(prefix="qa_test_corrupt_")
        with open(os.path.join(tmp, "qa_data.json"), "w", encoding="utf-8") as f:
            json.dump({"data": "not_a_dict"}, f)
        plugin = QaPlugin(None, {}, data_dir=tmp)
        self.assertEqual(plugin._data, {})

    def test_bad_item_filtered(self):
        tmp = tempfile.mkdtemp(prefix="qa_test_corrupt_")
        with open(os.path.join(tmp, "qa_data.json"), "w", encoding="utf-8") as f:
            json.dump({
                "data": {
                    "default:GroupMessage:123": [
                        {"id": 1, "q": "好问题", "a": "好答案"},
                        {"id": 2, "q": "缺答案"},
                        "garbage",
                    ]
                }
            }, f, ensure_ascii=False)
        plugin = QaPlugin(None, {}, data_dir=tmp)
        items = plugin._data.get("default:GroupMessage:123", [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["q"], "好问题")


if __name__ == "__main__":
    unittest.main(verbosity=1)
