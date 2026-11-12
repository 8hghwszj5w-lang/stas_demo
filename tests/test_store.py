"""真实临时 SQLite 的持久化、单写者、上下文与事务回归测试。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from store import Store, StoreError
from memory import extract_candidates


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.sid = self.mutate(self.store.create_session)["id"]
        self.counter = 0

    def mutate(self, fn, *args, **kwargs):
        # Store 内部方法的写锁由调用方负责，不以绕过锁作为并发测试。
        with self.store.writer():
            return fn(*args, **kwargs)

    def chat(self, prompt, response="收到", sid=None, candidates=None, rid=None):
        self.counter += 1
        sid = sid or self.sid
        with self.store.writer():
            context = self.store.context(sid, prompt)
            return self.store.commit_chat(sid, prompt, response, rid or "request_%08d" % self.counter,
                                          "local", context, extract_candidates(prompt) if candidates is None else candidates)

    def test_restart_preserves_identity_messages_memories_and_audit(self):
        result = self.chat("我叫林舟")
        before = self.store.state()
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.state(), before)
        self.assertEqual(reopened.messages(self.sid)[0], result["user_message"])
        self.assertEqual(reopened.memories()[0]["source_message_id"], result["user_message"]["id"])
        self.assertTrue(reopened.audit()["chain_valid"])
        self.assertEqual(reopened.path.stat().st_mode & 0o777, 0o600)

    def test_recall_cross_session_without_copying_history(self):
        result = self.chat("我喜欢喝咖啡")
        other = self.mutate(self.store.create_session)["id"]
        context = self.store.context(other, "我喜欢喝什么饮品？")
        self.assertEqual([m["id"] for m in context["memories"]], [result["created"][0]["id"]])
        self.assertEqual(context["history"], [])

    def test_same_key_revises_and_links_versions(self):
        old = self.chat("我住在杭州")["created"][0]
        new = self.chat("我住在上海")["updated"][0]
        self.assertEqual(new["key"], old["key"])
        self.assertEqual(new["version"], 2)
        self.assertEqual(self.store.memory(old["id"])["superseded_by"], new["id"])
        self.assertEqual(self.store.memory(old["id"])["status"], "superseded")
        self.assertEqual([m["version"] for m in self.store.memory_history(old["id"])], [2, 1])
        self.assertEqual(len(self.store.memories()), 1)

    def test_identical_candidate_does_not_create_revision(self):
        self.chat("我住在杭州")
        result = self.chat("我住在杭州")
        self.assertEqual(result["created"] + result["updated"], [])
        self.assertEqual(len(self.store.memories(status="all")), 1)

    def test_pinned_memory_protected_from_automatic_revision(self):
        old = self.chat("我住在杭州")["created"][0]
        self.mutate(self.store.change_memory, old["id"], {"pinned": True})
        result = self.chat("我住在上海")
        self.assertEqual(result["updated"], [])
        self.assertEqual(self.store.memories()[0]["content"], old["content"])

    def test_manual_correction_is_pinned_and_can_replace_pinned(self):
        old = self.chat("我住在杭州")["created"][0]
        new = self.mutate(self.store.change_memory, old["id"], {"content": "用户居住在上海"})
        final = self.mutate(self.store.change_memory, new["id"], {"content": "用户居住在苏州"})
        self.assertTrue(final["pinned"])
        self.assertEqual(final["version"], 3)
        self.chat("我住在北京")
        self.assertEqual(self.store.memories()[0]["id"], final["id"])

    def test_forget_excludes_source_and_context_reference_rounds(self):
        old = self.chat("我喜欢喝咖啡")["created"][0]
        other = self.mutate(self.store.create_session)["id"]
        # 无关轮在首次暴露记忆之前，才是真正可保留的干净历史。
        self.chat("今天讨论数学", sid=other)
        recalled = self.chat("我喜欢喝什么饮品？", sid=other)
        self.assertIn(old["id"], recalled["message"]["context_memory_ids"])
        self.mutate(self.store.change_memory, old["id"], {"status": "forgotten"})
        self.assertEqual(self.store.context(self.sid, "咖啡")["history"], [])
        context = self.store.context(other, "咖啡")
        self.assertEqual(context["memories"], [])
        self.assertEqual(context["history"], [{"role": "user", "content": "今天讨论数学"}, {"role": "assistant", "content": "收到"}])
        self.assertEqual(len(self.store.messages(other)), 4, "忘记不是物理擦除")

    def test_forgetting_revision_invalidates_all_versions(self):
        old = self.chat("我住在杭州")["created"][0]
        new = self.chat("我住在上海")["updated"][0]
        self.mutate(self.store.change_memory, new["id"], {"status": "forgotten"})
        self.assertEqual({m["status"] for m in self.store.memory_history(old["id"])}, {"forgotten"})
        self.assertEqual(self.store.context(self.sid, "我住在哪里")["history_count"], 0)

    def test_superseded_source_is_excluded_from_history(self):
        self.chat("我住在杭州")
        self.chat("我住在上海")
        context = self.store.context(self.sid, "今天聊天")
        self.assertNotIn("我住在杭州", json.dumps(context["history"], ensure_ascii=False))

    def test_memory_pause_disables_injection_without_deletion(self):
        old = self.chat("我喜欢喝咖啡")["created"][0]
        context = self.store.context(self.sid, "喝咖啡", memory_enabled=False)
        self.assertEqual(context["memories"], [])
        self.assertEqual(self.store.memory(old["id"])["status"], "active")

    def test_memory_pause_excludes_source_and_context_reference_history(self):
        self.chat("我喜欢喝咖啡")
        other = self.mutate(self.store.create_session)["id"]
        self.chat("今天讨论数学", sid=other)
        self.chat("我喜欢喝什么饮品？", sid=other)
        self.assertEqual(self.store.context(self.sid, "继续", memory_enabled=False)["history"], [])
        context = self.store.context(other, "继续", memory_enabled=False)
        self.assertEqual(context["history"], [{"role": "user", "content": "今天讨论数学"}, {"role": "assistant", "content": "收到"}])
        self.assertEqual(len(self.store.memories()), 1)

    def history_only_provenance_chain(self):
        """让来源轮离开八轮窗口，验证引用来源必须沿后续轮传递。"""
        memory = self.chat("我叫林舟")["created"][0]
        for i in range(9):
            prompt = "计算二加二，第%d轮" % i
            context = self.store.context(self.sid, prompt)
            self.assertEqual(context["memories"], [], "此处不得依靠直接召回传递姓名")
            self.assertIn(memory["id"], context["provenance_memory_ids"])
            turn = self.chat(prompt, response="林舟，计算结果是四", candidates=[])
            self.assertIn(memory["id"], turn["user_message"]["context_memory_ids"])
            self.assertIn(memory["id"], turn["message"]["context_memory_ids"])
        context = self.store.context(self.sid, "继续计算")
        self.assertEqual(context["history_count"], 8)
        self.assertNotIn("我叫林舟", [m["content"] for m in context["history"]])
        return memory

    def test_transitive_forget_excludes_history_only_provenance_after_source_ages_out(self):
        memory = self.history_only_provenance_chain()
        self.mutate(self.store.change_memory, memory["id"], {"status": "forgotten"})
        context = self.store.context(self.sid, "继续计算")
        self.assertEqual(context["memories"], [])
        self.assertEqual(context["history"], [])
        self.assertEqual(context["provenance_memory_ids"], [])
        self.assertEqual(len(self.store.messages(self.sid)), 20, "逻辑忘记不能删除原聊天")
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.context(self.sid, "继续计算")["history"], [])

    def test_transitive_pause_excludes_history_only_provenance_after_source_ages_out(self):
        memory = self.history_only_provenance_chain()
        context = self.store.context(self.sid, "继续计算", memory_enabled=False)
        self.assertEqual(context["memories"], [])
        self.assertEqual(context["history"], [])
        self.assertEqual(context["provenance_memory_ids"], [])
        self.assertEqual(self.store.memory(memory["id"])["status"], "active")
        self.assertEqual(self.store.context(self.sid, "继续计算", memory_enabled=True)["history_count"], 8)

    def test_restart_preserves_idempotent_request_result(self):
        first = self.chat("我叫林舟", rid="restart_request")
        reopened = Store(self.temp.name)
        self.assertEqual(reopened.previous_request("restart_request", self.sid, "我叫林舟"), first)
        with reopened.writer():
            replayed = reopened.commit_chat(self.sid, "我叫林舟", "不能覆盖", "restart_request", "local",
                                            reopened.context(self.sid, "我叫林舟"), [])
        self.assertEqual(replayed, first)
        self.assertEqual(reopened.stats()["messages"], 2)

    def test_candidate_merge_failure_rolls_back_messages_revision_and_event(self):
        old = self.chat("我住在杭州")["created"][0]
        before = self.store.state()
        valid = extract_candidates("我住在上海")[0]
        malformed = {"key": "profile.invalid", "category": "profile"}
        with self.assertRaises(KeyError):
            self.chat("我住在上海", candidates=[valid, malformed], rid="rollback_candidate")
        self.assertEqual(self.store.state(), before)
        self.assertEqual(self.store.memory(old["id"])["status"], "active")
        self.assertIsNone(self.store.previous_request("rollback_candidate", self.sid, "我住在上海"))

    def test_event_failure_rolls_back_messages_memories_and_request(self):
        before = self.store.state()
        original = self.store._event
        def insert_then_fail(*args):
            original(*args)
            raise sqlite3.OperationalError("注入提交故障")
        with mock.patch.object(self.store, "_event", side_effect=insert_then_fail):
            with self.assertRaises(sqlite3.OperationalError):
                self.chat("我住在杭州", rid="rollback_event")
        self.assertEqual(self.store.state(), before)
        self.assertIsNone(self.store.previous_request("rollback_event", self.sid, "我住在杭州"))
        self.assertTrue(self.store.audit()["chain_valid"])

    def test_duplicate_request_is_exactly_one_commit(self):
        first = self.chat("我叫林舟", rid="same_request")
        before = self.store.state()
        second = self.chat("我叫林舟", response="不应保存", rid="same_request")
        self.assertEqual(first, second)
        self.assertEqual(self.store.state(), before)

    def test_duplicate_request_changed_input_or_session_is_conflict(self):
        self.chat("你好", rid="same_request")
        other = self.mutate(self.store.create_session)["id"]
        for sid, prompt in [(self.sid, "不同内容"), (other, "你好")]:
            with self.subTest(sid=sid, prompt=prompt):
                with self.assertRaises(StoreError) as raised:
                    self.store.previous_request("same_request", sid, prompt)
                self.assertEqual((raised.exception.status, raised.exception.code), (409, "REQUEST_CONFLICT"))

    def test_writer_busy_and_exception_releases_lock(self):
        errors = []
        def competing_writer():
            try:
                with self.store.writer():
                    errors.append("错误取得锁")
            except StoreError as exc:
                errors.append((exc.status, exc.code))
        with self.store.writer():
            thread = threading.Thread(target=competing_writer)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(self.store.state()["busy"])
        self.assertEqual(errors, [(409, "BUSY")])
        with self.assertRaises(RuntimeError):
            with self.store.writer():
                raise RuntimeError("测试异常")
        with self.store.writer():
            self.assertTrue(self.store.state()["busy"])
        self.assertFalse(self.store.state()["busy"])

    def test_tampered_audit_fails_closed_on_restart(self):
        self.chat("你好")
        with self.store.writer(), self.store.connection() as db:
            db.execute("UPDATE events SET payload=? WHERE seq=0", ('{"tampered":true}',))
            db.commit()
        self.assertFalse(self.store.audit()["chain_valid"])
        with self.assertRaisesRegex(RuntimeError, "审计链"):
            Store(self.temp.name)

    def test_context_caps_history_to_eight_rounds_and_twelve_thousand_chars(self):
        for i in range(10):
            self.chat("轮次%d" % i, candidates=[])
        context = self.store.context(self.sid, "继续")
        self.assertEqual(context["history_count"], 8)
        self.assertEqual(context["history"][0]["content"], "轮次2")
        self.chat("甲" * 6000, "乙" * 6000, candidates=[])
        context = self.store.context(self.sid, "继续")
        self.assertEqual(context["history_count"], 1)
        self.assertEqual(sum(len(m["content"]) for m in context["history"]), 12000)

    def test_context_memory_count_and_character_budgets(self):
        for i in range(10):
            self.mutate(self.store.add_memory, "偏好%d" % i, "preference")
        self.assertEqual(len(self.store.context(self.sid, "偏好")["memories"]), 6)
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.writer():
                sid = store.create_session()["id"]
                for i in range(8):
                    store.add_memory(str(i) + "甲" * 499, "profile")
            memories = store.context(sid, "个人资料")["memories"]
            self.assertEqual(len(memories), 4)
            self.assertLessEqual(sum(len(m["content"]) + 80 for m in memories), 2400)

    def test_archiving_session_preserves_memory_source(self):
        old = self.chat("我叫林舟")["created"][0]
        self.mutate(self.store.change_session, self.sid, archive=True)
        self.assertEqual(self.store.sessions(), [])
        self.assertEqual(self.store.memory(old["id"])["source_session_id"], self.sid)
        self.assertEqual(self.store.stats()["messages"], 2)


if __name__ == "__main__":
    unittest.main()
