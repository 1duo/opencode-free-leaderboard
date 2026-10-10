from __future__ import annotations

import json
import math

from .config import Settings, now, week
from .db import DB


class BudgetExhausted(RuntimeError):
    pass


def estimate(messages: list[dict], cap: int) -> int:
    # Byte-based bound deliberately exceeds common tokenizer estimates. Not billed dollars.
    return len(json.dumps(messages, ensure_ascii=False).encode()) + 8192 + cap


def usage_total(usage: dict | None) -> int | None:
    if not usage:
        return None
    def valid(value):
        return type(value) is int and value >= 0
    total = usage.get('total_tokens')
    components = None
    for a, b in [("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")]:
        if a in usage or b in usage:
            if not valid(usage.get(a)) or not valid(usage.get(b)):
                return None
            # Cached tokens and reasoning are subsets of these totals, not additional tokens.
            components = usage[a] + usage[b]
            break
    if total is not None:
        return total if valid(total) and (components is None or total >= components) else None
    return components


class Budget:
    def __init__(self, db: DB, settings: Settings):
        self.db, self.settings = db, settings

    def plan(self, generation_tokens: list[int], probes: int, metadata: int = 2) -> dict:
        key = week()
        planned_attempts = math.ceil((len(generation_tokens) + probes + metadata) * 1.25)
        planned_tokens = math.ceil((sum(generation_tokens) + probes * 10000) * 1.25)
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
            COALESCE(SUM(reported_tokens IS NULL AND reserved_tokens>0),0) AS estimated_attempts,
            COALESCE(SUM(kind='opencode_generation'),0) AS opencode_attempts,
            COUNT(DISTINCT CASE WHEN kind='opencode_generation' THEN job_id END) AS opencode_invocations,
            COALESCE(SUM(CASE WHEN kind='opencode_generation' THEN accounted_tokens ELSE 0 END),0) AS opencode_accounted_tokens,
            COALESCE(SUM(CASE WHEN kind='opencode_generation' THEN reported_tokens ELSE 0 END),0) AS opencode_reported_tokens,
            COALESCE(SUM(kind='opencode_generation' AND reported_tokens IS NULL AND reserved_tokens>0),0) AS opencode_estimated_attempts,
            COALESCE(SUM(kind='discovery'),0) AS discovery_attempts,
            COALESCE(SUM(kind NOT IN ('opencode_generation','discovery')),0) AS prior_attempts,
            COALESCE(SUM(CASE WHEN kind NOT IN ('opencode_generation','discovery') THEN accounted_tokens ELSE 0 END),0) AS prior_accounted_tokens
            FROM attempts WHERE week=?""", (key,))
        return {**limits, **used}

    def reserve(self, kind: str, tokens: int = 0, job_id: int | None = None,
                model_id: str | None = None, manual_retry: bool = False) -> int:
        self.summary()
        conn = self.db.conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            key = week()
            limits = conn.execute("SELECT * FROM budgets WHERE week=?", (key,)).fetchone()
            used = conn.execute("SELECT COUNT(*),COALESCE(SUM(accounted_tokens),0) FROM attempts WHERE week=?", (key,)).fetchone()
            if used[0] >= limits["attempts_limit"] or used[1] + tokens > limits["tokens_limit"]:
                raise BudgetExhausted("Weekly accounted budget exhausted; work remains resumable")
            if manual_retry:
                job = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
                previous = conn.execute("SELECT * FROM attempts WHERE job_id=? AND kind='opencode_generation' ORDER BY id", (job_id,)).fetchall()
                if (kind != 'opencode_generation' or not job or job['status'] != 'ambiguous'
                        or job['response_path'] or not previous or previous[-1]['status'] != 'ambiguous'
                        or any(a['status'] in {'received', 'recovered'} for a in previous)
                        or conn.execute('SELECT 1 FROM manual_retries WHERE job_id=?', (job_id,)).fetchone()):
                    raise ValueError('Manual retry requires an unanswered unknown request and an unused allowance')
            result = conn.execute("""INSERT INTO attempts
                (job_id,model_id,week,kind,started_at,reserved_tokens,accounted_tokens)
                VALUES(?,?,?,?,?,?,?)""", (job_id, model_id, key, kind, now(), tokens, tokens))
            if manual_retry:
                # Reservation and consumption are one commit, before dispatch.
                # Even a crash immediately afterward cannot grant a second retry.
                original = {'job': dict(job), 'attempt_ids': [a['id'] for a in previous]}
                conn.execute('INSERT INTO manual_retries VALUES(?,?,?,?,?)',
                             (job_id, now(), previous[-1]['id'], result.lastrowid, json.dumps(original)))
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

    def record_unrequested_turn(self, parent: int, turn: int, evidence: str) -> int:
        """Charge a possible SDK continuation after abort; never authorize another dispatch."""
        existing = self.db.one("SELECT id FROM attempts WHERE json_extract(usage,'$.unrequested_from')=? AND json_extract(usage,'$.turn')=?", (parent, turn))
        if existing:
            return existing['id']
        row = self.db.one('SELECT * FROM attempts WHERE id=?', (parent,))
        stamp = now()
        return self.db.execute("""INSERT INTO attempts(job_id,model_id,week,kind,started_at,finished_at,status,
            reserved_tokens,accounted_tokens,usage,response_path) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (row['job_id'],row['model_id'],row['week'],'opencode_generation',stamp,stamp,'ambiguous',
             row['reserved_tokens'],row['reserved_tokens'],json.dumps({'source':'opencode-unrequested-turn',
             'unrequested_from':parent,'turn':turn}),evidence)).lastrowid
