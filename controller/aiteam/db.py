"""작은 상태 DB(SQLite)와 작업 큐. 모든 상태 전환은 일반 코드로 처리한다."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone

from .paths import P

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS projects(
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE, name TEXT, goal TEXT, idea TEXT, customers TEXT,
  stage TEXT DEFAULT 'planning', paused INTEGER DEFAULT 0, is_demo INTEGER DEFAULT 0,
  source_path TEXT, settings_json TEXT DEFAULT '{}', app_version TEXT DEFAULT '0.1.0',
  created_at TEXT);
CREATE TABLE IF NOT EXISTS dept_state(dept TEXT PRIMARY KEY, paused INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS cycles(
  id INTEGER PRIMARY KEY, project_id INTEGER, kind TEXT, proposal_id INTEGER,
  approved INTEGER DEFAULT 0, status TEXT DEFAULT 'open', base_commit TEXT,
  plan_json TEXT, design_json TEXT, verified_json TEXT, fail_sigs TEXT DEFAULT '[]',
  dev_chain INTEGER DEFAULT 0, created_at TEXT, closed_at TEXT);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, project_id INTEGER, cycle_id INTEGER, dept TEXT, kind TEXT,
  title TEXT, input_json TEXT DEFAULT '{}', dedup_key TEXT, status TEXT DEFAULT 'queued',
  priority INTEGER DEFAULT 3, is_fallback INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0,
  same_cause_count INTEGER DEFAULT 0, last_error_sig TEXT, last_error TEXT,
  retry_after REAL DEFAULT 0, backoff_count INTEGER DEFAULT 0,
  token_limit INTEGER, time_limit_sec INTEGER, done_criteria TEXT,
  result_summary TEXT, output_json TEXT, next_dept TEXT, note TEXT,
  created_at TEXT, started_at TEXT, finished_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS tasks_dedup ON tasks(dedup_key)
  WHERE status NOT IN ('failed','cancelled');
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY, project_id INTEGER, cycle_id INTEGER, kind TEXT, title TEXT,
  body_json TEXT, status TEXT DEFAULT 'pending', answer_json TEXT, dedup_key TEXT UNIQUE,
  blocks TEXT, requested_at TEXT, answered_at TEXT);
CREATE TABLE IF NOT EXISTS inquiries(
  id INTEGER PRIMARY KEY, project_id INTEGER, receipt_no TEXT UNIQUE, lookup_hash TEXT,
  category TEXT, content TEXT, expected TEXT, app_version TEXT, screen TEXT, device TEXT,
  contact TEXT, attachments_json TEXT DEFAULT '[]', customer_key TEXT, dedup_hash TEXT,
  repeat_of_customer INTEGER DEFAULT 0, status TEXT DEFAULT 'received', cluster_id INTEGER,
  source TEXT DEFAULT 'app', created_at TEXT);
CREATE TABLE IF NOT EXISTS clusters(
  id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, problem TEXT,
  requested_feature TEXT, underlying_problem TEXT, kind TEXT, severity TEXT, impact TEXT,
  reproducible INTEGER DEFAULT 0, facts_json TEXT DEFAULT '[]', estimates_json TEXT DEFAULT '[]',
  quotes_json TEXT DEFAULT '[]', reply_draft TEXT, status TEXT DEFAULT 'open',
  created_at TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS proposals(
  id INTEGER PRIMARY KEY, project_id INTEGER, batch_task INTEGER, title TEXT, body_json TEXT,
  size TEXT, status TEXT DEFAULT 'proposed', scope_note TEXT, cluster_ids TEXT DEFAULT '[]',
  inquiry_ids TEXT DEFAULT '[]', decided_at TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS usage(
  id INTEGER PRIMARY KEY, task_id INTEGER, attempt INTEGER, project_id INTEGER, dept TEXT,
  model TEXT, input_tokens INTEGER, output_tokens INTEGER, cache_read INTEGER,
  cache_write INTEGER, cost_usd REAL, status TEXT, is_mock INTEGER DEFAULT 0,
  reserved_usd REAL DEFAULT 0, session_id TEXT, result_uuid TEXT, day TEXT, month TEXT,
  created_at TEXT, UNIQUE(task_id, attempt, model));
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT, project_id INTEGER, dept TEXT, task_id INTEGER,
  kind TEXT, message TEXT, is_mock INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS artifacts(
  id INTEGER PRIMARY KEY, project_id INTEGER, task_id INTEGER, dept TEXT, kind TEXT,
  path TEXT, title TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS qa_results(
  id INTEGER PRIMARY KEY, project_id INTEGER, cycle_id INTEGER, task_id INTEGER, verdict TEXT,
  summary TEXT, checks_json TEXT, findings_json TEXT, evidence TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS releases(
  id INTEGER PRIMARY KEY, project_id INTEGER, cycle_id INTEGER, version TEXT,
  status TEXT DEFAULT 'prepared', notes TEXT, created_at TEXT, deployed_at TEXT);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_day(ts=None):
    d = datetime.now().astimezone() if ts is None else ts
    return d.strftime("%Y-%m-%d")


class DB:
    def __init__(self, path=None):
        self.path = str(path or P.db_file)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None,
                                    timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
                self.conn.execute("COMMIT")
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise

    def x(self, sql, args=()):
        with self.lock:
            return self.conn.execute(sql, args)

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def insert(self, table, **cols):
        keys = ",".join(cols)
        qs = ",".join("?" for _ in cols)
        with self.lock:
            cur = self.conn.execute(f"INSERT INTO {table}({keys}) VALUES({qs})",
                                    tuple(cols.values()))
            return cur.lastrowid

    def update(self, table, row_id, **cols):
        sets = ",".join(f"{k}=?" for k in cols)
        with self.lock:
            self.conn.execute(f"UPDATE {table} SET {sets} WHERE id=?", (*cols.values(), row_id))

    def meta_get(self, key, default=None):
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return json.loads(r["value"]) if r else default

    def meta_set(self, key, value):
        self.x("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, json.dumps(value, ensure_ascii=False)))

    def event(self, kind, message, project_id=None, dept=None, task_id=None, is_mock=False):
        from .safety import redact
        self.insert("events", ts=now(), project_id=project_id, dept=dept, task_id=task_id,
                    kind=kind, message=redact(str(message))[:2000], is_mock=int(bool(is_mock)))

    def close(self):
        with self.lock:
            self.conn.close()


def jl(s, default=None):
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except ValueError:
        return default


def jd(v):
    return json.dumps(v, ensure_ascii=False)
