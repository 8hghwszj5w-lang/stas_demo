"""本地单主体事务存储：对话、可追溯记忆及哈希链审计。"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from datetime import datetime, timezone
import uuid


DEFAULT_SETTINGS = {
    "mode": "local", "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
    "model": "deepseek-chat", "api_key": "", "memory_enabled": True,
}
CATEGORIES = {"profile", "preference", "goal", "boundary"}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def timestamp():
    # 墙钟只用于界面展示；不进入事件哈希、认知决策或记忆排序评分。
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix):
    # 随机标识用于资源身份，不用于模型推理；本演示不承诺逐位认知重放。
    return prefix + "_" + uuid.uuid4().hex


class StoreError(Exception):
    def __init__(self, code, message, status=400):
        self.code, self.message, self.status = code, message, status
        super().__init__(message)


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.directory, 0o700)
        self.path = self.directory / "guanlan.sqlite3"
        self.write_lock = threading.Lock()
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, subject_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, archived INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
                    mode TEXT NOT NULL, context_memory_ids TEXT NOT NULL, request_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id);
                CREATE TABLE IF NOT EXISTS memories (
                    id TEXT PRIMARY KEY, key TEXT NOT NULL, category TEXT NOT NULL, content TEXT NOT NULL,
                    status TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL,
                    source_session_id TEXT, source_message_id TEXT, source_quote TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, superseded_by TEXT);
                CREATE INDEX IF NOT EXISTS memories_key ON memories(key);
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_key ON memories(key) WHERE status='active';
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    prev TEXT NOT NULL, hash TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES(1,?,?)", (canonical(DEFAULT_SETTINGS), new_id("subject")))
            db.commit()
        os.chmod(self.path, 0o600)
        if not self.audit()["chain_valid"]:
            raise RuntimeError("审计链校验失败，拒绝启动；请保留数据副本并检查损坏。")

    @contextlib.contextmanager
    def connection(self):
        db = sqlite3.connect(str(self.path), timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        try:
            yield db
        finally:
            db.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except Exception:
                db.rollback()
                raise

    @contextlib.contextmanager
    def writer(self):
        if not self.write_lock.acquire(blocking=False):
            raise StoreError("BUSY", "正在完成一次对话或状态变更，请稍后重试。", 409)
        try:
            yield
        finally:
            self.write_lock.release()

    def _event(self, db, kind, payload):
        last = db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, prev = (last["seq"] + 1, last["hash"]) if last else (0, "GENESIS")
        body = {"seq": seq, "kind": kind, "payload": payload, "prev": prev}
        digest = hashlib.sha256(canonical(body).encode()).hexdigest()
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?)", (seq, kind, canonical(payload), prev, digest, timestamp()))

    def audit(self):
        prev, valid, count = "GENESIS", True, 0
        with self.connection() as db:
            for row in db.execute("SELECT * FROM events ORDER BY seq"):
                try:
                    body = {"seq": row["seq"], "kind": row["kind"], "payload": json.loads(row["payload"]), "prev": row["prev"]}
                    digest = hashlib.sha256(canonical(body).encode()).hexdigest()
                    valid = valid and row["seq"] == count and row["prev"] == prev and row["hash"] == digest
                except (ValueError, TypeError):
                    valid = False
                prev, count = row["hash"], count + 1
        return {"chain_valid": bool(valid), "event_count": count, "head_hash": prev,
                "scope": "单机单主体的状态变更元数据链；SQLite 事务提交对话与记忆。",
                "limitations": ["不是硬件唯一性证明或完整 STAS 合规认证", "不提供链外锚定，不能阻止持有数据库的人重写全链", "元数据审计不等于全部内容的独立校验或真实模型逐位重放", "忘记为检索与上下文排除，不是聊天原文物理擦除"]}

    def settings(self, public=True):
        with self.connection() as db:
            result = json.loads(db.execute("SELECT data FROM settings WHERE id=1").fetchone()[0])
        if public:
            key = result.pop("api_key", "")
            result.update(has_api_key=bool(key), api_key_masked=(("••••" + key[-4:] if len(key) > 8 else "••••••••") if key else None))
        return result

    def save_settings(self, values):
        with self.transaction() as db:
            db.execute("UPDATE settings SET data=? WHERE id=1", (canonical(values),))
            self._event(db, "settings_changed", {"mode": values["mode"], "memory_enabled": values["memory_enabled"],
                                               "provider": values["provider"], "model": values["model"]})
        return self.settings()

    def stats(self):
        with self.connection() as db:
            result = {
                "active_memories": db.execute("SELECT count(*) FROM memories WHERE status='active'").fetchone()[0],
                "sessions": db.execute("SELECT count(*) FROM sessions WHERE archived=0").fetchone()[0],
                "messages": db.execute("SELECT count(*) FROM messages").fetchone()[0],
                "events": db.execute("SELECT count(*) FROM events").fetchone()[0],
            }
        result["chain_valid"] = self.audit()["chain_valid"]
        return result

    def state(self):
        with self.connection() as db:
            subject = db.execute("SELECT subject_id FROM settings WHERE id=1").fetchone()[0]
        return {"identity": {"id": subject, "name": "观澜"}, "settings": self.settings(),
                "sessions": self.sessions(), "memories": self.memories(), "stats": self.stats(), "busy": self.write_lock.locked()}

    def sessions(self):
        with self.connection() as db:
            return [self._session(row) for row in db.execute("""SELECT s.*, (SELECT count(*) FROM messages m WHERE m.session_id=s.id) AS message_count
                FROM sessions s WHERE archived=0 ORDER BY updated_at DESC, rowid DESC""")]

    @staticmethod
    def _session(row):
        result = dict(row)
        result["archived"] = bool(result["archived"])
        return result

    def session(self, sid, db=None):
        if db is None:
            with self.connection() as connection:
                return self.session(sid, connection)
        row = db.execute("SELECT s.*, (SELECT count(*) FROM messages WHERE session_id=s.id) AS message_count FROM sessions s WHERE id=?", (sid,)).fetchone()
        if not row or row["archived"]:
            raise StoreError("NOT_FOUND", "会话不存在或已归档。", 404)
        return self._session(row)

    def create_session(self, title="新对话"):
        sid, now = new_id("session"), timestamp()
        with self.transaction() as db:
            db.execute("INSERT INTO sessions VALUES(?,?,?,?,0)", (sid, title[:80], now, now))
            self._event(db, "session_created", {"session_id": sid})
        return self.session(sid)

    def change_session(self, sid, title=None, archive=False):
        with self.transaction() as db:
            self.session(sid, db)
            if archive:
                db.execute("UPDATE sessions SET archived=1 WHERE id=?", (sid,))
            else:
                db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?", (title[:80], timestamp(), sid))
            self._event(db, "session_archived" if archive else "session_renamed", {"session_id": sid})
        return None if archive else self.session(sid)

    @staticmethod
    def _message(row):
        result = dict(row)
        result["context_memory_ids"] = json.loads(result["context_memory_ids"])
        return result

    def messages(self, sid):
        self.session(sid)
        with self.connection() as db:
            return [self._message(row) for row in db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid", (sid,))]

    @staticmethod
    def _memory(row):
        result = dict(row)
        result["pinned"] = bool(result["pinned"])
        return result

    def memories(self, query="", status="active"):
        with self.connection() as db:
            sql, params = "SELECT * FROM memories", []
            clauses = []
            if status != "all":
                clauses.append("status='active'")
            if query:
                clauses.append("instr(lower(content),lower(?))>0")
                params.append(query)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY pinned DESC, rowid DESC"
            return [self._memory(row) for row in db.execute(sql, params)]

    def memory(self, mid, db=None):
        if db is None:
            with self.connection() as connection:
                return self.memory(mid, connection)
        row = db.execute("SELECT * FROM memories WHERE id=?", (mid,)).fetchone()
        if not row:
            raise StoreError("NOT_FOUND", "记忆不存在。", 404)
        return self._memory(row)

    def memory_history(self, mid):
        item = self.memory(mid)
        with self.connection() as db:
            return [self._memory(row) for row in db.execute("SELECT * FROM memories WHERE key=? ORDER BY version DESC", (item["key"],))]

    def _remember(self, db, candidate, source_session=None, source_message=None, pinned=False, force=False):
        key = candidate["key"][:160]
        old = db.execute("SELECT * FROM memories WHERE key=? AND status='active'", (key,)).fetchone()
        if old and old["content"].strip().casefold() == candidate["content"].strip().casefold():
            return None, False
        # 自动抽取不能覆盖用户手动更正/置顶的记忆；显式编辑仍可修改。
        if old and old["pinned"] and not force:
            return None, False
        maximum = db.execute("SELECT max(version) FROM memories WHERE key=?", (key,)).fetchone()[0] or 0
        mid, now = new_id("memory"), timestamp()
        if old:
            db.execute("UPDATE memories SET status='superseded',superseded_by=?,updated_at=? WHERE id=?", (mid, now, old["id"]))
            pinned = pinned or bool(old["pinned"])
        db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            mid, key, candidate["category"], candidate["content"], "active", int(pinned), maximum + 1,
            source_session, source_message, candidate.get("source_quote", ""), now, now, None))
        return self.memory(mid, db), bool(old)

    def add_memory(self, content, category):
        with self.transaction() as db:
            item, _ = self._remember(db, {"key": "manual:" + uuid.uuid4().hex, "category": category,
                "content": content, "source_quote": "用户主动添加"}, pinned=True)
            self._event(db, "memory_added", {"memory_id": item["id"], "content_hash": hashlib.sha256(content.encode()).hexdigest()})
        return item

    def change_memory(self, mid, values):
        with self.transaction() as db:
            old = self.memory(mid, db)
            if old["status"] != "active":
                raise StoreError("INACTIVE_MEMORY", "该记忆已被取代或忘记，请查看最新版本。", 409)
            if values.get("status") == "forgotten":
                # 同属性全部历史版本失效；防止旧版本从另一入口重新注入。
                db.execute("UPDATE memories SET status='forgotten',updated_at=? WHERE key=?", (timestamp(), old["key"]))
                item = self.memory(mid, db)
                kind = "memory_forgotten"
            elif "content" in values and values["content"].strip() != old["content"]:
                item, _ = self._remember(db, {"key": old["key"], "category": old["category"],
                    "content": values["content"], "source_quote": "用户手动纠正"}, pinned=True, force=True)
                kind = "memory_corrected"
            else:
                if "pinned" in values:
                    db.execute("UPDATE memories SET pinned=?,updated_at=? WHERE id=?", (int(values["pinned"]), timestamp(), mid))
                item, kind = self.memory(mid, db), "memory_pinned"
            self._event(db, kind, {"memory_id": item["id"], "previous_id": mid, "version": item["version"]})
        return item

    def context(self, sid, prompt, memory_enabled=True):
        from memory import relevance_score
        self.session(sid)
        recalled, used = [], 0
        if memory_enabled:
            ranked = []
            for item in self.memories():
                score = relevance_score(prompt, item) + (10 if item["pinned"] else 0)
                if item["key"] in {"preference.reply_style", "boundary.emoji"}:
                    score = max(score, 0.6)  # 交流偏好适用于日常回答，不要求用户再次问到偏好。
                if score > 0:
                    ranked.append((score, item))
            ranked.sort(key=lambda pair: (-pair[0], pair[1]["key"], pair[1]["id"]))
            for _, item in ranked:
                size = len(item["content"]) + 80
                if used + size <= 2400 and len(recalled) < 6:
                    recalled.append(item)
                    used += size
        # 从日志引用反查，不让被忘记的内容通过近期聊天窗口重新进入模型。
        with self.connection() as db:
            inactive = list(db.execute("SELECT id,source_message_id FROM memories" + (" WHERE status!='active'" if memory_enabled else "")))
            blocked_ids = {row["id"] for row in inactive}
            blocked_sources = {row["source_message_id"] for row in inactive if row["source_message_id"]}
            # 只读最后 8 轮，避免每次扫描整段生命史。
            rows = list(db.execute("SELECT * FROM messages WHERE session_id=? ORDER BY rowid DESC LIMIT 16", (sid,)))
        messages = [self._message(row) for row in reversed(rows)]
        source_memory_ids = {}
        if messages:
            with self.connection() as db:
                placeholders = ",".join("?" for _ in messages)
                source_rows = db.execute("SELECT id,source_message_id FROM memories WHERE source_message_id IN (" + placeholders + ")", [m["id"] for m in messages])
                for row in source_rows:
                    source_memory_ids.setdefault(row["source_message_id"], set()).add(row["id"])
        history, history_chars = [], 0
        provenance = {m["id"] for m in recalled}
        rounds = [messages[i:i+2] for i in range(0, len(messages), 2)]
        for pair in reversed(rounds):
            if len(pair) != 2 or pair[0]["role"] != "user" or pair[1]["role"] != "assistant":
                continue
            if any(m["id"] in blocked_sources or set(m["context_memory_ids"]) & blocked_ids for m in pair):
                continue
            size = sum(len(m["content"]) for m in pair)
            if history_chars + size > 12000:
                break
            history[0:0] = [{"role": m["role"], "content": m["content"]} for m in pair]
            for message in pair:
                provenance.update(message["context_memory_ids"])
                provenance.update(source_memory_ids.get(message["id"], set()))
            history_chars += size
        return {"memories": recalled, "history": history, "history_count": len(history)//2,
                "provenance_memory_ids": sorted(provenance), "budget_chars": 20400}

    @staticmethod
    def fingerprint(sid, content):
        return hashlib.sha256(canonical({"session_id": sid, "content": content}).encode()).hexdigest()

    def previous_request(self, rid, sid, content):
        with self.connection() as db:
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
        if not row:
            return None
        if row["fingerprint"] != self.fingerprint(sid, content):
            raise StoreError("REQUEST_CONFLICT", "同一请求标识不能用于不同内容，请重新发送。", 409)
        return json.loads(row["result"])

    def commit_chat(self, sid, prompt, response, rid, mode, context, candidates):
        existing = self.previous_request(rid, sid, prompt)
        if existing:
            return existing
        now = timestamp()
        context_ids = sorted({m["id"] for m in context["memories"]} | set(context.get("provenance_memory_ids", [])))
        user = {"id": new_id("message"), "session_id": sid, "role": "user", "content": prompt,
                "created_at": now, "mode": mode, "context_memory_ids": context_ids, "request_id": rid}
        answer = dict(user, id=new_id("message"), role="assistant", content=response)
        created, updated = [], []
        with self.transaction() as db:
            session = self.session(sid, db)
            for message in (user, answer):
                db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?,?,?)", (message["id"], sid, message["role"],
                    message["content"], now, mode, canonical(context_ids), rid))
            for candidate in candidates[:4]:
                item, revised = self._remember(db, candidate, sid, user["id"])
                if item:
                    (updated if revised else created).append(item)
            title = session["title"]
            if session["message_count"] == 0 and title == "新对话":
                title = prompt.replace("\n", " ")[:28] + ("…" if len(prompt) > 28 else "")
            db.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?", (title, now, sid))
            self._event(db, "thought_committed", {"request_id": rid, "session_id": sid,
                "user_message_id": user["id"], "assistant_message_id": answer["id"], "mode": mode,
                "input_hash": hashlib.sha256(prompt.encode()).hexdigest(), "output_hash": hashlib.sha256(response.encode()).hexdigest(),
                "memory_ids": [m["id"] for m in created + updated], "context_memory_ids": context_ids})
            result = {"message": answer, "user_message": user, "session": self.session(sid, db),
                      "created": created, "updated": updated, "mode": mode}
            db.execute("INSERT INTO requests VALUES(?,?,?)", (rid, self.fingerprint(sid, prompt), canonical(result)))
        return result
