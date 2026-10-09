from __future__ import annotations

import itertools
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict

from .budget import Budget
from .config import COUNTS, Settings, credential, digest, now
from .db import DB
from .opencode import REVISION, VERSION, interruption_status


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def reasoning_label(profile: dict) -> str:
    control = profile.get('reasoning', {})
    if control.get('mode') == 'highest-exposed':
        return f"Highest exposed: {control['variant']}"
    if control.get('mode') == 'provider-managed':
        return 'Provider-managed · no selectable level'
    return 'Unverified'


class PublicRow(Strict):
    model_id: str
    transport: Literal['local-opencode'] = 'local-opencode'
    name: str
    epoch: str
    tier: str
    season: str | None
    status: str
    availability: str
    free_eligible: bool = False
    protocol: str | None
    endpoint: str | None
    cap_verified: bool
    reasoning_setting: str = 'Unverified'
    reasoning_variant: str | None = None
    observed_at: str
    next_retry_at: str | None = None
    evaluated_at: str | None = None
    cycle_started_at: str | None = None
    progress: dict[str, int]
    pending_reasons: dict[str, int] = {}
    expected: dict[str, int]
    scores: dict[str, float] | None = None
    intervals: dict[str, list[float]] | None = None
    sample_counts: dict[str, int] | None = None
    latency_seconds: float | None = None
    accounted_tokens: int = 0
    reported_tokens: int = 0
    truncation_rate: float | None = None
    deadline_missed: bool = False


class Comparison(Strict):
    a: str
    b: str
    tier: str
    season: str
    view: str
    difference: float
    interval: list[float]
    unresolved: bool


class PublicPilot(Strict):
    model_id: str
    transport: Literal['local-opencode'] = 'local-opencode'
    season: str
    unranked: bool = True
    status: str
    health_graded: int
    cap_probe_verified: bool = False
    reasoning_setting: str = 'Unverified'
    reasoning_variant: str | None = None
    progress: dict[str, int]
    expected: dict[str, int]
    pending_reasons: dict[str, int]
    benchmark_scores: dict[str, float]
    evaluated_at: str | None


class PublicScreen(Strict):
    model_id: str
    epoch: str
    transport: Literal['local-opencode'] = 'local-opencode'
    client_version: str = VERSION
    protocol_revision: int = REVISION
    extra_system_context: bool = True
    season: str
    tier: str = 'public-screen'
    unranked: bool = True
    cap_verified: bool
    reasoning_setting: str = 'Unverified'
    reasoning_variant: str | None = None
    status: str
    progress: dict[str, int]
    expected: dict[str, int]
    pending_reasons: dict[str, int]
    benchmark_scores: dict[str, float]
    intervals: dict[str, list[float]]
    benchmark_evaluated_at: dict[str, str | None] = {}
    evaluated_at: str | None
    latency_seconds: float | None
    accounted_tokens: int
    reported_tokens: int
    truncation_rate: float | None


class PublicManifest(Strict):
    id: str
    seed: int
    sources: dict[str, str]
    livebench_release: str
    livecodebench_release: str
    language: str
    counts: dict[str, dict[str, int]]
    panels: dict[str, list[dict[str, str]]]
    validated: bool
    active: bool
    partial: bool = False
    grader_image: str | None
    protocol: dict = {}
    grading_files: dict[str, str] = {}


class Snapshot(Strict):
    schema_version: int = 2
    snapshot_id: str
    generated_at: str
    published_at: str | None = None
    discovery_at: str | None
    discovery_ok: bool
    discovery_error: str | None
    rows: list[PublicRow]
    history: list[PublicRow]
    comparisons: list[Comparison]
    pilots: list[PublicPilot] = []
    public_screens: list[PublicScreen] = []
    seasons: list[PublicManifest]
    budget: dict[str, int | str | None]
    queue_size: int
    missed_deadlines: int
    blockers: list[str]


def component_bootstrap(records: list[dict], samples: int = 10000) -> dict[str, np.ndarray]:
    groups = {}
    for row in sorted(records, key=lambda r: (r["benchmark"], r["stratum"], r["item_id"])):
        groups.setdefault((row["benchmark"], row["stratum"]), []).append(row)
    totals, sizes = {}, {}
    for (benchmark, stratum), group in sorted(groups.items()):
        scores = np.array([r["score"] for r in group], dtype=float)
        # Identical panel IDs generate identical draws across models, preserving pairing.
        seed = int(digest([benchmark, stratum, [r["item_id"] for r in group]])[:16], 16)
        rng = np.random.default_rng(seed)
        draws = scores[rng.integers(0, len(group), size=(samples, len(group)))].sum(axis=1)
        totals[benchmark] = totals.get(benchmark, np.zeros(samples)) + draws
        sizes[benchmark] = sizes.get(benchmark, 0) + len(group)
    return {k: totals[k] / sizes[k] * 100 for k in totals}


def bootstrap(records: list[dict], samples: int = 10000) -> dict[str, np.ndarray]:
    values = component_bootstrap(records, samples)
    reasoning = (values["gpqa"] + values["livebench"]) / 2
    coding = values["livecodebench"]
    return {"reasoning": reasoning, "coding": coding, "overall": reasoning * .4 + coding * .6}


def point_scores(records: list[dict]) -> dict[str, float]:
    values = {b: float(np.mean([r["score"] for r in records if r["benchmark"] == b])) * 100
              for b in ["gpqa", "livebench", "livecodebench"]}
    reasoning = (values["gpqa"] + values["livebench"]) / 2
    return {**values, "reasoning": reasoning, "coding": values["livecodebench"],
            "overall": reasoning * .4 + values["livecodebench"] * .6}


def records(db: DB, cycle: dict, tier: str) -> list[dict]:
    rows = db.rows("""SELECT j.*,i.benchmark,i.stratum FROM jobs j JOIN items i ON i.id=j.item_id
        JOIN panels p ON p.item_id=i.id AND p.season=? AND p.tier=? WHERE j.cycle_id=?""",
                   (cycle["season"], tier, cycle["id"]))
    for row in rows:
        prefix, separator, ident = row['item_id'].partition(':')
        if separator and prefix.startswith(('s1-', 'public-pilot-')):
            row['item_id'] = ident
    return rows


def is_complete(items: list[dict], tier: str) -> bool:
    return all(sum(r["benchmark"] == b and r["status"] == "graded" for r in items) == n
               for b, n in COUNTS[tier].items()) and len(items) == sum(COUNTS[tier].values())


def make_row(db: DB, settings: Settings, model: dict, cycle: dict | None, tier: str) -> tuple[PublicRow, dict | None]:
    items = records(db, cycle, tier) if cycle else []
    complete = is_complete(items, tier)
    scores, intervals, draws = None, None, None
    if complete:
        scores = point_scores(items)
        draws = bootstrap(items, settings.bootstrap_samples)
        intervals = {k: np.quantile(v, [.025, .975]).tolist() for k, v in draws.items()}
    progress = {b: sum(r["benchmark"] == b and r["status"] == "graded" for r in items) for b in COUNTS[tier]}
    attempts = db.one("""SELECT COALESCE(SUM(a.accounted_tokens),0) AS accounted,
        COALESCE(SUM(a.reported_tokens),0) AS reported FROM attempts a JOIN jobs j ON a.job_id=j.id
        WHERE j.cycle_id=? AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)""",
                     (cycle["id"], cycle["season"], tier)) if cycle else {"accounted": 0, "reported": 0}
    generated = [r for r in items if r["latency"] is not None]
    stamp = max((r["finished_at"] for r in db.rows("""SELECT a.finished_at FROM attempts a
        JOIN jobs j ON j.id=a.job_id WHERE j.cycle_id=? AND a.status IN ('received','recovered')
        AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)""", (cycle["id"], cycle["season"], tier))
                 if r["finished_at"]), default=None) if cycle else None
    stale = bool(cycle and (datetime.now(timezone.utc) - datetime.fromisoformat(cycle["started_at"])).days >= 28)
    missed = bool(cycle and not complete and datetime.fromisoformat(cycle["due_at"]) < datetime.now(timezone.utc))
    status = "stale" if complete and stale else "complete" if complete else "pending"
    profile = json.loads(cycle['profile'] if cycle else model["profile"])
    availability = model['status']
    if availability in {'eligible', 'provider_error'}:
        last = db.one("""SELECT a.status,a.response_path FROM attempts a LEFT JOIN jobs j ON j.id=a.job_id
            LEFT JOIN cycles c ON c.id=j.cycle_id WHERE a.model_id=?
            AND (c.epoch=? OR a.kind='opencode_access_diagnostic' AND a.started_at>=COALESCE(
                (SELECT MIN(c2.started_at) FROM cycles c2 WHERE c2.model_id=a.model_id AND c2.epoch=?),?))
            AND a.kind IN ('opencode_generation','opencode_access_diagnostic')
            ORDER BY a.id DESC LIMIT 1""", (model['id'], model['epoch'], model['epoch'], model['observed_at']))
        if last and last['status'] in {'authentication_failed', 'quota_limited', 'model_unavailable',
                                      'client_access_restricted', 'configuration_error'}:
            availability = last['status']
        elif last and last['status'] == 'ambiguous':
            availability = 'provider_error'
            try:
                evidence = json.loads(Path(last['response_path']).read_text())
                availability = interruption_status(evidence)
            except (OSError, TypeError, ValueError):
                pass
    if not cycle:
        status = "cap_unverified" if model["status"] == "eligible" and not profile.get("cap_verified") else "pending"
    retry = db.one("""SELECT MIN(j.next_after) AS at FROM jobs j JOIN cycles c ON c.id=j.cycle_id
        WHERE c.model_id=? AND c.epoch=? AND j.status='deferred'
        AND j.error='Verified quota rejection; awaiting retry window'""", (model['id'], model['epoch']))
    return PublicRow(model_id=model["id"], name=model["name"], epoch=cycle["epoch"] if cycle else model["epoch"],
                     tier=tier, season=cycle["season"] if cycle else None, status=status,
                     availability=availability, free_eligible=model['status'] in {'eligible','authentication_failed','quota_limited',
                         'model_unavailable','configuration_error','cap_violation','cap_unverified','client_access_restricted','provider_error'},
                     protocol=profile['protocol'], endpoint=profile.get('endpoint', model['endpoint']),
                     cap_verified=profile.get("cap_verified", False), observed_at=model["observed_at"],
                     next_retry_at=retry['at'] if availability == 'quota_limited' else None,
                     reasoning_setting=reasoning_label(profile), reasoning_variant=profile.get('reasoning', {}).get('variant'),
                     evaluated_at=stamp if complete else None,
                     cycle_started_at=cycle["started_at"] if cycle else None,
                     progress=progress, pending_reasons={s: sum(r['status'] == s for r in items) for s in
                         sorted({r['status'] for r in items if r['status'] != 'graded'})},
                     expected=COUNTS[tier], scores=scores, intervals=intervals,
                     sample_counts=progress if complete else None,
                     latency_seconds=float(np.median([r["latency"] for r in generated])) if generated else None,
                     accounted_tokens=attempts["accounted"], reported_tokens=attempts["reported"],
                     truncation_rate=sum(r["truncated"] for r in generated) / len(generated) if generated else None,
                     deadline_missed=missed or bool(complete and stale)), draws


def snapshot(db: DB, settings: Settings) -> Snapshot:
    rows, history, draws_by_model = [], [], {}
    active = db.one("SELECT id FROM seasons WHERE active=1")
    season_id = active["id"] if active else ""
    for model in db.rows("SELECT * FROM models ORDER BY id"):
        cycles = db.rows(f"""SELECT * FROM cycles WHERE model_id=? AND kind='screen'
            AND json_extract(profile,'$.transport')='local-opencode'
            AND json_extract(profile,'$.protocol_revision')={REVISION} ORDER BY started_at DESC""", (model["id"],))
        proofs = db.rows("SELECT evidence FROM observations WHERE model_id=?", (model["id"],))
        ever_free = any(json.loads(p["evidence"]).get("free") for p in proofs)
        if not ever_free and not cycles and model["status"] != "eligible":
            continue
        for tier in ["screen", "confirmation"]:
            matching = [c for c in cycles if c["epoch"] == model["epoch"] and c["season"] == season_id]
            if tier == "confirmation":
                matching = [c for c in matching if c["confirmation_requested"]]
            cycle = matching[0] if matching else None
            public, draws = make_row(db, settings, model, cycle, tier)
            rows.append(public)
            if draws and public.availability == 'eligible':
                draws_by_model[(tier, model["id"])] = (public, draws, {r["item_id"] for r in records(db, cycle, tier)})
            for old in cycles:
                if old["id"] == (cycle or {}).get("id"):
                    continue
                if tier == "confirmation" and not old["confirmation_requested"]:
                    continue
                prior, _ = make_row(db, settings, model, old, tier)
                if prior.scores:
                    history.append(prior)
    comparisons = []
    for (tier_a, id_a), (tier_b, id_b) in itertools.combinations(draws_by_model, 2):
        if tier_a != tier_b:
            continue
        a, values_a, ids_a = draws_by_model[(tier_a, id_a)]
        b, values_b, ids_b = draws_by_model[(tier_b, id_b)]
        if a.season != b.season or ids_a != ids_b:
            continue
        for view in ["reasoning", "coding", "overall"]:
            ci = np.quantile(values_a[view] - values_b[view], [.025, .975]).tolist()
            comparisons.append(Comparison(a=id_a, b=id_b, tier=tier_a, season=a.season, view=view,
                                          difference=a.scores[view] - b.scores[view], interval=ci,
                                          unresolved=ci[0] <= 0 <= ci[1]))
    manifests = []
    for season in db.rows("SELECT * FROM seasons ORDER BY created_at"):
        data = json.loads(season["manifest"])
        manifests.append(PublicManifest(id=season["id"], **{k: data[k] for k in ["seed", "sources", "livebench_release",
                            "livecodebench_release", "language", "counts", "panels"]},
                        validated=bool(season["validated"]), active=bool(season["active"]), grader_image=data.get("grader_image"),
                        partial=data.get('partial', False),
                        protocol=data.get('protocol', {}), grading_files=data.get('grading_files', {})))
    discovery = db.one("SELECT * FROM discovery ORDER BY id DESC LIMIT 1")
    blockers = []
    if not credential("hf"):
        blockers.append("Authenticated GPQA access is not configured")
    if not shutil.which("docker"):
        blockers.append("Docker is not installed")
    if not active:
        blockers.append("Benchmark season awaits preparation and grader validation")
    if discovery and not discovery["ok"]:
        blockers.append(discovery["error"])
    queue = db.one(f"""SELECT COUNT(*) AS n FROM jobs j JOIN cycles c ON c.id=j.cycle_id
        JOIN models m ON m.id=c.model_id AND m.epoch=c.epoch
        WHERE j.status!='graded' AND c.kind IN ('health','cap_calibration','pilot','screen','public_screen')
        AND json_extract(c.profile,'$.transport')='local-opencode'
        AND json_extract(c.profile,'$.protocol_revision')={REVISION}""")["n"]
    pilots = []
    for cycle in db.rows(f"""SELECT * FROM cycles WHERE kind='pilot'
        AND json_extract(profile,'$.transport')='local-opencode'
        AND json_extract(profile,'$.protocol_revision')={REVISION}
        AND epoch=(SELECT epoch FROM models WHERE id=cycles.model_id)
        ORDER BY started_at,model_id"""):
        items = records(db, cycle, 'pilot')
        panel = db.rows("SELECT i.benchmark FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=? AND p.tier='pilot'", (cycle['season'],))
        expected = {b: sum(r['benchmark'] == b for r in panel) for b in COUNTS['pilot']}
        progress = {b: sum(r['benchmark'] == b and r['status'] == 'graded' for r in items) for b in expected}
        # Later weekly probes cannot change a completed pilot's compatibility evidence.
        health_cycle = db.one("""SELECT id FROM cycles WHERE model_id=? AND epoch=? AND season=?
            AND kind=? AND started_at<=? ORDER BY started_at DESC LIMIT 1""",
            (cycle['model_id'], cycle['epoch'], cycle['season'], 'health',
             cycle['completed_at'] or now()))
        cap_checks = db.rows("""SELECT i.content,j.score,j.status FROM jobs j JOIN items i ON i.id=j.item_id
            WHERE j.cycle_id=?""", ((health_cycle or {}).get('id'),))
        health = sum(j['status'] == 'graded' for j in cap_checks)
        verified = any(json.loads(j['content']).get('check') == 'cap' and j['score'] == 1 for j in cap_checks)
        if not verified:
            verified = bool(db.one("""SELECT j.id FROM jobs j JOIN cycles c ON c.id=j.cycle_id
                WHERE c.model_id=? AND c.epoch=? AND c.season=? AND c.kind='cap_calibration'
                AND j.status='graded' AND j.score=1 AND c.completed_at<=?""",
                (cycle['model_id'],cycle['epoch'],cycle['season'],cycle['completed_at'] or now())))
        replacement_cap = (health == 5 and verified and any(j['status'] == 'ambiguous'
            and json.loads(j['content']).get('check') == 'cap' for j in cap_checks))
        complete = (sum(expected.values()) in {4, 6} and len(items) == sum(expected.values())
                    and (health == 6 or replacement_cap) and all(progress[b] == expected[b] for b in expected))
        pilots.append(PublicPilot(model_id=cycle['model_id'],
            reasoning_setting=reasoning_label(json.loads(cycle['profile'])),
            reasoning_variant=json.loads(cycle['profile']).get('reasoning', {}).get('variant'),
            season=cycle['season'], status='complete' if complete else 'pending',
            health_graded=health, cap_probe_verified=verified, progress=progress, expected=expected,
            pending_reasons={s: sum(r['status'] == s for r in items) for s in sorted({r['status'] for r in items if r['status'] != 'graded'})},
            benchmark_scores={b: float(np.mean([r['score'] for r in items if r['benchmark'] == b])) * 100
                              for b in expected if expected[b] and progress[b] == expected[b]},
            evaluated_at=cycle['completed_at'] if complete else None))
    public_screens = []
    for cycle in db.rows(f"""SELECT * FROM cycles WHERE kind='public_screen'
        AND json_extract(profile,'$.transport')='local-opencode'
        AND json_extract(profile,'$.protocol_revision')={REVISION}
        AND epoch=(SELECT epoch FROM models WHERE id=cycles.model_id) ORDER BY started_at,model_id"""):
        items = records(db, cycle, 'screen')
        panel = db.rows("SELECT i.benchmark FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=? AND p.tier='screen'", (cycle['season'],))
        expected = {b: sum(r['benchmark'] == b for r in panel) for b in COUNTS['screen']}
        progress = {b: sum(r['benchmark'] == b and r['status'] == 'graded' for r in items) for b in expected}
        complete = (expected == {'gpqa': 0, 'livebench': 20, 'livecodebench': 40}
                    and len(items) == 60 and progress == expected)
        scores, intervals = {}, {}
        completed_components = [b for b in ['livebench', 'livecodebench'] if progress[b] == expected[b] and expected[b]]
        component_items = [r for r in items if r['benchmark'] in completed_components]
        if component_items:
            scores = {b: float(np.mean([r['score'] for r in component_items if r['benchmark'] == b])) * 100
                      for b in completed_components}
            intervals = {b: np.quantile(v, [.025, .975]).tolist()
                         for b, v in component_bootstrap(component_items, settings.bootstrap_samples).items()}
        usage = db.one('SELECT COALESCE(SUM(accounted_tokens),0) accounted,COALESCE(SUM(reported_tokens),0) reported FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE cycle_id=?)', (cycle['id'],))
        component_dates = {b: db.one("""SELECT MAX(a.finished_at) stamp FROM attempts a JOIN jobs j ON j.id=a.job_id
            JOIN items i ON i.id=j.item_id WHERE j.cycle_id=? AND i.benchmark=? AND j.status='graded'
            AND a.status IN ('received','recovered')""", (cycle['id'], b))['stamp'] for b in completed_components}
        generated_items = [r for r in items if r['latency'] is not None]
        profile = json.loads(cycle['profile'])
        public_screens.append(PublicScreen(model_id=cycle['model_id'], epoch=cycle['epoch'], season=cycle['season'],
            reasoning_setting=reasoning_label(profile), reasoning_variant=profile.get('reasoning', {}).get('variant'),
            cap_verified=profile.get('cap_verified', False) and not any(
                r['status'] == 'cap_unverified' or 'output cap' in (r.get('error') or '').lower() for r in items),
            status='complete' if complete else 'pending', progress=progress, expected=expected,
            pending_reasons={s: sum(('grading_blocked' if r['status'] == 'generated' and r.get('error') else r['status']) == s for r in items)
                             for s in sorted({'grading_blocked' if r['status'] == 'generated' and r.get('error') else r['status']
                                              for r in items if r['status'] != 'graded'})},
            benchmark_scores=scores, intervals=intervals, benchmark_evaluated_at=component_dates,
            evaluated_at=cycle['completed_at'] if complete else None,
            latency_seconds=float(np.median([r['latency'] for r in generated_items])) if generated_items else None,
            accounted_tokens=usage['accounted'], reported_tokens=usage['reported'],
            truncation_rate=sum(r['truncated'] for r in generated_items) / len(generated_items) if generated_items else None))
    generated = now()
    return Snapshot(snapshot_id=digest([generated, [r.model_dump() for r in rows],
                                       [r.model_dump() for r in public_screens], [r.model_dump() for r in pilots]])[:20],
                    generated_at=generated, discovery_at=(discovery or {}).get("observed_at"),
                    discovery_ok=bool((discovery or {}).get("ok")), discovery_error=(discovery or {}).get("error"),
                    rows=rows, history=history, comparisons=comparisons, pilots=pilots, public_screens=public_screens, seasons=manifests,
                    budget=Budget(db, settings).summary(), queue_size=queue,
                    missed_deadlines=sum(r.deadline_missed for r in rows if r.tier == 'screen'), blockers=blockers)
