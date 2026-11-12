#!/usr/bin/env python3
"""观澜本地工作台：仅本机 HTTP 服务、真实模型流与事务认知提交。"""
import argparse
import fcntl
import json
import mimetypes
import os
from pathlib import Path
import re
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

from store import Store, StoreError, CATEGORIES
from model import normalize_base, origin, stream_reply, extract_with_model, complete_json
from memory import extract_candidates, local_reply, is_sensitive

ROOT = Path(__file__).resolve().parent
MAX_BODY = 131072


def bounded_string(value, label, maximum, allow_empty=False):
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip()):
        raise StoreError("INVALID_INPUT", "%s须为%s%d字以内的文本。" % (label, "" if allow_empty else "非空的", maximum))
    if "\x00" in value:
        raise StoreError("INVALID_INPUT", "%s含有无效字符。" % label)
    return value.strip()


def bool_value(value, label):
    if not isinstance(value, bool):
        raise StoreError("INVALID_INPUT", label + "必须为布尔值。")
    return value


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store):
        self.store = store
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "GuanlanLocal/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)
        self.streaming = False
        self.client_gone = False

    def log_message(self, format, *args):
        # 不输出路径/输入/凭证，防止对话内容进入终端日志。
        pass

    @property
    def store(self):
        return self.server.store

    def security(self, write=False):
        port = self.server.server_address[1]
        allowed_hosts = {"127.0.0.1:%d" % port, "localhost:%d" % port}
        if self.headers.get("Host", "") not in allowed_hosts:
            raise StoreError("INVALID_HOST", "仅允许通过本机地址访问。", 403)
        origin_header = self.headers.get("Origin")
        if origin_header and origin_header not in {"http://" + h for h in allowed_hosts}:
            raise StoreError("INVALID_ORIGIN", "拒绝跨站请求。", 403)
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise StoreError("INVALID_ORIGIN", "拒绝跨站请求。", 403)
        if write:
            if self.headers.get("X-Local-App") != "guanlan":
                raise StoreError("CSRF", "请求缺少本地应用标识。", 403)
            if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
                raise StoreError("CONTENT_TYPE", "写请求必须使用 application/json。", 415)
            if self.headers.get("Transfer-Encoding"):
                raise StoreError("CONTENT_LENGTH", "不接受分块上传请求。", 400)

    def common_headers(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")

    def respond(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise StoreError("CONTENT_LENGTH", "请求长度无效。")
        if length < 0 or length > MAX_BODY:
            raise StoreError("TOO_LARGE", "请求超过本地大小上限。", 413)
        try:
            raw = self.rfile.read(length)
            data = json.loads(raw or b"{}")
        except (ValueError, UnicodeError):
            raise StoreError("INVALID_JSON", "请求不是有效 JSON。")
        if not isinstance(data, dict):
            raise StoreError("INVALID_JSON", "请求必须为 JSON 对象。")
        return data

    def handle_error(self, error):
        if isinstance(error, StoreError):
            code, message, status = error.code, error.message, error.status
        else:
            code, message, status = "INTERNAL", "本地服务未能完成操作。未确认提交的对话可用相同请求重试。", 500
            # 只打印异常类型，不打印可能包含输入或密钥的异常正文。
            print("操作失败：" + type(error).__name__, file=sys.stderr, flush=True)
        if self.streaming:
            self.emit({"type": "error", "code": code, "message": message, "committed": getattr(self, "committed", False)})
        else:
            self.respond({"error": {"code": code, "message": message}}, status)

    def do_GET(self):
        try:
            self.security()
            url = urlsplit(self.path)
            path, query = url.path, parse_qs(url.query)
            if path == "/api/state":
                return self.respond(self.store.state())
            if path == "/api/audit":
                return self.respond(self.store.audit())
            if path == "/api/memories":
                status = query.get("status", ["active"])[0]
                if status not in {"active", "all"}:
                    raise StoreError("INVALID_STATUS", "记忆状态无效。")
                return self.respond({"memories": self.store.memories(query.get("q", [""])[0][:200], status)})
            match = re.fullmatch(r"/api/sessions/([a-zA-Z0-9_-]+)", path)
            if match:
                sid = match[1]
                return self.respond({"session": self.store.session(sid), "messages": self.store.messages(sid)})
            match = re.fullmatch(r"/api/memories/([a-zA-Z0-9_-]+)/history", path)
            if match:
                return self.respond({"revisions": self.store.memory_history(match[1])})
            static_files = {"/": "index.html", "/index.html": "index.html", "/style.css": "style.css", "/app.js": "app.js"}
            if path in static_files:
                file = ROOT / "static" / static_files[path]
                if not file.is_file():
                    raise StoreError("NOT_READY", "界面文件尚未就绪。", 503)
                data = file.read_bytes()
                self.send_response(200)
                self.common_headers()
                content_type = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
                self.send_header("Content-Type", content_type + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if path == "/favicon.ico":
                self.send_response(204)
                self.common_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raise StoreError("NOT_FOUND", "资源不存在。", 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.handle_error(exc)

    def do_POST(self):
        self.mutate("POST")

    def do_PATCH(self):
        self.mutate("PATCH")

    def do_DELETE(self):
        self.mutate("DELETE")

    def do_OPTIONS(self):
        self.respond({"error": {"code": "CORS_DISABLED", "message": "不允许跨站调用。"}}, 403)

    def mutate(self, method):
        try:
            self.security(write=True)
            data, path = self.body(), urlsplit(self.path).path
            if method == "POST" and path == "/api/chat":
                return self.chat(data)
            if method == "POST" and path == "/api/settings/test":
                settings = self.store.settings(public=False)
                reply = complete_json(settings, [{"role": "user", "content": "Reply OK."}], max_tokens=8)
                if not isinstance(reply, str) or not reply.strip():
                    raise StoreError("MODEL_EMPTY", "模型连接成功但未返回文字。", 502)
                return self.respond({"ok": True, "message": "连接成功，模型已返回响应。", "model": settings["model"]})
            with self.store.writer():
                if method == "POST" and path == "/api/sessions":
                    title = bounded_string(data.get("title", "新对话"), "标题", 80)
                    return self.respond({"session": self.store.create_session(title)}, 201)
                match = re.fullmatch(r"/api/sessions/([a-zA-Z0-9_-]+)", path)
                if match and method == "PATCH":
                    return self.respond({"session": self.store.change_session(match[1], bounded_string(data.get("title"), "标题", 80))})
                if match and method == "DELETE":
                    self.store.change_session(match[1], archive=True)
                    return self.respond({"ok": True})
                if method == "POST" and path == "/api/settings":
                    return self.respond({"settings": self.update_settings(data)})
                if method == "POST" and path == "/api/memories":
                    content = bounded_string(data.get("content"), "记忆", 500)
                    category = data.get("category", "profile")
                    if not isinstance(category, str) or category not in CATEGORIES:
                        raise StoreError("INVALID_CATEGORY", "记忆分类无效。")
                    if is_sensitive(content):
                        raise StoreError("SENSITIVE_MEMORY", "不把凭证、敏感号码或权限变更指令存入长期记忆。")
                    return self.respond({"memory": self.store.add_memory(content, category)}, 201)
                match = re.fullmatch(r"/api/memories/([a-zA-Z0-9_-]+)", path)
                if match and method == "PATCH":
                    values = {}
                    if "content" in data:
                        values["content"] = bounded_string(data["content"], "记忆", 500)
                        if is_sensitive(values["content"]):
                            raise StoreError("SENSITIVE_MEMORY", "该内容不适合保存为长期记忆。")
                    if "pinned" in data:
                        values["pinned"] = bool_value(data["pinned"], "置顶状态")
                    if "status" in data:
                        if data["status"] != "forgotten":
                            raise StoreError("INVALID_STATUS", "只允许主动忘记，不允许恢复历史失效记忆。")
                        values["status"] = "forgotten"
                    if not values:
                        raise StoreError("EMPTY_CHANGE", "没有提供可修改的字段。")
                    return self.respond({"memory": self.store.change_memory(match[1], values)})
                raise StoreError("NOT_FOUND", "接口不存在。", 404)
        except Exception as exc:
            self.handle_error(exc)

    def update_settings(self, data):
        values = self.store.settings(public=False)
        before_origin, before_provider = origin(values["base_url"]), values["provider"]
        if "mode" in data:
            if data["mode"] not in ("local", "openai"):
                raise StoreError("INVALID_MODE", "请选择本地预览或真实模型。")
            values["mode"] = data["mode"]
        for key, maximum in (("provider", 60), ("model", 120)):
            if key in data:
                values[key] = bounded_string(data[key], key, maximum)
        if "base_url" in data:
            values["base_url"] = normalize_base(data["base_url"])
        if before_origin != origin(values["base_url"]) or before_provider != values["provider"]:
            values["api_key"] = ""
        if "api_key" in data:
            if data["api_key"] is None:
                values["api_key"] = ""
            else:
                key = bounded_string(data["api_key"], "API key", 2048, allow_empty=True)
                if any(ord(c) < 32 or ord(c) > 126 for c in key):
                    raise StoreError("INVALID_KEY", "API key 含有无效字符。")
                if key:
                    values["api_key"] = key
        if "memory_enabled" in data:
            values["memory_enabled"] = bool_value(data["memory_enabled"], "记忆开关")
        return self.store.save_settings(values)

    def begin_stream(self):
        self.send_response(200)
        self.common_headers()
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.streaming = True
        self.close_connection = True

    def emit(self, event):
        if self.client_gone:
            return
        try:
            self.wfile.write((json.dumps(event, ensure_ascii=False) + "\n").encode())
            self.wfile.flush()
        except (OSError, TimeoutError):
            # 客户端断开不取消已开始的认知；最终状态可通过同 request_id 恢复。
            self.client_gone = True

    def finish_chat(self, result):
        self.committed = True
        self.emit({"type": "memory", "created": result["created"], "updated": result["updated"]})
        self.emit({"type": "done", "message": result["message"], "user_message": result["user_message"],
                   "session": result["session"], "stats": self.store.stats(), "mode": result["mode"],
                   "memory_notice": result.get("memory_notice")})

    def chat(self, data):
        sid = bounded_string(data.get("session_id"), "会话标识", 100)
        prompt = bounded_string(data.get("content"), "消息", 6000)
        rid = bounded_string(data.get("request_id"), "请求标识", 100)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", rid):
            raise StoreError("INVALID_REQUEST_ID", "请求标识格式无效。")
        # 整轮引擎调用到提交共享唯一写权；读取快照不受长推理锁阻塞。
        with self.store.writer():
            self.store.session(sid)
            previous = self.store.previous_request(rid, sid, prompt)
            if previous:
                self.begin_stream()
                self.emit({"type": "start", "request_id": rid, "mode": previous["mode"], "replayed": True})
                self.emit({"type": "delta", "text": previous["message"]["content"]})
                self.finish_chat(previous)
                return
            settings = self.store.settings(public=False)
            context = self.store.context(sid, prompt, settings["memory_enabled"])
            candidates = extract_candidates(prompt) if settings["memory_enabled"] else []
            self.begin_stream()
            self.emit({"type": "start", "request_id": rid, "mode": settings["mode"]})
            self.emit({"type": "context", "memories": context["memories"], "history_count": context["history_count"], "budget_chars": context["budget_chars"]})
            response, memory_notice = "", None
            if settings["mode"] == "local":
                response = local_reply(prompt, context["memories"], candidates, settings["memory_enabled"])
                # 分段只改善阅读呈现；不是伪造模型推理或延时。
                for index in range(0, len(response), 24):
                    self.emit({"type": "delta", "text": response[index:index+24]})
            else:
                for chunk in stream_reply(settings, prompt, context):
                    response += chunk
                    self.emit({"type": "delta", "text": chunk})
                if settings["memory_enabled"] and not is_sensitive(prompt):
                    try:
                        extracted = extract_with_model(settings, prompt, self.store.memories())
                        by_key = {item["key"]: item for item in extracted}
                        # 确定性规则的明确字段优先，避免模型扩展篡改已解析的事实。
                        by_key.update({item["key"]: item for item in candidates})
                        candidates = list(by_key.values())[:4]
                    except Exception:
                        memory_notice = "本轮模型记忆提取不可用，仅采用保守规则；回答已正常完成。"
            result = self.store.commit_chat(sid, prompt, response, rid, settings["mode"], context, candidates)
            if memory_notice:
                result["memory_notice"] = memory_notice
            self.finish_chat(result)


def main():
    parser = argparse.ArgumentParser(description="观澜 · 对话与长期记忆工作台")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--data-dir", default=str(ROOT / "runtime"))
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0–65535 之间")
    os.umask(0o077)
    directory = Path(args.data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = open(directory / "server.lock", "a+")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("此数据目录已有观澜实例运行，拒绝启动第二个写者。", file=sys.stderr)
        return 1
    try:
        store = Store(directory)
        server = AppServer(("127.0.0.1", args.port), store)
    except Exception as exc:
        print("启动失败：" + str(exc), file=sys.stderr)
        return 1
    actual = server.server_address[1]
    print("观澜已启动：http://127.0.0.1:%d" % actual, flush=True)
    print("仅本机访问；本地预览为规则引擎，连接模型请打开设置。", flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
