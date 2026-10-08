from __future__ import annotations

import json
import math

from .config import Settings, now, week
from .db import DB


class BudgetExhausted(RuntimeError):
    pass


def estimate(messages: list[dict], cap: int) -> int:
    # Byte-based bound deliberately exceeds common tokenizer estimates. Not billed dollars.
    return len(json.dumps(messages, ensure_ascii=False).encode()) + 256 + cap


def usage_total(usage: dict | None) -> int | None:
    if not usage:
        return None
    if isinstance(usage.get("total_tokens"), int):
        return max(0, usage["total_tokens"])
    for a, b in [("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")]:
        if isinstance(usage.get(a), int) and isinstance(usage.get(b), int):
            # Cached tokens and reasoning are subsets of these totals, not additional tokens.
            return max(0, usage[a]) + max(0, usage[b])
    return None


class Budget:
    def __init__(self, db: DB, settings: Settings):
        self.db, self.settings = db, settings

    def plan(self, generation_tokens: list[int], probes: int, metadata: int = 2) -> dict:
        key = week()
        planned_attempts = math.ceil((len(generation_tokens) + probes + metadata) * 1.25)
        planned_tokens = math.ceil((sum(generation_tokens) + probes * 1536) * 1.25)
        self.db.execute("""INSERT INTO budgets VALUES(?,?,?,?,?) ON CONFLICT(week) DO UPDATE SET
            attempts_limit=MAX(attempts_limit,excluded.attempts_limit),
            tokens_limit=MAX(tokens_limit,excluded.tokens_limit),
            planned_attempts=excluded.planned_attempts, planned_tokens=excluded.planned_tokens""",
                        (key, max(self.settings.minimum_attempts, planned_attempts),
                         max(self.settings.minimum_tokens, planned_tokens),
                         planned_attempts, planned_tokens))
        return self.summary()

    def summary(self) -> dict:
        key = week()
        limits = self.db.one("SELECT * FROM budgets WHERE week=?", (key,))
        if not limits:
            self.plan([], 0)
            return self.summary()
        used = self.db.one("""SELECT COUNT(*) AS attempts_used,
            COALESCE(SUM(accounted_tokens),0) AS accounted_tokens,
            COALESCE(SUM(reported_tokens),0) AS reported_tokens,
            SUM(reported_tokens IS NULL AND reserved_tokens>0) AS estimated_attempts
            FROM attempts WHERE week=?""", (key,))
        return {**limits, **used}

    def reserve(self, kind: str, tokens: int = 0, job_id: int | None = None,
                model_id: str | None = None) -> int:
        self.summary()
        conn = self.db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            key = week()
            limits = conn.execute("SELECT * FROM budgets WHERE week=?", (key,)).fetchone()
            used = conn.execute("SELECT COUNT(*),COALESCE(SUM(accounted_tokens),0) FROM attempts WHERE week=?", (key,)).fetchone()
            if used[0] >= limits["attempts_limit"] or used[1] + tokens > limits["tokens_limit"]:
                raise BudgetExhausted("Weekly accounted budget exhausted; work remains resumable")
            result = conn.execute("""INSERT INTO attempts
                (job_id,model_id,week,kind,started_at,reserved_tokens,accounted_tokens)
                VALUES(?,?,?,?,?,?,?)""", (job_id, model_id, key, kind, now(), tokens, tokens))
            if job_id:
                conn.execute("UPDATE jobs SET status='dispatching' WHERE id=?", (job_id,))
            conn.commit()
            return result.lastrowid
        except BaseException:
            conn.rollback()
            raise

    def finish(self, attempt: int, status: str, http_status: int | None = None,
               usage: dict | None = None, response_path: str | None = None) -> None:
        reported = usage_total(usage)
        row = self.db.one("SELECT reserved_tokens FROM attempts WHERE id=?", (attempt,))
        charged = reported if reported is not None else row["reserved_tokens"]
        self.db.execute("""UPDATE attempts SET finished_at=?,status=?,http_status=?,
            accounted_tokens=?,reported_tokens=?,usage=?,response_path=? WHERE id=?""",
                        (now(), status, http_status, charged, reported,
                         json.dumps(usage) if usage else None, response_path, attempt))

