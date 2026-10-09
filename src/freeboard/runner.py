from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from .adapters import parse
from .budget import Budget, BudgetExhausted, estimate
from .config import Settings, credential, digest, now, week
from .db import DB
from .discovery import discover
from .grading import DockerGrader, GradingUnavailable, gpqa_score
from .opencode import OpenCode, REVISION, completion_events, failure_status, prompt_body

PROBES = [
    {"stratum": "format", "prompt": "Return a JSON array of all integers from 1 to 10000, without omitting any. No prose.", "check": "cap"},
    {"stratum": "format", "prompt": 'Reply with exactly this JSON object: {"ready":true}', "check": "json"},
    {"stratum": "reasoning", "prompt": "All glips are blue. Ada is a glip. Is Ada blue? Reply only YES or NO.", "answer": "YES"},
    {"stratum": "reasoning", "prompt": "A box has 3 red and 2 blue balls. Two red balls are removed. How many balls remain? Reply only the integer.", "answer": "3"},
    {"stratum": "code", "prompt": "Write Python that reads an integer from stdin and prints twice it. Return a fenced python code block.", "tests": [["2\n", "4\n"], ["-3\n", "-6\n"]]},
    {"stratum": "code", "prompt": "Write Python that reads a line from stdin and prints its reversal. Return a fenced python code block.", "tests": [["abc\n", "cba\n"], ["a\n", "a\n"]]},
]


class Runner:
    def __init__(self, db: DB, settings: Settings, checkout: Path, client=None):
        self.db, self.settings, self.checkout = db, settings, checkout
        self.budget = Budget(db, settings)
        self.grader = DockerGrader(settings)
        self.client = client or httpx.Client(timeout=settings.request_timeout, follow_redirects=False)
        self.opencode = OpenCode(settings)

    def active_season(self) -> dict:
        season = self.db.one("SELECT * FROM seasons WHERE active=1 AND validated=1")
        if not season:
            raise RuntimeError("No validated active season; prepare-panels requires datasets and Docker")
        return season

    def models(self) -> list[dict]:
        return self.db.rows("SELECT * FROM models WHERE status='eligible' ORDER BY id")

    def cycle(self, model: dict, season: dict, kind: str, ident: str) -> dict:
        started = now()
        profile = {**json.loads(model['profile']), 'endpoint': model['endpoint']}
        self.db.execute("""INSERT OR IGNORE INTO cycles
            (id,model_id,epoch,season,kind,started_at,due_at,profile) VALUES(?,?,?,?,?,?,?,?)""",
                        (ident, model["id"], model["epoch"], season["id"], kind, started,
                         (datetime.fromisoformat(started) + timedelta(days=28)).isoformat(), json.dumps(profile)))
        return self.db.one("SELECT * FROM cycles WHERE id=?", (ident,))

    def add_panel(self, cycle: dict, tier: str) -> None:
        for item in self.db.rows("SELECT item_id FROM panels WHERE season=? AND tier=?", (cycle["season"], tier)):
            self.db.execute("INSERT OR IGNORE INTO jobs(cycle_id,item_id) VALUES(?,?)", (cycle["id"], item["item_id"]))

    def health(self, model: dict, season: dict) -> dict:
        ident = "health:" + digest([model["id"], model["epoch"], season["id"], week()])[:24]
        cycle = self.cycle(model, season, "health", ident)
        for i, probe in enumerate(PROBES):
            item = {"benchmark": "health", **probe,
                    "messages": [{"role": "user", "content": probe["prompt"]}]}
            item_id = "health:" + digest(probe)
            self.db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)",
                            (item_id, None, "health", probe["stratum"], digest(item), json.dumps(item)))
            self.db.execute("INSERT OR IGNORE INTO jobs(cycle_id,item_id) VALUES(?,?)", (ident, item_id))
        return cycle

    def schedule(self, season: dict) -> None:
        dt = datetime.now(timezone.utc)
        cohort = (dt.date().toordinal() // 7) % 4
        for model in self.models():
            self.health(model, season)
            if not json.loads(model["profile"]).get("cap_verified"):
                continue
            latest = self.db.one("""SELECT * FROM cycles WHERE model_id=? AND epoch=? AND season=?
                AND kind='screen' ORDER BY started_at DESC LIMIT 1""", (model["id"], model["epoch"], season["id"]))
            if latest and not latest["completed_at"]:
                continue
            due = (not latest or (dt - datetime.fromisoformat(latest["started_at"])).days >= 28)
            # Cohort checks may refresh slightly early; overdue work always takes priority.
            if latest and model["cohort"] == cohort:
                due = due or (dt - datetime.fromisoformat(latest["started_at"])).days >= 21
            if due:
                ident = "screen:" + digest([model["id"], model["epoch"], season["id"], week()])[:24]
                cycle = self.cycle(model, season, "screen", ident)
                if latest:
                    deadline = (datetime.fromisoformat(latest["started_at"]) + timedelta(days=28)).isoformat()
                    self.db.execute("UPDATE cycles SET due_at=? WHERE id=?", (deadline, ident))
                self.add_panel(cycle, "screen")

    def capacity(self) -> dict:
        # Headline obligations alone set the scalable budget; confirmations consume slack.
        jobs = self.db.rows("""SELECT i.content,i.benchmark FROM jobs j JOIN items i ON j.item_id=i.id
            JOIN cycles c ON c.id=j.cycle_id JOIN models m ON m.id=c.model_id
            WHERE c.kind='screen' AND c.epoch=m.epoch AND m.status='eligible'
            AND j.status NOT IN ('graded','ambiguous','failed') AND EXISTS
            (SELECT 1 FROM panels p WHERE p.season=c.season AND p.tier='screen' AND p.item_id=i.id)""")
        health_jobs = self.db.one("""SELECT COUNT(*) AS n FROM jobs j JOIN cycles c ON c.id=j.cycle_id
            JOIN models m ON m.id=c.model_id WHERE c.kind='health' AND c.epoch=m.epoch
            AND m.status='eligible' AND c.started_at>=? AND j.status!='graded'""", (week(),))["n"]
        amounts = [estimate(json.loads(j["content"])["messages"], self.settings.max_output_tokens) for j in jobs]
        # Already accounted attempts are included when a daily resume recalculates capacity.
        used = self.budget.summary()
        planned = self.budget.plan(amounts, health_jobs, 2)
        self.db.execute("UPDATE budgets SET attempts_limit=MAX(attempts_limit,?),tokens_limit=MAX(tokens_limit,?) WHERE week=?",
                        (used["attempts_used"] + planned["planned_attempts"],
                         used["accounted_tokens"] + planned["planned_tokens"], week()))
        return self.budget.summary()

    def generate(self, job: dict, model: dict, item: dict, cap: int) -> None:
        if job.get('response_path'):
            raise RuntimeError('A durable answer exists; this job must not be regenerated')
        prompt_body(model, item['messages'])
        previous = self.db.rows("SELECT * FROM attempts WHERE job_id=? AND kind='opencode_generation'", (job['id'],))
        if len(previous) >= 3:
            self.db.execute("UPDATE jobs SET status='failed',error='Retry allowance exhausted' WHERE id=?", (job['id'],))
            return
        amount = estimate(item['messages'], cap)
        attempts = [self.budget.reserve('opencode_generation', amount, job['id'], model['id'])]
        started = time.monotonic()
        def on_retry(status):
            # OpenCode emits this before its backoff and next provider dispatch.
            self.budget.finish(attempts[-1], 'retryable')
            if len(previous) + len(attempts) >= 3 or status['next'] / 1000 - time.time() > 60:
                raise RuntimeError('OpenCode retry allowance exhausted; no automatic replay')
            attempts.append(self.budget.reserve('opencode_generation', amount, job['id'], model['id']))
        try:
            result = self.opencode.generate(model, item['messages'], cap, on_retry)
        except (RuntimeError, httpx.HTTPError, ValueError, KeyError):
            self.budget.finish(attempts[-1], 'ambiguous')
            self.db.execute("UPDATE models SET status='provider_error' WHERE id=?", (model['id'],))
            self.db.execute("UPDATE jobs SET status='ambiguous',error='OpenCode outcome unknown; no automatic replay' WHERE id=?", (job['id'],))
            self.opencode.close()
            return
        attempt = attempts[-1]
        path = self.settings.state / 'responses' / f'{attempt}.json'
        record = {'protocol': 'opencode', 'body': '{}', 'cap': cap, 'latency': time.monotonic() - started,
                  'client_result': result, 'protocol_revision': REVISION}
        try:
            record['body'] = json.dumps({'events': completion_events(result, model['id'])})
            completion = parse('opencode', json.loads(record['body']))
        except (ValueError, KeyError, TypeError):
            completion = None
        temp = path.with_suffix('.partial')
        temp.write_text(json.dumps(record))
        with temp.open('rb') as handle:
            os.fsync(handle.fileno())
        temp.replace(path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        if completion is None:
            status, message = failure_status(result)
            error = result.get('info', {}).get('error') or result.get('error') or {}
            code = (error.get('data') or {}).get('statusCode')
            self.budget.finish(attempt, status, code if type(code) is int else None, response_path=str(path))
            self.db.execute('UPDATE models SET status=? WHERE id=?', (status, model['id']))
            known_rejection = bool(result.get('info', {}).get('error') or result.get('error'))
            self.db.execute("UPDATE jobs SET status=?,response_path=?,next_after=?,error=? WHERE id=?",
                            ('deferred' if known_rejection else 'ambiguous', None if known_rejection else str(path),
                             (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), message, job['id']))
            return
        self.budget.finish(attempt, 'received', usage=completion.usage, response_path=str(path))
        self.db.execute("UPDATE jobs SET status='generated',response_path=?,latency=?,truncated=? WHERE id=?",
                        (str(path), record['latency'], int(completion.truncated), job['id']))
        if not completion.bounded(cap):
            self.db.invalidate_cap(job['id'], 'Combined output cap not verified; no replay',
                                   'cap_unverified' if completion.output_tokens is None else 'failed')

    def grade(self, job: dict, item: dict, image: str) -> None:
        record = json.loads(Path(job["response_path"]).read_text())
        completion = parse(record["protocol"], json.loads(record["body"]))
        if item["benchmark"] == "gpqa":
            score = gpqa_score(item["answer"], completion.text)
        elif item["benchmark"] == "health":
            if item.get("check") == "cap":
                score = float(completion.truncated and completion.output_tokens is not None and completion.output_tokens <= record["cap"])
            elif item.get("check") == "json":
                try:
                    score = float(json.loads(completion.text) == {"ready": True})
                except ValueError:
                    score = 0
            elif item["stratum"] == "code":
                score = self.grader.grade({"benchmark": "synthetic_code", "tests": item["tests"]}, completion.text, image)
            else:
                score = float(completion.text.strip().upper() == item["answer"].upper())
        else:
            score = self.grader.grade(item, completion.text, image)
        self.db.execute("UPDATE jobs SET status='graded',score=?,error=NULL WHERE id=?", (score, job["id"]))

    def finish_cycle(self, cycle_id: str) -> None:
        cycle = self.db.one("SELECT * FROM cycles WHERE id=?", (cycle_id,))
        if cycle["kind"] == "screen":
            tier, expected = "screen", 80
        elif cycle["kind"] in {"pilot", "public_screen"}:
            tier = 'pilot' if cycle['kind'] == 'pilot' else 'screen'
            expected = self.db.one('SELECT COUNT(*) AS n FROM panels WHERE season=? AND tier=?', (cycle['season'], tier))['n']
        else:
            tier, expected = None, 6
        clause = "" if tier is None else " AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)"
        args = (cycle_id,) if tier is None else (cycle_id, cycle["season"], tier)
        counts = self.db.one("SELECT COUNT(*) AS n,SUM(j.status='graded') AS done FROM jobs j WHERE cycle_id=?" + clause, args)
        if counts["n"] == expected and counts["done"] == expected:
            self.db.execute("UPDATE cycles SET completed_at=COALESCE(completed_at,?) WHERE id=?", (now(), cycle_id))
        if cycle["kind"] == "health" and counts["done"] == 6:
            jobs = self.db.rows("SELECT * FROM jobs WHERE cycle_id=?", (cycle_id,))
            records = [json.loads(Path(j["response_path"]).read_text()) for j in jobs]
            completions = [parse(r["protocol"], json.loads(r["body"])) for r in records]
            bounded = all(c.bounded(r['cap']) for c, r in zip(completions, records))
            bounded = bounded and any(c.truncated and json.loads(self.db.one('SELECT content FROM items WHERE id=?', (j['item_id'],))['content']).get('check') == 'cap'
                                      for c, j in zip(completions, jobs))
            model = self.db.one("SELECT * FROM models WHERE id=?", (cycle["model_id"],))
            if model and model["epoch"] == cycle["epoch"] and bounded:
                profile = json.loads(model["profile"])
                profile["cap_verified"] = True
                self.db.execute("UPDATE models SET profile=? WHERE id=?", (json.dumps(profile), model["id"]))

    def drain(self, season: dict, kind: str | None = None, limit: int | None = None, model_ids: set[str] | None = None) -> dict:
        manifest = json.loads(season["manifest"])
        image = manifest["grader_image"]
        processed = 0
        while limit is None or processed < limit:
            jobs = self.db.rows("""SELECT j.*,c.model_id,c.epoch,c.kind,c.profile AS cycle_profile,i.content
                FROM jobs j JOIN cycles c ON c.id=j.cycle_id JOIN items i ON i.id=j.item_id
                JOIN models m ON m.id=c.model_id WHERE c.season=?
                AND c.kind IN ('health','pilot','screen','public_screen')
                AND json_extract(c.profile,'$.transport')='local-opencode'
                AND json_extract(c.profile,'$.protocol_revision')=3
                AND (j.status='generated' OR (c.epoch=m.epoch AND m.status='eligible'
                    AND j.status IN ('pending','deferred') AND (c.kind!='health' OR c.started_at>=?)))
                AND (j.next_after IS NULL OR j.next_after<=?)
                ORDER BY CASE WHEN c.kind='health' AND json_extract(m.profile,'$.cap_verified')!=1 THEN 0
                    WHEN c.kind='screen' AND EXISTS(SELECT 1 FROM panels p WHERE p.season=c.season AND p.tier='screen' AND p.item_id=j.item_id) THEN 1
                    WHEN c.kind='health' THEN 2 ELSE 3 END,
                c.due_at,j.id""", (season["id"], week(), now()))
            if kind:
                jobs = [j for j in jobs if j["kind"] == kind or (kind == "pilot" and j["kind"] == "health")]
            if model_ids is not None:
                jobs = [j for j in jobs if j['model_id'] in model_ids]
            if not jobs:
                break
            job = jobs[0]
            job_kind = job['kind']
            item = json.loads(job["content"])
            model = self.db.one("SELECT * FROM models WHERE id=?", (job["model_id"],))
            if job["status"] != 'generated' and job["kind"] != "health" and not json.loads(model["profile"]).get("cap_verified"):
                self.db.execute("UPDATE jobs SET status='deferred',next_after=?,error='Output cap not verified' WHERE id=?",
                                ((datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), job["id"]))
                continue
            try:
                if job["status"] != "generated":
                    self.generate(job, model, item, 256 if job["kind"] == "health" else 4096)
                    job = self.db.one("SELECT * FROM jobs WHERE id=?", (job["id"],))
                if job["status"] == "generated":
                    self.grade(job, item, image)
                self.finish_cycle(job["cycle_id"])
                if kind != 'pilot' and job_kind == 'health':
                    self.schedule(season)
                    self.capacity()
            except BudgetExhausted:
                break
            except GradingUnavailable as exc:
                self.db.execute("UPDATE jobs SET error=?,next_after=? WHERE id=?",
                                (str(exc), (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(), job["id"]))
                # One saved answer with a grader failure must not prevent the
                # remaining questions from finishing. Never regenerate it.
                processed += 1
                continue
            processed += 1
        return {"processed": processed, "budget": self.budget.summary()}

    def run(self, pilot=False, limit=None, pilot_models=None) -> dict:
        self.db.recover()
        result = discover(self.db, self.budget, self.client)
        if not result["ok"]:
            return {"blocked": result["error"], "discovery": result}
        if not credential("zen"):
            return {"blocked": "OpenCode Zen credential missing", "discovery": result}
        season = self.active_season()
        if not pilot and json.loads(season['manifest']).get('partial'):
            return {'blocked': 'Public compatibility pilots cannot enter headline evaluations; authenticated GPQA is required'}
        if pilot:
            available = {m['id']: m for m in self.models()}
            selected = sorted(pilot_models) if pilot_models else sorted(available)[:3]
            if any(m not in available for m in selected):
                raise ValueError('Pilot model lacks current free eligibility')
            for model in [available[m] for m in selected]:
                self.health(model, season)
                ident = "pilot:" + digest([model["id"], model["epoch"], season["id"]])[:24]
                self.add_panel(self.cycle(model, season, "pilot", ident), "pilot")
            self.budget.plan([20000] * 18, 18)
            return self.drain(season, "pilot", limit, set(selected))
        # Headline execution requires successful pilot for the three selected endpoints.
        targets = self.models()[:3]
        if any(not json.loads(m['profile']).get('cap_verified') or not self.db.one("SELECT id FROM cycles WHERE kind='pilot' AND model_id=? AND epoch=? AND season=? AND completed_at IS NOT NULL",
                               (m["id"], m["epoch"], season["id"])) for m in targets):
            return {"blocked": "Complete the three-model compatibility pilot before headline screens"}
        self.schedule(season)
        self.capacity()
        result = self.drain(season, limit=limit)
        self.schedule(season)  # Verified new endpoints can now join the screen queue.
        self.capacity()
        return result

    def confirm(self, model_id: str) -> dict:
        season = self.active_season()
        model = self.db.one("SELECT * FROM models WHERE id=? AND status='eligible'", (model_id,))
        if not model:
            raise ValueError("Model is not currently eligible")
        cycle = self.db.one("""SELECT * FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind='screen'
            AND completed_at IS NOT NULL ORDER BY started_at DESC LIMIT 1""", (model_id, model["epoch"], season["id"]))
        if not cycle or (datetime.now(timezone.utc) - datetime.fromisoformat(cycle["started_at"])).days >= 28:
            raise ValueError("Confirmation requires a completed current screen from this evaluation cycle")
        self.db.execute("UPDATE cycles SET confirmation_requested=1 WHERE id=?", (cycle["id"],))
        self.add_panel(cycle, "confirmation")
        return {"cycle": cycle["id"], "queued": "160 additional completions; headline jobs take priority"}
