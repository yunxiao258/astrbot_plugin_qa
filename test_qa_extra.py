# -*- coding: utf-8 -*-
"""astrbot_plugin_qa 新功能测试：正则/模糊匹配、图片回复、命中统计、导入导出。"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, r"D:\astrbot\data\plugins")

from astrbot_plugin_qa.main import QaPlugin  # noqa: E402


class FakeSession:
    def __init__(self, umo="default:GroupMessage:123"):
        self.umo = umo

    def __str__(self):
        return self.umo


class FakeEvent:
    def __init__(self, message_str="", umo="default:GroupMessage:123", role="member"):
        self.message_str = message_str
        self.session = FakeSession(umo)
        self.unified_msg_origin = umo
        self.role = role
        self._sender_id = "user1"
        self._self_id = ""

    def get_sender_id(self):
        return self._sender_id

    def get_self_id(self):
        return self._self_id

    def chain_result(self, chain):
        return chain


def make_plugin(config=None):
    tmp = tempfile.mkdtemp(prefix="qa_extra_test_")
    plugin = QaPlugin(None, config or {}, data_dir=tmp)
    return plugin, tmp


def run(coro):
    return asyncio.run(coro)


async def add_qa(plugin, ev, q, a):
    ev.message_str = f"/问答 添加 {q} = {a}"
    return await plugin.qa_command(ev)


def admin_ev(msg=""):
    return FakeEvent(message_str=msg, role="admin")


class TestRegexMode(unittest.TestCase):
    def test_regex_hit(self):
        plugin, _ = make_plugin({"qa_match_mode": "regex"})
        run(add_qa(plugin, admin_ev(), r"\d+号.*天气", "查天气请看预报"))
        r = run(plugin.on_msg(FakeEvent("今天18号天气怎么样")))
        self.assertIsNotNone(r)
        self.assertEqual(r[0].text, "查天气请看预报")

    def test_regex_no_hit(self):
        plugin, _ = make_plugin({"qa_match_mode": "regex"})
        run(add_qa(plugin, admin_ev(), r"^\d+$", "纯数字"))
        r = run(plugin.on_msg(FakeEvent("abc123")))
        self.assertIsNone(r)

    def test_regex_invalid_pattern_skipped(self):
        # 非法正则不崩溃，跳过该条
        plugin, _ = make_plugin({"qa_match_mode": "regex", "qa_min_length": 1})
        run(add_qa(plugin, admin_ev(), "[invalid(", "坏正则"))
        r = run(plugin.on_msg(FakeEvent("随便说点什么")))
        self.assertIsNone(r)

    def test_regex_min_length_not_applied(self):
        # regex 模式下匹配阶段不受最短长度限制（如从磁盘加载的历史短条目）
        plugin, _ = make_plugin({"qa_match_mode": "regex", "qa_min_length": 5})
        plugin._data["default:GroupMessage:123"] = [
            {"id": 1, "q": "你好", "a": "短正则也命中"}
        ]
        r = run(plugin.on_msg(FakeEvent("你好呀朋友")))
        self.assertEqual(r[0].text, "短正则也命中")


class TestFuzzyMode(unittest.TestCase):
    def test_fuzzy_high_similarity_hit(self):
        plugin, _ = make_plugin({"qa_match_mode": "fuzzy", "qa_fuzzy_threshold": 0.8})
        run(add_qa(plugin, admin_ev(), "今天天气怎么样", "看看天气预报吧"))
        r = run(plugin.on_msg(FakeEvent("今天天气怎么洋")))
        self.assertIsNotNone(r)
        self.assertEqual(r[0].text, "看看天气预报吧")

    def test_fuzzy_low_similarity_no_hit(self):
        plugin, _ = make_plugin({"qa_match_mode": "fuzzy", "qa_fuzzy_threshold": 0.9})
        run(add_qa(plugin, admin_ev(), "今天天气怎么样", "看看天气预报吧"))
        r = run(plugin.on_msg(FakeEvent("完全不同的另一句话")))
        self.assertIsNone(r)

    def test_fuzzy_threshold_config(self):
        # 高阈值时低相似度不命中；降低阈值后命中
        plugin, _ = make_plugin({"qa_match_mode": "fuzzy", "qa_fuzzy_threshold": 0.99})
        run(add_qa(plugin, admin_ev(), "请问食堂几点开门", "中午十一点"))
        r = run(plugin.on_msg(FakeEvent("请问食堂几点开门呀")))
        self.assertIsNone(r)
        plugin2, _ = make_plugin({"qa_match_mode": "fuzzy", "qa_fuzzy_threshold": 0.75})
        run(add_qa(plugin2, admin_ev(), "请问食堂几点开门", "中午十一点"))
        r2 = run(plugin2.on_msg(FakeEvent("请问食堂几点开门呀")))
        self.assertEqual(r2[0].text, "中午十一点")

    def test_fuzzy_dirty_threshold(self):
        # 脏值回退默认 0.85
        plugin, _ = make_plugin({"qa_match_mode": "fuzzy", "qa_fuzzy_threshold": "abc"})
        run(add_qa(plugin, admin_ev(), "你好你好", "打招呼"))
        r = run(plugin.on_msg(FakeEvent("你好你好")))
        self.assertIsNotNone(r)


class TestImageReply(unittest.TestCase):
    def test_img_tag_parsed(self):
        plugin, _ = make_plugin()
        run(add_qa(plugin, admin_ev(), "看图", "[img]https://example.com/cat.png[/img]"))
        r = run(plugin.on_msg(FakeEvent("给我看图")))
        self.assertIsNotNone(r)
        kinds = [type(c).__name__ for c in r]
        self.assertIn("Image", kinds)

    def test_img_and_text_mixed(self):
        plugin, _ = make_plugin()
        run(add_qa(plugin, admin_ev(), "教程", "看这里 [img]https://example.com/t.jpg[/img] 学会了吗"))
        r = run(plugin.on_msg(FakeEvent("发一下教程")))
        texts = "".join(c.text for c in r if type(c).__name__ == "Plain")
        self.assertIn("看这里", texts)
        self.assertIn("学会了吗", texts)
        self.assertTrue(any(type(c).__name__ == "Image" for c in r))

    def test_plain_answer_still_works(self):
        plugin, _ = make_plugin()
        run(add_qa(plugin, admin_ev(), "普通答案", "就是文本"))
        r = run(plugin.on_msg(FakeEvent("普通答案")))
        self.assertEqual(len(r), 1)
        self.assertEqual(r[0].text, "就是文本")

    def test_bad_url_fallback_to_text(self):
        from astrbot_plugin_qa.main import QaPlugin as QP
        comps = QP._build_reply("[img]not-a-valid-url[/img]")
        # 非法 URL 降级为原文文本，不抛异常
        joined = "".join(getattr(c, "text", "") for c in comps)
        self.assertIn("not-a-valid-url", joined)


class TestHitStats(unittest.TestCase):
    def test_stats_recorded_on_hit(self):
        plugin, tmp = make_plugin()
        run(add_qa(plugin, admin_ev(), "打卡问题", "打卡回答"))
        run(plugin.on_msg(FakeEvent("打卡问题")))
        run(plugin.on_msg(FakeEvent("打卡问题")))
        # 同一问题有冷却，第二条不计；冷却默认 10 秒内只记 1 次
        key = "default:GroupMessage:123|1"
        self.assertGreaterEqual(plugin._stats.get(key, 0), 1)

    def test_stats_persisted(self):
        plugin, tmp = make_plugin()
        run(add_qa(plugin, admin_ev(), "持久化问题", "持久化回答"))
        run(plugin.on_msg(FakeEvent("持久化问题")))
        # 重新加载，统计应还在
        plugin2 = QaPlugin(None, {}, data_dir=tmp)
        self.assertGreaterEqual(
            plugin2._stats.get("default:GroupMessage:123|1", 0), 1
        )

    def test_stats_command_top(self):
        plugin, tmp = make_plugin({"qa_cooldown_seconds": 0})
        run(add_qa(plugin, admin_ev(), "问题甲", "答案甲"))
        run(add_qa(plugin, admin_ev(), "问题乙乙乙乙乙", "答案乙"))
        run(plugin.on_msg(FakeEvent("问题甲")))
        run(plugin.on_msg(FakeEvent("问题甲")))
        run(plugin.on_msg(FakeEvent("问题甲")))
        run(plugin.on_msg(FakeEvent("问题乙乙乙乙乙")))
        r = run(plugin.qa_command(admin_ev("/问答 统计")))
        self.assertIn("命中排行", r[0].text)
        self.assertIn("问题甲", r[0].text)
        self.assertIn("3 次", r[0].text)

    def test_stats_empty_hint(self):
        plugin, _ = make_plugin()
        r = run(plugin.qa_command(admin_ev("/问答 统计")))
        self.assertIn("还没有命中记录", r[0].text)

    def test_stats_disabled(self):
        plugin, _ = make_plugin({"qa_stats_enabled": False})
        run(add_qa(plugin, admin_ev(), "不统计问题", "不统计回答"))
        run(plugin.on_msg(FakeEvent("不统计问题")))
        self.assertEqual(plugin._stats, {})


class TestExportImport(unittest.TestCase):
    def test_export_contains_json(self):
        plugin, _ = make_plugin()
        run(add_qa(plugin, admin_ev(), "导出问题一", "导出答案一"))
        run(add_qa(plugin, admin_ev(), "导出问题二", "导出答案二"))
        r = run(plugin.qa_command(admin_ev("/问答 导出")))
        self.assertIn("导出成功", r[0].text)
        self.assertIn('"q"', r[0].text)
        self.assertIn("导出问题一", r[0].text)

    def test_export_empty(self):
        plugin, _ = make_plugin()
        r = run(plugin.qa_command(admin_ev("/问答 导出")))
        self.assertIn("没有", r[0].text)

    def test_import_success(self):
        plugin, _ = make_plugin()
        payload = json.dumps(
            [{"q": "导入问题A", "a": "导入答案A"}, {"q": "导入问题B", "a": "导入答案B"}],
            ensure_ascii=False,
        )
        r = run(plugin.qa_command(admin_ev(f"/问答 导入 {payload}")))
        self.assertIn("新增 2 条", r[0].text)
        items = plugin._data.get("default:GroupMessage:123", [])
        self.assertEqual(len(items), 2)
        # 导入的问答可正常触发
        r2 = run(plugin.on_msg(FakeEvent("导入问题A")))
        self.assertEqual(r2[0].text, "导入答案A")

    def test_import_skip_duplicates(self):
        plugin, _ = make_plugin()
        run(add_qa(plugin, admin_ev(), "已有问题", "已有答案"))
        payload = json.dumps([{"q": "已有问题", "a": "新答案"}], ensure_ascii=False)
        r = run(plugin.qa_command(admin_ev(f"/问答 导入 {payload}")))
        self.assertIn("新增 0 条", r[0].text)
        self.assertIn("重复跳过 1 条", r[0].text)

    def test_import_skip_invalid(self):
        plugin, _ = make_plugin()
        payload = json.dumps(
            [{"q": "", "a": "空问题"}, {"q": "缺答案"}, "garbage",
             {"q": "有效问题", "a": "有效答案"}],
            ensure_ascii=False,
        )
        r = run(plugin.qa_command(admin_ev(f"/问答 导入 {payload}")))
        self.assertIn("新增 1 条", r[0].text)
        self.assertIn("无效跳过 3 条", r[0].text)

    def test_import_bad_json(self):
        plugin, _ = make_plugin()
        r = run(plugin.qa_command(admin_ev("/问答 导入 {不是json")))
        self.assertIn("JSON 解析失败", r[0].text)

    def test_import_wrong_type(self):
        plugin, _ = make_plugin()
        r = run(plugin.qa_command(admin_ev('/问答 导入 {"q": "不是数组"}')))
        self.assertIn("格式错误", r[0].text)

    def test_import_requires_permission(self):
        plugin, _ = make_plugin()
        payload = json.dumps([{"q": "无权限问题", "a": "无权限答案"}], ensure_ascii=False)
        r = run(plugin.qa_command(FakeEvent(f"/问答 导入 {payload}", role="member")))
        self.assertIn("没有权限", r[0].text)

    def test_import_respects_max_limit(self):
        plugin, _ = make_plugin({"qa_max_per_session": 3})
        rows = [{"q": f"上限问题{i}", "a": f"上限答案{i}"} for i in range(5)]
        payload = json.dumps(rows, ensure_ascii=False)
        r = run(plugin.qa_command(admin_ev(f"/问答 导入 {payload}")))
        self.assertIn("新增 3 条", r[0].text)
        items = plugin._data.get("default:GroupMessage:123", [])
        self.assertEqual(len(items), 3)


class TestClearCleansStats(unittest.TestCase):
    def test_clear_removes_session_stats(self):
        plugin, tmp = make_plugin({"qa_cooldown_seconds": 0})
        run(add_qa(plugin, admin_ev(), "清理问题甲", "清理答案甲"))
        run(plugin.on_msg(FakeEvent("清理问题甲")))
        key_prefix = "default:GroupMessage:123|"
        before = {k for k in plugin._stats if k.startswith(key_prefix)}
        self.assertTrue(before)
        r = run(plugin.qa_command(admin_ev("/问答 清空")))
        self.assertIn("已清空", r[0].text)
        after = {k for k in plugin._stats if k.startswith(key_prefix)}
        self.assertEqual(after, set())


if __name__ == "__main__":
    unittest.main(verbosity=1)
