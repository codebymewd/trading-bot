"""SQLite storage (single-user start; the schema is plain SQL so a PostgreSQL migration is mechanical)."""
from __future__ import annotations

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional


class Db:
    def __init__(self, path: str = ":memory:"):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS config_versions(version INTEGER PRIMARY KEY, json TEXT, hash TEXT,
                created INTEGER, actor TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, actor TEXT, action TEXT, detail TEXT);
            CREATE TABLE IF NOT EXISTS events(event_id TEXT PRIMARY KEY, ts INTEGER, type TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS commands(command_id TEXT PRIMARY KEY, ts INTEGER, kind TEXT, payload TEXT, status TEXT);
            CREATE TABLE IF NOT EXISTS nonces(nonce TEXT PRIMARY KEY, ts INTEGER);
            CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
            """)

    def _run(self, sql: str, params=()):
        with self.lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    # ---- audit
    def audit(self, ts: int, actor: str, action: str, detail: Any = "") -> None:
        self._run("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (ts, str(actor), action, json.dumps(detail)))

    def audit_log(self, limit: int = 20) -> List[dict]:
        rows = self._run("SELECT ts,actor,action,detail FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": r["ts"], "actor": r["actor"], "action": r["action"], "detail": json.loads(r["detail"])} for r in rows]

    # ---- key/value
    def kv_set(self, key: str, value: Any) -> None:
        self._run("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value)))

    def kv_get(self, key: str, default: Any = None) -> Any:
        r = self._run("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(r["value"]) if r else default

    # ---- events (idempotent by event_id)
    def add_event(self, event_id: str, ts: int, typ: str, payload: dict) -> bool:
        cur = self._run("INSERT OR IGNORE INTO events(event_id,ts,type,payload) VALUES(?,?,?,?)",
                        (event_id, ts, typ, json.dumps(payload)))
        return cur.rowcount == 1

    def events(self, types: Optional[List[str]] = None, since: int = 0, limit: int = 50) -> List[dict]:
        sql, params = "SELECT event_id,ts,type,payload FROM events WHERE ts>=?", [since]
        if types:
            sql += " AND type IN (%s)" % ",".join("?" * len(types))
            params += types
        sql += " ORDER BY ts DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [{"event_id": r["event_id"], "ts": r["ts"], "type": r["type"], **json.loads(r["payload"])}
                for r in self._run(sql, params).fetchall()]

    # ---- commands to the EA
    def add_command(self, command_id: str, ts: int, kind: str, payload: dict) -> None:
        self._run("INSERT OR IGNORE INTO commands VALUES(?,?,?,?, 'pending')", (command_id, ts, kind, json.dumps(payload)))

    def pending_commands(self) -> List[dict]:
        return [{"command_id": r["command_id"], "kind": r["kind"], **json.loads(r["payload"])}
                for r in self._run("SELECT * FROM commands WHERE status='pending' ORDER BY ts").fetchall()]

    def set_command_status(self, command_id: str, status: str) -> None:
        self._run("UPDATE commands SET status=? WHERE command_id=?", (status, command_id))

    # ---- replay protection
    def add_nonce(self, nonce: str, ts: int) -> bool:
        return self._run("INSERT OR IGNORE INTO nonces VALUES(?,?)", (nonce, ts)).rowcount == 1

    def purge_nonces(self, before: int) -> None:
        self._run("DELETE FROM nonces WHERE ts<?", (before,))

    # ---- config versions
    def save_config(self, version: int, js: str, h: str, ts: int, actor: str, status: str) -> None:
        self._run("INSERT OR REPLACE INTO config_versions VALUES(?,?,?,?,?,?)", (version, js, h, ts, actor, status))

    def set_config_status(self, version: int, status: str) -> None:
        self._run("UPDATE config_versions SET status=? WHERE version=?", (status, version))

    def latest_config(self) -> Optional[dict]:
        r = self._run("SELECT * FROM config_versions ORDER BY version DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    def last_applied_version(self) -> int:
        r = self._run("SELECT MAX(version) AS v FROM config_versions WHERE status='applied'").fetchone()
        return int(r["v"]) if r and r["v"] is not None else 0
