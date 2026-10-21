"""任务元数据（SQLite）。比赛期单机部署足够；表结构保持简单，后续可换 PostgreSQL。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# Keep delivery and pause states explicit even though SQLite stores TEXT. The
# worker, polling API and restart recovery must agree about warning deliveries.
TERMINAL_STATUSES = frozenset({"READY", "READY_WITH_WARNINGS", "FAILED", "CANCELLED"})
PAUSED_STATUSES = frozenset({"WAITING_CONFIRMATION"})

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  prompt TEXT NOT NULL,
  file_ref TEXT,
  parent_id TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL,
  result_json TEXT,
  messages_json TEXT
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL,
  ts REAL NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
"""


class DB:
    def __init__(self, path: Path):
        self.path=Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock:
            self.conn.executescript(SCHEMA)
            columns={r[1] for r in self.conn.execute('PRAGMA table_info(tasks)')}
            if 'state_json' not in columns:self.conn.execute('ALTER TABLE tasks ADD COLUMN state_json TEXT')
            from zhihui.kb.gaps import SCHEMA as gap_schema
            self.conn.execute(gap_schema)
            self.conn.commit()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def create_task(self, task_id: str, prompt: str, file_ref: str | None, parent_id: str | None = None) -> None:
        now = time.time()
        self._exec(
            "INSERT INTO tasks(id,status,prompt,file_ref,parent_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, "QUEUED", prompt, file_ref, parent_id, now, now),
        )
        self.add_event(task_id, "status", {"status": "QUEUED"})

    def set_status(self, task_id: str, status: str) -> None:
        self._exec("UPDATE tasks SET status=?, updated_at=? WHERE id=?", (status, time.time(), task_id))

    def set_result(self, task_id: str, status: str, result: dict, messages: list | None = None) -> None:
        self._exec(
            "UPDATE tasks SET status=?, updated_at=?, result_json=?, messages_json=? WHERE id=?",
            (status, time.time(), json.dumps(result, ensure_ascii=False), json.dumps(messages, ensure_ascii=False) if messages else None, task_id),
        )

    def add_event(self, task_id: str, kind: str, payload: dict) -> None:
        self._exec("INSERT INTO events(task_id,ts,kind,payload_json) VALUES(?,?,?,?)", (task_id, time.time(), kind, json.dumps(payload, ensure_ascii=False)))

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self.lock:
            row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["result"] = json.loads(d.pop("result_json") or "null")
        d["messages"] = json.loads(d.pop("messages_json") or "null")
        d['state']=json.loads(d.pop('state_json',None) or 'null')
        return d

    def list_tasks(self, limit: int = 30) -> list[dict[str, Any]]:
        with self.lock:
            rows = self.conn.execute("SELECT id,status,prompt,parent_id,created_at,updated_at FROM tasks ORDER BY updated_at DESC LIMIT ?", (max(1, min(int(limit), 200)),)).fetchall()
        return [dict(r) for r in rows]

    def set_state(self, task_id: str, state: dict) -> None:
        self._exec('UPDATE tasks SET state_json=?,updated_at=? WHERE id=?',(json.dumps(state,ensure_ascii=False),time.time(),task_id))

    def events(self, task_id: str, after: int = 0) -> list[dict]:
        with self.lock:
            rows = self.conn.execute("SELECT id,ts,kind,payload_json FROM events WHERE task_id=? AND id>? ORDER BY id", (task_id, after)).fetchall()
        return [{"id": r["id"], "ts": r["ts"], "kind": r["kind"], **json.loads(r["payload_json"])} for r in rows]

    def recover_interrupted(self) -> list[str]:
        """进程重启时，把跑到一半的任务标成失败（原件不受影响）。"""
        with self.lock:
            rows = self.conn.execute("SELECT id FROM tasks WHERE status IN ('PLANNING','RUNNING','VALIDATING','REPAIRING')").fetchall()
        ids = [r["id"] for r in rows]
        for i in ids:
            self.set_result(i, "FAILED", {"message": "服务重启，任务中断，请重新提交"})
        return ids
