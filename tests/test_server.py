"""线程化真实 AppServer、本机模型 HTTP 与 CLI 双实例回归。"""
import concurrent.futures
import contextlib
import http.client
import io
import json
from pathlib import Path
import select
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from store import Store
from test_model import LocalModel, delta, sse


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.app = server.AppServer(("127.0.0.1", 0), self.store)
        self.thread = threading.Thread(target=self.app.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        original_connect = socket.socket.connect
        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("测试禁止外部网络: %r" % (address,))
            return original_connect(sock, address)
        guard = mock.patch.object(socket.socket, "connect", loopback_only)
        guard.start()
        self.addCleanup(guard.stop)
        self.counter = 0

    def close_server(self):
        self.app.shutdown()
        self.app.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, payload=None, headers=None, raw=None):
        body = raw if raw is not None else json.dumps(payload if payload is not None else {}, ensure_ascii=False).encode()
        request_headers = {"Host": "127.0.0.1:%d" % self.app.server_port}
        if method != "GET":
            request_headers.update({"Content-Type": "application/json", "X-Local-App": "guanlan"})
        for key, value in (headers or {}).items():
            if value is None:
                request_headers.pop(key, None)
            else:
                request_headers[key] = value
        connection = http.client.HTTPConnection("127.0.0.1", self.app.server_port, timeout=10)
        try:
            connection.request(method, path, body=None if method == "GET" else body, headers=request_headers)
            response = connection.getresponse()
            content = response.read()
            content_type = response.getheader("Content-Type", "")
            if "application/x-ndjson" in content_type:
                value = [json.loads(line) for line in content.splitlines() if line]
            elif "application/json" in content_type:
                value = json.loads(content)
            else:
                value = content
            return response.status, value, dict(response.getheaders())
        finally:
            connection.close()

    def new_session(self):
        status, value, _ = self.request("POST", "/api/sessions")
        self.assertEqual(status, 201)
        return value["session"]["id"]

    def chat(self, sid, content="你好", rid=None):
        self.counter += 1
        return self.request("POST", "/api/chat", {"session_id": sid, "content": content, "request_id": rid or "http_request_%08d" % self.counter})

    def settings(self, **values):
        status, result, _ = self.request("POST", "/api/settings", values)
        self.assertEqual(status, 200, result)
        return result["settings"]

    def use_model(self, upstream, memory=True):
        self.settings(mode="openai", base_url=upstream.settings["base_url"], model="test-model", memory_enabled=memory)

    def test_local_chat_persists_and_cross_session_recalls(self):
        sid = self.new_session()
        status, events, _ = self.chat(sid, "我喜欢喝咖啡")
        self.assertEqual(status, 200)
        self.assertEqual([e["type"] for e in events[:2]], ["start", "context"])
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["mode"], "local")
        self.assertIn("非大模型", events[-1]["message"]["content"])
        other = self.new_session()
        _, recalled, _ = self.chat(other, "我喜欢喝什么饮品？")
        self.assertEqual(recalled[1]["memories"][0]["content"], "用户偏好喝咖啡")
        self.assertEqual(recalled[1]["history_count"], 0)
        status, session, _ = self.request("GET", "/api/sessions/" + sid)
        self.assertEqual(status, 200)
        self.assertEqual(len(session["messages"]), 2)

    def test_host_origin_fetch_site_and_app_header_guards(self):
        cases = [({"Host": "evil.invalid"}, "INVALID_HOST"),
                 ({"Origin": "https://evil.invalid"}, "INVALID_ORIGIN"),
                 ({"Sec-Fetch-Site": "cross-site"}, "INVALID_ORIGIN"),
                 ({"X-Local-App": None}, "CSRF"), ({"X-Local-App": "wrong"}, "CSRF")]
        for headers, code in cases:
            with self.subTest(headers=headers):
                status, value, _ = self.request("POST", "/api/sessions", headers=headers)
                self.assertEqual(status, 403)
                self.assertEqual(value["error"]["code"], code)
        self.assertEqual(self.store.stats()["sessions"], 0)

    def test_same_origin_with_charset_json_is_accepted(self):
        status, _, _ = self.request("POST", "/api/sessions", headers={"Origin": "http://127.0.0.1:%d" % self.app.server_port, "Content-Type": "application/json; charset=utf-8"})
        self.assertEqual(status, 201)

    def test_get_rejects_untrusted_host_and_origin(self):
        for headers in [{"Host": "127.0.0.1.evil.invalid"}, {"Origin": "null"}, {"Origin": "http://127.0.0.1:1"}]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request("GET", "/api/state", headers=headers)[0], 403)

    def test_content_type_and_chunked_upload_rejected(self):
        for headers, status, code in [({"Content-Type": "text/plain"}, 415, "CONTENT_TYPE"),
                                      ({"Content-Type": None}, 415, "CONTENT_TYPE"),
                                      ({"Transfer-Encoding": "chunked"}, 400, "CONTENT_LENGTH")]:
            with self.subTest(headers=headers):
                actual, value, _ = self.request("POST", "/api/sessions", headers=headers)
                self.assertEqual(actual, status)
                self.assertEqual(value["error"]["code"], code)

    def test_json_body_size_limit_checked_before_reading(self):
        status, value, _ = self.request("POST", "/api/sessions", headers={"Content-Length": str(server.MAX_BODY + 1)}, raw=b"")
        self.assertEqual((status, value["error"]["code"]), (413, "TOO_LARGE"))
        raw = b'{"title":"ok","padding":"' + b"x" * server.MAX_BODY + b'"}'
        self.assertEqual(self.request("POST", "/api/sessions", raw=raw)[0], 413)

    def test_invalid_json_and_nonobject_json_rejected(self):
        for raw in [b"{", b"[]", b"null", b'"string"', b"\xff"]:
            with self.subTest(raw=raw):
                status, value, _ = self.request("POST", "/api/sessions", raw=raw)
                self.assertEqual((status, value["error"]["code"]), (400, "INVALID_JSON"))

    def test_static_allowlist_never_exposes_runtime_or_sources(self):
        secret = "runtime-private-marker"
        Path(self.temp.name, "private.txt").write_text(secret)
        for path in ["/runtime/private.txt", "/runtime/guanlan.sqlite3", "/server.py", "/store.py", "/CONTRACT.md", "/../runtime/private.txt", "/%2e%2e/runtime/private.txt", "/static/../runtime/private.txt"]:
            with self.subTest(path=path):
                status, value, _ = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertNotIn(secret, str(value))
        # 用临时静态根避免依赖前端 teammate 是否已完成页面。
        with tempfile.TemporaryDirectory() as root:
            static = Path(root, "static")
            static.mkdir()
            for name in ["index.html", "style.css", "app.js"]:
                (static / name).write_text("safe-static:" + name)
            with mock.patch.object(server, "ROOT", Path(root)):
                for path in ["/", "/index.html", "/style.css", "/app.js"]:
                    status, value, headers = self.request("GET", path)
                    self.assertEqual(status, 200)
                    self.assertTrue(value.startswith(b"safe-static:"))
                    self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                    self.assertEqual(headers["X-Frame-Options"], "DENY")

    def test_key_is_masked_and_never_in_state_or_audit_payload(self):
        key = "synthetic-test-key-12345678"
        public = self.settings(api_key=key)
        self.assertTrue(public["has_api_key"])
        self.assertNotIn("api_key", public)
        self.assertNotIn(key, json.dumps(public))
        self.assertEqual(public["api_key_masked"], "••••5678")
        for path in ["/api/state", "/api/audit"]:
            self.assertNotIn(key, json.dumps(self.request("GET", path)[1]))
        with self.store.connection() as db:
            self.assertNotIn(key, " ".join(row[0] for row in db.execute("SELECT payload FROM events")))
        self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)

    def test_key_preserved_same_origin_and_cleared_on_origin_or_provider(self):
        key = "synthetic-test-key-12345678"
        self.settings(api_key=key)
        self.assertTrue(self.settings(base_url="https://api.deepseek.com:443/other", api_key="")["has_api_key"])
        self.assertEqual(self.store.settings(public=False)["api_key"], key)
        self.assertFalse(self.settings(base_url="https://different.invalid/v1", api_key="")["has_api_key"])
        self.settings(api_key=key)
        self.assertFalse(self.settings(provider="other-provider")["has_api_key"])
        self.settings(api_key=key)
        self.assertFalse(self.settings(api_key=None)["has_api_key"])

    def test_remote_plain_http_rejected_without_mutation(self):
        before = self.store.state()
        status, value, _ = self.request("POST", "/api/settings", {"base_url": "http://example.invalid/v1"})
        self.assertEqual((status, value["error"]["code"]), (400, "INSECURE_ENDPOINT"))
        self.assertEqual(self.store.state(), before)

    def test_duplicate_request_replays_without_new_model_call(self):
        sid = self.new_session()
        with LocalModel() as upstream:
            self.use_model(upstream, memory=False)
            _, first, _ = self.chat(sid, rid="repeat_request")
            before = self.store.stats()
            _, second, _ = self.chat(sid, rid="repeat_request")
            self.assertEqual(first[-1]["message"], second[-1]["message"])
            self.assertTrue(second[0]["replayed"])
            self.assertEqual(len(upstream.requests), 1)
            self.assertEqual(self.store.stats(), before)
            status, value, _ = self.chat(sid, "不同输入", rid="repeat_request")
            self.assertEqual((status, value["error"]["code"]), (409, "REQUEST_CONFLICT"))

    def test_memory_pause_blocks_injection_and_automatic_writes(self):
        sid = self.new_session()
        self.chat(sid, "我喜欢喝咖啡")
        original = self.store.memories()
        self.settings(memory_enabled=False)
        _, events, _ = self.chat(sid, "我住在杭州")
        self.assertEqual(events[1]["memories"], [])
        self.assertEqual(events[1]["history_count"], 0, "暂停时来源历史不能继续带入旧记忆")
        self.assertEqual(events[-2]["created"] + events[-2]["updated"], [])
        self.assertEqual(self.store.memories(), original)
        self.settings(memory_enabled=True)
        other = self.new_session()
        _, resumed, _ = self.chat(other, "我喜欢喝什么饮品？")
        self.assertEqual(len(resumed[1]["memories"]), 1)

    def test_model_stream_success_commits_once_and_does_not_extract_assistant(self):
        sid = self.new_session()
        with LocalModel(stream=sse(delta("我住在火星"), "[DONE]")) as upstream:
            self.use_model(upstream)
            _, events, _ = self.chat(sid, "你好")
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(events[-1]["message"]["content"], "我住在火星")
            self.assertEqual(self.store.memories(), [])
            self.assertEqual(len(upstream.requests), 2)
            extractor_input = upstream.requests[1]["body"]["messages"][-1]["content"]
            self.assertNotIn("火星", extractor_input)
            self.assertEqual(self.store.stats()["messages"], 2)

    def test_model_and_rule_candidates_merge_with_rule_priority(self):
        sid = self.new_session()
        candidates = [{"key": "profile.city", "category": "profile", "content": "用户居住在错误城市", "source_quote": "我住在杭州"},
                      {"key": "profile.pet", "category": "profile", "content": "用户养了一只猫", "source_quote": "我养了一只猫"}]
        with LocalModel(extraction=json.dumps({"memories": candidates}, ensure_ascii=False)) as upstream:
            self.use_model(upstream)
            _, events, _ = self.chat(sid, "我住在杭州。我养了一只猫")
            self.assertEqual(events[-1]["type"], "done")
            facts = {m["key"]: m["content"] for m in self.store.memories()}
            self.assertEqual(facts, {"profile.city": "用户居住在杭州", "profile.pet": "用户养了一只猫"})
            self.assertEqual(events[-1]["stats"]["messages"], 2)

    def test_model_failures_leave_no_partial_commit_and_same_id_can_retry(self):
        streams = [(sse(delta("片段")), "MODEL_INTERRUPTED"),
                   (sse(delta("片段"), {"error": {"message": "故障"}}), "MODEL_FAILED"),
                   (sse(delta("x" * 20000), delta("y" * 20001), "[DONE]"), "MODEL_BUDGET")]
        sid = self.new_session()
        with LocalModel() as upstream:
            self.use_model(upstream)
            before = self.store.stats()
            for stream, code in streams:
                with self.subTest(code=code):
                    upstream.stream = stream
                    status, events, _ = self.chat(sid, "我住在杭州", rid="retry_failure")
                    self.assertEqual(status, 200)
                    self.assertEqual(events[-1]["type"], "error")
                    self.assertEqual(events[-1]["code"], code)
                    self.assertFalse(events[-1]["committed"])
                    self.assertNotIn("done", [e["type"] for e in events])
                    self.assertEqual(self.store.stats(), before)
                    self.assertIsNone(self.store.previous_request("retry_failure", sid, "我住在杭州"))
            upstream.stream = sse(delta("完成"), "[DONE]")
            _, events, _ = self.chat(sid, "我住在杭州", rid="retry_failure")
            self.assertEqual(events[-1]["type"], "done")
            self.assertEqual(self.store.stats()["messages"], 2)

    def test_commit_failure_rolls_back_and_emits_error(self):
        sid = self.new_session()
        before = self.store.stats()
        original = self.store._event
        def event_then_fail(*args):
            original(*args)
            raise RuntimeError("故障注入")
        diagnostic = io.StringIO()
        with contextlib.redirect_stderr(diagnostic), mock.patch.object(self.store, "_event", side_effect=event_then_fail):
            _, events, _ = self.chat(sid, "我住在杭州", rid="rollback_http")
        self.assertIn("RuntimeError", diagnostic.getvalue())
        self.assertNotIn("故障注入", diagnostic.getvalue())
        self.assertNotIn("我住在杭州", diagnostic.getvalue())
        self.assertEqual(events[-1]["type"], "error")
        self.assertFalse(events[-1]["committed"])
        self.assertEqual(self.store.stats(), before)
        self.assertEqual(self.store.memories(), [])
        self.assertIsNone(self.store.previous_request("rollback_http", sid, "我住在杭州"))

    def test_writer_held_entire_model_round_blocks_mutations_not_reads(self):
        sid = self.new_session()
        with LocalModel() as upstream:
            self.use_model(upstream, memory=False)
            upstream.release = threading.Event()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                pending = executor.submit(self.chat, sid)
                try:
                    self.assertTrue(upstream.entered.wait(timeout=5), "本机模型未收到请求")
                    for path, payload in [("/api/settings", {"memory_enabled": False}),
                                          ("/api/memories", {"content": "用户偏好茶", "category": "preference"}),
                                          ("/api/sessions", {})]:
                        status, value, _ = self.request("POST", path, payload)
                        self.assertEqual((status, value["error"]["code"]), (409, "BUSY"))
                    status, value, _ = self.request("GET", "/api/state")
                    self.assertEqual(status, 200)
                    self.assertTrue(value["busy"])
                finally:
                    upstream.release.set()
                self.assertEqual(pending.result(timeout=10)[1][-1]["type"], "done")
        self.assertFalse(self.store.state()["busy"])

    def test_chat_input_budget_rejected_before_commit(self):
        sid = self.new_session()
        before = self.store.stats()
        status, value, _ = self.chat(sid, "字" * 6001)
        self.assertEqual((status, value["error"]["code"]), (400, "INVALID_INPUT"))
        self.assertEqual(self.store.stats(), before)

    def test_settings_test_uses_saved_config_without_switching_mode(self):
        with LocalModel(extraction="OK") as upstream:
            self.settings(base_url=upstream.settings["base_url"], model="test-model", mode="local")
            status, value, _ = self.request("POST", "/api/settings/test")
            self.assertEqual(status, 200)
            self.assertTrue(value["ok"])
            self.assertEqual(self.store.settings()["mode"], "local")
            self.assertEqual(upstream.requests[0]["body"]["model"], "test-model")


class CLILockTests(unittest.TestCase):
    def test_real_cli_second_instance_refused_by_directory_flock(self):
        with tempfile.TemporaryDirectory() as directory:
            command = [sys.executable, "-u", str(Path(server.__file__).resolve()), "--port", "0", "--data-dir", directory]
            first = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                readable, _, _ = select.select([first.stdout], [], [], 10)
                self.assertTrue(readable, "CLI 未发出就绪信号")
                line = first.stdout.readline()
                self.assertIn("http://127.0.0.1:", line)
                second = subprocess.run(command, capture_output=True, text=True, timeout=10)
                self.assertEqual(second.returncode, 1)
                self.assertIn("拒绝启动第二个写者", second.stderr)
                self.assertIsNone(first.poll())
            finally:
                first.terminate()
                try:
                    first.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    first.kill()
                    first.communicate(timeout=5)
            # flock 随进程退出释放；同目录能够再次真实启动。
            restarted = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                readable, _, _ = select.select([restarted.stdout], [], [], 10)
                self.assertTrue(readable)
                self.assertIn("http://127.0.0.1:", restarted.stdout.readline())
            finally:
                restarted.terminate()
                try:
                    restarted.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    restarted.kill()
                    restarted.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
