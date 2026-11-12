"""仅本机 HTTP 模型桩与标准库 mock：流协议、预算和保守提取。"""
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import socket
import sys
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import model
from store import StoreError


def sse(*items):
    return b"".join(b"data: " + (item.encode() if isinstance(item, str) else json.dumps(item, ensure_ascii=False).encode()) + b"\n\n" for item in items)


def delta(text, finish=None):
    return {"choices": [{"delta": {"content": text}, "finish_reason": finish}]}


class LocalModel:
    """真实本地模型 HTTP；禁止任何非 loopback socket 连接。"""
    def __init__(self, stream=None, extraction=None, status=200):
        self.stream = stream if stream is not None else sse(delta("本机模型回答"), "[DONE]")
        self.extraction = extraction if extraction is not None else '{"memories":[]}'
        self.status = status
        self.requests = []
        self.entered = threading.Event()
        self.release = None

    def __enter__(self):
        owner = self
        original_connect = socket.socket.connect
        def loopback_only(sock, address):
            if not isinstance(address, tuple) or address[0] not in {"127.0.0.1", "::1", "localhost"}:
                raise AssertionError("测试禁止外部网络: %r" % (address,))
            return original_connect(sock, address)
        self.guard = mock.patch.object(socket.socket, "connect", loopback_only)
        self.guard.start()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": payload})
                owner.entered.set()
                if owner.release is not None:
                    if not owner.release.wait(timeout=10):
                        self.send_error(504)
                        return
                body = owner.stream if payload.get("stream") else json.dumps({"choices": [{"message": {"content": owner.extraction}}]}, ensure_ascii=False).encode()
                self.send_response(owner.status)
                self.send_header("Content-Type", "text/event-stream" if payload.get("stream") else "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.settings = {"base_url": "http://127.0.0.1:%d/v1" % self.server.server_port,
                         "model": "test-model", "api_key": ""}
        return self

    def __exit__(self, *args):
        if self.release is not None:
            self.release.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.guard.stop()


class ModelTests(unittest.TestCase):
    context = {"memories": [], "history": []}

    def assert_stream_error(self, data, code):
        with LocalModel(stream=data) as upstream:
            with self.assertRaises(StoreError) as raised:
                list(model.stream_reply(upstream.settings, "你好", self.context))
            self.assertEqual(raised.exception.code, code)

    def candidate(self, quote="我住在杭州", **changes):
        item = {"key": "profile.city", "category": "profile", "content": "用户居住在杭州", "source_quote": quote}
        item.update(changes)
        return item

    def extract(self, prompt, candidates):
        with mock.patch.object(model, "complete_json", return_value=json.dumps({"memories": candidates}, ensure_ascii=False)):
            return model.extract_with_model({}, prompt, [])

    def test_local_http_sse_text_done_and_payload(self):
        with LocalModel(stream=sse(delta("你好"), delta("，世界"), "[DONE]")) as upstream:
            result = list(model.stream_reply(upstream.settings, "打招呼", self.context))
            self.assertEqual(result, ["你好", "，世界"])
            request = upstream.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertTrue(request["body"]["stream"])
            self.assertEqual(request["body"]["messages"][-1], {"role": "user", "content": "打招呼"})
            self.assertNotIn("Authorization", request["headers"])

    def test_finish_reason_is_terminal_without_done(self):
        with LocalModel(stream=sse(delta("完整回答"), delta("", "stop"))) as upstream:
            self.assertEqual(list(model.stream_reply(upstream.settings, "你好", self.context)), ["完整回答"])

    def test_eof_after_text_is_interrupted(self):
        self.assert_stream_error(sse(delta("只有一半")), "MODEL_INTERRUPTED")

    def test_upstream_error_is_not_success(self):
        self.assert_stream_error(sse(delta("片段"), {"error": {"message": "private diagnostic"}}), "MODEL_FAILED")

    def test_malformed_sse_json_is_rejected(self):
        self.assert_stream_error(b"data: {not-json}\n\n", "MODEL_FORMAT")

    def test_empty_stream_is_rejected(self):
        self.assert_stream_error(sse("[DONE]"), "MODEL_EMPTY")

    def test_stream_line_budget(self):
        self.assert_stream_error(b"data: " + b"x" * 65536 + b"\n", "MODEL_FORMAT")

    def test_output_character_budget(self):
        self.assert_stream_error(sse(delta("甲" * 15000), delta("乙" * 15000), delta("丙" * 10001), "[DONE]"), "MODEL_BUDGET")

    def test_connection_error_has_sanitized_message(self):
        with mock.patch.object(model.urllib.request.OpenerDirector, "open", side_effect=OSError("secret test diagnostic")):
            with self.assertRaises(StoreError) as raised:
                model._open({"base_url": "http://127.0.0.1:1", "api_key": "synthetic-only"}, {})
        self.assertEqual(raised.exception.code, "MODEL_CONNECTION")
        self.assertNotIn("secret", raised.exception.message)

    def test_http_auth_error_has_sanitized_message(self):
        with LocalModel(status=401) as upstream:
            with self.assertRaises(StoreError) as raised:
                list(model.stream_reply(upstream.settings, "你好", self.context))
        self.assertEqual(raised.exception.code, "MODEL_AUTH")

    def test_json_response_budget(self):
        with mock.patch.object(model, "_open", return_value=io.BytesIO(b"x" * 131073)):
            with self.assertRaises(StoreError) as raised:
                model.complete_json({"model": "mock"}, [])
        self.assertEqual(raised.exception.code, "MODEL_BUDGET")

    def test_memory_is_user_data_not_system_authority(self):
        memory = {"id": "memory_test", "category": "profile", "content": "测试记忆"}
        messages = model.chat_messages("当前问题", {"memories": [memory], "history": [{"role": "user", "content": "历史"}]})
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user", "user"])
        self.assertNotIn("测试记忆", messages[0]["content"])
        self.assertIn("memory_data", messages[1]["content"])
        self.assertEqual(messages[-1]["content"], "当前问题")

    def test_remote_http_and_endpoint_credentials_are_rejected(self):
        for endpoint in ["http://example.invalid/v1", "https://user:pass@example.invalid/v1", "https://example.invalid/v1?key=x", "file:///tmp/model", "https://example.invalid:wrong"]:
            with self.subTest(endpoint=endpoint), self.assertRaises(StoreError):
                model.normalize_base(endpoint)
        self.assertEqual(model.normalize_base(" http://127.0.0.1:1234/v1/chat/completions/ "), "http://127.0.0.1:1234/v1")

    def test_extractor_accepts_literal_first_person_quote(self):
        item = self.candidate()
        self.assertEqual(self.extract("我住在杭州", [item]), [item])

    def test_extractor_rejects_fabricated_nonliteral_and_noun_quotes(self):
        for quote in ["我住在上海", "杭州", "用户居住在杭州", "住在杭州"]:
            with self.subTest(quote=quote):
                self.assertEqual(self.extract("我住在杭州", [self.candidate(quote)]), [])

    def test_extractor_rejects_wrong_key_category_and_sensitive_content(self):
        for item in [self.candidate(key="profile.杭州"), self.candidate(category="goal"),
                     self.candidate(content="用户密码是abc123"), self.candidate(key="system.rules")]:
            with self.subTest(item=item):
                self.assertEqual(self.extract("我住在杭州", [item]), [])

    def test_extractor_rejects_questions_hypothetical_and_reported_quotes(self):
        for prompt in ["如果我住在杭州", "我住在杭州？", "朋友说我住在杭州"]:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.extract(prompt, [self.candidate(prompt)]), [])

    def test_extractor_does_not_accept_assistant_or_quoted_user_claims(self):
        # source_quote 必须是本人陈述，不能靠剪掉转述前缀绕过准入。
        for prompt in ['助手说：“我住在杭州”。', '请翻译“我住在杭州”。', '```text\n我住在杭州\n```', '> 我住在杭州']:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.extract(prompt, [self.candidate()]), [])

    def test_extractor_respects_explicit_no_memory(self):
        for prompt in ["我住在杭州，但不要记", "我住在杭州。别记。", "我住在杭州，请不保存"]:
            with self.subTest(prompt=prompt):
                self.assertEqual(self.extract(prompt, [self.candidate()]), [])

    def test_extraction_request_never_contains_assistant_answer(self):
        with LocalModel(extraction=json.dumps({"memories": [self.candidate()]}, ensure_ascii=False)) as upstream:
            result = model.extract_with_model(upstream.settings, "我住在杭州", [])
            self.assertEqual(len(result), 1)
            messages = upstream.requests[0]["body"]["messages"]
            self.assertEqual([m["role"] for m in messages], ["system", "user"])
            self.assertEqual(json.loads(messages[-1]["content"])["current_user_input"], "我住在杭州")

    def test_extractor_caps_candidates_and_rejects_invalid_envelope(self):
        candidates = [self.candidate(key="profile.field%d" % i) for i in range(6)]
        self.assertEqual(len(self.extract("我住在杭州", candidates)), 4)
        for text in ['{"memories":null}', '{"memories":{}}', "[]", "not-json"]:
            with self.subTest(text=text), mock.patch.object(model, "complete_json", return_value=text):
                self.assertEqual(model.extract_with_model({}, "我住在杭州", []), [])


if __name__ == "__main__":
    unittest.main()
