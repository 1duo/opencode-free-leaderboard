from __future__ import annotations

import fcntl
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .config import now

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS discovery (
 id INTEGER PRIMARY KEY, observed_at TEXT NOT NULL, week TEXT NOT NULL,
 ok INTEGER NOT NULL, evidence TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS models (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, endpoint TEXT, protocol TEXT,
 status TEXT NOT NULL, epoch TEXT NOT NULL, cohort INTEGER NOT NULL,
 profile TEXT NOT NULL, observed_at TEXT NOT NULL, evidence_id INTEGER REFERENCES discovery(id));
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, discovery_id INTEGER REFERENCES discovery(id), model_id TEXT NOT NULL,
 epoch TEXT NOT NULL, status TEXT NOT NULL, evidence TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS seasons (
 id TEXT PRIMARY KEY, created_at TEXT NOT NULL, manifest TEXT NOT NULL,
 validated INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS items (
 id TEXT PRIMARY KEY, season TEXT REFERENCES seasons(id), benchmark TEXT NOT NULL,
 stratum TEXT NOT NULL, question_hash TEXT NOT NULL, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS panels (
 season TEXT REFERENCES seasons(id), tier TEXT NOT NULL, item_id TEXT REFERENCES items(id),
 PRIMARY KEY(season,tier,item_id));
CREATE TABLE IF NOT EXISTS cycles (
 id TEXT PRIMARY KEY, model_id TEXT NOT NULL, epoch TEXT NOT NULL, season TEXT NOT NULL,
 kind TEXT NOT NULL, started_at TEXT NOT NULL, due_at TEXT NOT NULL,
 completed_at TEXT, confirmation_requested INTEGER NOT NULL DEFAULT 0, profile TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id INTEGER PRIMARY KEY, cycle_id TEXT REFERENCES cycles(id), item_id TEXT REFERENCES items(id),
 status TEXT NOT NULL DEFAULT 'pending', score REAL, response_path TEXT,
 latency REAL, truncated INTEGER NOT NULL DEFAULT 0, next_after TEXT, error TEXT,
 UNIQUE(cycle_id,item_id));
CREATE TABLE IF NOT EXISTS budgets (
 week TEXT PRIMARY KEY, attempts_limit INTEGER NOT NULL, tokens_limit INTEGER NOT NULL,
 planned_attempts INTEGER NOT NULL DEFAULT 0, planned_tokens INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS attempts (
 id INTEGER PRIMARY KEY, job_id INTEGER REFERENCES jobs(id), model_id TEXT,
 week TEXT NOT NULL REFERENCES budgets(week), kind TEXT NOT NULL, started_at TEXT NOT NULL,
 finished_at TEXT, status TEXT NOT NULL DEFAULT 'reserved', http_status INTEGER,
 reserved_tokens INTEGER NOT NULL, accounted_tokens INTEGER NOT NULL,
 reported_tokens INTEGER, usage TEXT, response_path TEXT);
CREATE TABLE IF NOT EXISTS publications (
 id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL, created_at TEXT NOT NULL,
 commit_sha TEXT, deployment_status TEXT NOT NULL);
PRAGMA user_version=1;
"""


class DB:
    def __init__(self, state: Path):
        self.state = state
        self.path = state / "state.sqlite3"
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.path.chmod(0o600)

    def rows(self, sql: str, args: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, args)]

    def one(self, sql: str, args: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.conn:
            return self.conn.execute(sql, args)

    @contextmanager
    def locked(self):
        with (self.state / "runner.lock").open("a") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("Another leaderboard process is running") from None
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def backup(self) -> None:
        target = self.state / "backups" / f"{now()[:10]}.sqlite3"
        with sqlite3.connect(target) as conn:
            self.conn.backup(conn)
        target.chmod(0o600)
        for old in sorted((self.state / "backups").glob("*.sqlite3"))[:-7]:
            old.unlink()

    def recover(self) -> None:
        # A durable response can be graded again; an unknown request cannot be redispatched.
        for attempt in self.rows("SELECT * FROM attempts WHERE status='reserved'"):
            path = self.state / "responses" / f"{attempt['id']}.json"
            if path.exists() and attempt["job_id"]:
                json.loads(path.read_text())  # Ensure the atomic response is intact.
                self.execute("UPDATE jobs SET status='generated', response_path=? WHERE id=?",
                             (str(path), attempt["job_id"]))
                self.execute("UPDATE attempts SET status='recovered', finished_at=?, response_path=? WHERE id=?",
                             (now(), str(path), attempt["id"]))
            else:
                self.execute("UPDATE attempts SET status='ambiguous', finished_at=? WHERE id=?",
                             (now(), attempt["id"]))
                if attempt["job_id"]:
                    self.execute("UPDATE jobs SET status='ambiguous', error='Outcome unknown; no automatic replay' WHERE id=?",
                                 (attempt["job_id"],))

