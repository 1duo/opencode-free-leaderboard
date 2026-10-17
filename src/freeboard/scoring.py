from __future__ import annotations

import itertools
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from .budget import Budget
from .coding_protocol import SPECS, coding_spec
from .config import COUNTS, OMITTED_MODELS, Settings, credential, digest, now
from .db import DB
from .opencode import REVISION, VERSION, interruption_status
from .privacy import gpqa_allowed, public_only_reason
from .submissions import CommunityResult


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
    public_only_reason: str | None = None
    transport: Literal['local-opencode'] = 'local-opencode'
    name: str
    epoch: str
    client_version: str | None = None
    tier: str
    season: str | None
    status: str
    availability: str
    free_eligible: bool = False
    protocol: str | None
    endpoint: str | None
    cap_verified: bool
    cap_failure: dict[str, int] | None = None
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
    benchmark_scores: dict[str, float] = {}
    benchmark_intervals: dict[str, list[float]] = {}
    benchmark_evaluated_at: dict[str, str | None] = {}
    sample_counts: dict[str, int] | None = None
    latency_seconds: float | None = None
    accounted_tokens: int = 0
    reported_tokens: int = 0
    truncation_rate: float | None = None
    deadline_missed: bool = False
    manual_retry_count: int = 0
    manual_recovered_questions: int = 0


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
    epoch: str
    public_only_reason: str | None = None
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
    manual_retry_count: int = 0
    manual_recovered_questions: int = 0
    unanswered_retries_used: int = 0


class PublicScreen(Strict):
    model_id: str
    public_only_reason: str | None = None
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
    cycle_started_at: str | None = None
    latency_seconds: float | None
    accounted_tokens: int
    reported_tokens: int
    truncation_rate: float | None
    manual_retry_count: int = 0
    manual_recovered_questions: int = 0


class Lightweight(PublicScreen):
    tier: str = 'lightweight'
    availability: str
    health_graded: int
    health_passed: int
    tool_rejections: int = 0
    interval_warnings: dict[str, str] = {}


class PublicProtocol(Strict):
    output_cap: int | None = None
    temperature: float | None = None
    tools: bool | None = None
    completions: int | None = None
    panel: Literal['lightweight-v1', 'bigcodebench-cpu-v1', 'systems-c-v1'] | None = None
    source_season: str | None = None
    strata: dict[Literal['livebench', 'livecodebench', 'bigcodebench', 'systems_c'], dict[Literal['spatial', 'zebra_puzzle', 'easy', 'medium', 'hard', 'stdlib', 'scientific', 'memory', 'encoding', 'parsing', 'numeric'], int]] = {}
    purpose: Literal['Public subset; no headline or overall ranking', 'CPU library-use subset; equal-weight coding expansion', 'CPU systems programming subset; equal-weight coding expansion'] | None = None
    compiler: Literal['gcc-12.2.0'] | None = None
    sanitizers: list[Literal['address', 'undefined']] = []
    toolchain_packages_hash: str | None = Field(default=None, pattern=r'^[0-9a-f]{64}$')


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
    protocol: PublicProtocol = Field(default_factory=PublicProtocol)
    grading_files: dict[str, str] = {}
    coding_promoted_at: str | None = None


class CapabilityResult(Strict):
    model_id: str
    epoch: str
    category: Literal['reasoning', 'coding']
    source_tier: str | None = None
    season: str | None = None
    score: float | None = None
    interval: list[float] | None = None
    interval_note: str | None = None
    sample_counts: dict[str, int] = {}
    benchmark_scores: dict[str, float] = {}
    evaluated_at: str | None = None
    rank: int | None = None
    stale: bool = False
    refresh_pending: bool = False
    benchmark_weights: dict[str, float] = {}
    benchmark_evaluated_at: dict[str, str | None] = {}
    source_seasons: dict[str, str] = {}


class CodingEvaluation(Strict):
    model_id: str
    epoch: str
    season: str
    benchmark: Literal['bigcodebench', 'systems_c'] = 'bigcodebench'
    status: Literal['pending', 'complete']
    phase: Literal['pilot', 'screen'] = 'screen'
    pilot_progress: int = 0
    pilot_expected: int = 2
    progress: int
    expected: Literal[24] = 24
    score: float | None = None
    interval: list[float] | None = None
    evaluated_at: str | None = None
    started_at: str
    pending_reasons: dict[str, int] = {}
    accounted_tokens: int = 0
    reported_tokens: int = 0
    truncation_rate: float | None = None


class Snapshot(Strict):
    schema_version: int = 2
    snapshot_id: str
    repository: str = Field(default='1duo/opencode-free-leaderboard', pattern=r'^[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9][A-Za-z0-9_.-]*$')
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
    lightweight: list[Lightweight] = []
    capabilities: list[CapabilityResult] = []
    community_results: list[CommunityResult] = []
    coding_evaluations: list[CodingEvaluation] = []
    coding_weights: dict[str, float] = {'livecodebench': 1.0}
    coding_expansion_status: Literal['not-started', 'pending', 'access-restricted', 'active'] = 'not-started'
    seasons: list[PublicManifest]
    budget: dict[str, int | str | None]
    queue_size: int
    missed_deadlines: int
    blockers: list[str]


def merged_capabilities(rows: list[PublicRow], public_screens: list[PublicScreen],
                        lightweight: list[Lightweight], history: list[PublicRow] | None = None) -> list[CapabilityResult]:
    """One result per current model/category; larger panels replace nested quick checks."""
    result = []
    priority = {'screen': 3, 'public-screen': 2, 'lightweight': 1}
    for model in rows:
        if model.tier != 'screen' or model.availability in {'removed', 'excluded', 'paid', 'unsupported'}:
            continue
        sources = [model] if model.scores and model.cap_verified else []
        # A new unfinished cycle does not erase a completed matching screen.
        sources += [r for r in history or [] if r.model_id == model.model_id and r.epoch == model.epoch
                    and r.season == model.season and r.tier == 'screen'
                    and r.scores and r.cap_verified and model.cap_verified]
        sources += [r for r in [*public_screens, *lightweight]
                    if r.model_id == model.model_id and r.epoch == model.epoch
                    and r.status == 'complete' and r.cap_verified and model.cap_verified]
        if model.availability in {'authentication_failed', 'client_access_restricted', 'model_unavailable'} and not sources:
            continue
        for category in ['reasoning', 'coding']:
            choices = []
            for source in sources:
                full = source.tier == 'screen'
                benchmarks = (['gpqa', 'livebench'] if full else ['livebench']) if category == 'reasoning' else ['livecodebench']
                scores = source.scores if full else source.benchmark_scores
                if not all(b in scores and source.progress.get(b, 0) > 0 for b in benchmarks):
                    continue
                counts = {b: source.progress[b] for b in benchmarks}
                metric = category if full else benchmarks[0]
                interval = (source.intervals or {}).get(metric)
                dates = [source.benchmark_evaluated_at.get(b) for b in benchmarks]
                candidate = CapabilityResult(model_id=model.model_id, epoch=model.epoch, category=category,
                    source_tier=source.tier, season=source.season,
                    score=sum(scores[b] for b in benchmarks) / len(benchmarks), interval=interval,
                    interval_note=getattr(source, 'interval_warnings', {}).get(metric),
                    sample_counts=counts, benchmark_scores={b: scores[b] for b in benchmarks},
                    evaluated_at=max((d for d in dates if d), default=source.evaluated_at),
                    stale=bool((stamp := getattr(source, 'cycle_started_at', None) or source.evaluated_at)
                        and (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).days >= 28),
                    refresh_pending=(source.tier == 'screen' and not model.scores) or (
                        source.tier == 'public-screen' and any(r.model_id == source.model_id
                            and r.epoch == source.epoch and r.season == source.season and r.status == 'pending'
                            and (r.cycle_started_at or '') > (source.cycle_started_at or '') for r in public_screens)))
                choices.append((sum(counts.values()), priority[source.tier], candidate.evaluated_at or '', source.season or '', candidate))
            result.append(max(choices, key=lambda c: c[:4])[4] if choices else CapabilityResult(
                model_id=model.model_id, epoch=model.epoch, category=category))
    models = {r.model_id: r for r in rows if r.tier == 'screen'}
    groups = {}
    for row in result:
        if (row.source_tier == 'screen' and models[row.model_id].availability == 'eligible'
                and not models[row.model_id].public_only_reason):
            groups.setdefault((row.category, row.season), []).append(row)
    for group in groups.values():
        position, last_score, rank = 0, None, None
        for row in sorted(group, key=lambda r: (-r.score, r.model_id)):
            position += 1
            if row.score != last_score:
                rank, last_score = position, row.score
            row.rank = rank
    return result


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
    completed_sections = []
    for benchmark, count in COUNTS[tier].items():
        section = [r for r in items if r['benchmark'] == benchmark]
        if len(section) == count and all(r['status'] == 'graded' for r in section):
            completed_sections.extend(section)
    section_draws = component_bootstrap(completed_sections, settings.bootstrap_samples)
    benchmark_scores = {b: float(np.mean([r['score'] for r in completed_sections if r['benchmark'] == b])) * 100
                        for b in section_draws}
    benchmark_intervals = {b: np.quantile(v, [.025, .975]).tolist() for b, v in section_draws.items()}
    benchmark_dates = {}
    if cycle and completed_sections:
        dates = db.rows("""SELECT i.benchmark,a.finished_at FROM attempts a JOIN jobs j ON j.id=a.job_id
            JOIN items i ON i.id=j.item_id WHERE j.cycle_id=? AND j.status='graded'
            AND a.status IN ('received','recovered') AND a.finished_at IS NOT NULL
            AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)""",
                        (cycle['id'], cycle['season'], tier))
        benchmark_dates = {b: max((r['finished_at'] for r in dates if r['benchmark'] == b), default=None)
                           for b in benchmark_scores}
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
    manual = db.one("""SELECT COUNT(*) AS attempted,COUNT(DISTINCT CASE WHEN j.status='graded' THEN j.id END) AS recovered
        FROM (SELECT * FROM manual_retries UNION ALL SELECT * FROM manual_retry_extensions) r
        JOIN jobs j ON j.id=r.job_id WHERE j.cycle_id=?
        AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)""",
                    (cycle['id'], cycle['season'], tier)) if cycle else {'attempted': 0, 'recovered': 0}
    stamp = max((r["finished_at"] for r in db.rows("""SELECT a.finished_at FROM attempts a
        JOIN jobs j ON j.id=a.job_id WHERE j.cycle_id=? AND a.status IN ('received','recovered')
        AND j.item_id IN (SELECT item_id FROM panels WHERE season=? AND tier=?)""", (cycle["id"], cycle["season"], tier))
                 if r["finished_at"]), default=None) if cycle else None
    stale = bool(cycle and (datetime.now(timezone.utc) - datetime.fromisoformat(cycle["started_at"])).days >= 28)
    missed = bool(cycle and not complete and datetime.fromisoformat(cycle["due_at"]) < datetime.now(timezone.utc))
    status = "stale" if complete and stale else "complete" if complete else "pending"
    profile = json.loads(cycle['profile'] if cycle else model["profile"])
    availability = model['status']
    if availability in {'eligible', 'provider_error', 'cap_violation', 'cap_unverified'}:
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
        status = ("not_applicable" if availability == 'unsupported' else
                  "blocked" if availability in {'cap_violation', 'cap_unverified', 'client_access_restricted'} else
                  "public_only" if not gpqa_allowed(db, model) else
                  "cap_unverified" if model["status"] == "eligible" and not profile.get("cap_verified") else "pending")
    retry = db.one("""SELECT MIN(j.next_after) AS at FROM jobs j JOIN cycles c ON c.id=j.cycle_id
        WHERE c.model_id=? AND c.epoch=? AND j.status='deferred'
        AND j.error='Verified quota rejection; awaiting retry window'""", (model['id'], model['epoch']))
    failure = db.one("""SELECT a.usage,c.kind FROM attempts a JOIN jobs j ON j.id=a.job_id
        JOIN cycles c ON c.id=j.cycle_id WHERE c.model_id=? AND c.epoch=?
        AND j.status='failed' AND j.error LIKE '%output cap%' ORDER BY a.id DESC LIMIT 1""",
        (model['id'], model['epoch'])) if availability == 'cap_violation' and (not cycle or cycle['epoch'] == model['epoch']) else None
    output = json.loads(failure['usage'] or '{}').get('completion_tokens') if failure else None
    cap_failure = {'limit': 256 if failure['kind'] in {'health', 'cap_calibration'} else 4096,
                   'observed': output} if type(output) is int else None
    return PublicRow(model_id=model["id"], name=model["name"], epoch=cycle["epoch"] if cycle else model["epoch"],
                     client_version=profile.get('version'),
                     public_only_reason=public_only_reason(db, model),
                     tier=tier, season=cycle["season"] if cycle else None, status=status,
                     availability=availability, free_eligible=model['status'] in {'eligible','authentication_failed','quota_limited',
                         'model_unavailable','configuration_error','cap_violation','cap_unverified','client_access_restricted','provider_error'},
                     protocol=profile['protocol'], endpoint=profile.get('endpoint', model['endpoint']),
                     cap_verified=profile.get("cap_verified", False) and not (cycle and cycle['epoch'] == model['epoch']
                         and model['status'] in {'cap_violation', 'cap_unverified'}), observed_at=model["observed_at"],
                     cap_failure=cap_failure,
                     next_retry_at=retry['at'] if availability == 'quota_limited' else None,
                     reasoning_setting=reasoning_label(profile), reasoning_variant=profile.get('reasoning', {}).get('variant'),
                     evaluated_at=stamp,
                     cycle_started_at=cycle["started_at"] if cycle else None,
                     progress=progress, pending_reasons={s: sum(r['status'] == s for r in items) for s in
                         sorted({r['status'] for r in items if r['status'] != 'graded'})},
                     expected=COUNTS[tier], scores=scores, intervals=intervals,
                     benchmark_scores=benchmark_scores, benchmark_intervals=benchmark_intervals,
                     benchmark_evaluated_at=benchmark_dates,
                     sample_counts=progress if complete else None,
                     latency_seconds=float(np.median([r["latency"] for r in generated])) if generated else None,
                     accounted_tokens=attempts["accounted"], reported_tokens=attempts["reported"],
                     truncation_rate=sum(r["truncated"] for r in generated) / len(generated) if generated else None,
                     deadline_missed=missed or bool(complete and stale),
                     manual_retry_count=manual['attempted'], manual_recovered_questions=manual['recovered']), draws


def manual_recoveries(db: DB, cycle_id: str) -> dict:
    row = db.one("""SELECT COUNT(*) AS attempted,
        COUNT(DISTINCT CASE WHEN j.status='graded' THEN j.id END) AS recovered
        FROM (SELECT * FROM manual_retries UNION ALL SELECT * FROM manual_retry_extensions) r
        JOIN jobs j ON j.id=r.job_id WHERE j.cycle_id=?""", (cycle_id,))
    return {'manual_retry_count': row['attempted'], 'manual_recovered_questions': row['recovered']}


def snapshot(db: DB, settings: Settings) -> Snapshot:
    rows, history, draws_by_model = [], [], {}
    active = db.one("SELECT id FROM seasons WHERE active=1")
    season_id = active["id"] if active else ""
    for model in db.rows("SELECT * FROM models ORDER BY id"):
        if model['id'] in OMITTED_MODELS:
            continue
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
            if draws and public.availability == 'eligible' and gpqa_allowed(db, model):
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
                        protocol=data.get('protocol', {}), grading_files=data.get('grading_files', {}),
                        coding_promoted_at=data.get('coding_promoted_at')))
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
        WHERE j.status!='graded' AND c.kind IN ('health','cap_calibration','pilot','screen','public_screen','lightweight','coding','coding_pilot')
        AND m.id NOT IN ({','.join('?' for _ in OMITTED_MODELS)})
        AND json_extract(c.profile,'$.transport')='local-opencode'
        AND json_extract(c.profile,'$.protocol_revision')={REVISION}""", tuple(sorted(OMITTED_MODELS)))["n"]
    pilots = []
    for cycle in db.rows(f"""SELECT * FROM cycles WHERE kind='pilot'
        AND json_extract(profile,'$.transport')='local-opencode'
        AND json_extract(profile,'$.protocol_revision')={REVISION}
        AND epoch=(SELECT epoch FROM models WHERE id=cycles.model_id)
        ORDER BY started_at,model_id"""):
        if cycle['model_id'] in OMITTED_MODELS:
            continue
        model = db.one('SELECT * FROM models WHERE id=?', (cycle['model_id'],))
        season = db.one('SELECT manifest FROM seasons WHERE id=?', (cycle['season'],))
        if not json.loads(season['manifest']).get('partial') and not gpqa_allowed(db, model):
            continue
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
        retries_used = db.one("""SELECT COUNT(*) n FROM jobs j WHERE j.cycle_id=? AND j.status='ambiguous'
            AND EXISTS(SELECT 1 FROM manual_retries r WHERE r.job_id=j.id)""", (cycle['id'],))['n']
        pilots.append(PublicPilot(model_id=cycle['model_id'], epoch=cycle['epoch'],
            unanswered_retries_used=retries_used,
            public_only_reason=public_only_reason(db, model),
            reasoning_setting=reasoning_label(json.loads(cycle['profile'])),
            reasoning_variant=json.loads(cycle['profile']).get('reasoning', {}).get('variant'),
            season=cycle['season'], status='complete' if complete else 'pending',
            health_graded=health, cap_probe_verified=verified, progress=progress, expected=expected,
            pending_reasons={s: sum(r['status'] == s for r in items) for s in sorted({r['status'] for r in items if r['status'] != 'graded'})},
            benchmark_scores={b: float(np.mean([r['score'] for r in items if r['benchmark'] == b])) * 100
                              for b in expected if expected[b] and progress[b] == expected[b]},
            evaluated_at=cycle['completed_at'] if complete else None,
            **manual_recoveries(db, cycle['id'])))
    public_screens = []
    for cycle in db.rows(f"""SELECT * FROM cycles WHERE kind='public_screen'
        AND json_extract(profile,'$.transport')='local-opencode'
        AND json_extract(profile,'$.protocol_revision')={REVISION}
        ORDER BY started_at,model_id"""):
        if cycle['model_id'] in OMITTED_MODELS:
            continue
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
            client_version=profile['version'],
            public_only_reason=public_only_reason(db, db.one('SELECT * FROM models WHERE id=?', (cycle['model_id'],))),
            reasoning_setting=reasoning_label(profile), reasoning_variant=profile.get('reasoning', {}).get('variant'),
            cap_verified=profile.get('cap_verified', False) and not any(
                r['status'] == 'cap_unverified' or 'output cap' in (r.get('error') or '').lower() for r in items),
            status='complete' if complete else 'pending', progress=progress, expected=expected,
            pending_reasons={s: sum(('grading_blocked' if r['status'] == 'generated' and r.get('error') else r['status']) == s for r in items)
                             for s in sorted({'grading_blocked' if r['status'] == 'generated' and r.get('error') else r['status']
                                              for r in items if r['status'] != 'graded'})},
            benchmark_scores=scores, intervals=intervals, benchmark_evaluated_at=component_dates,
            evaluated_at=cycle['completed_at'] if complete else None,
            cycle_started_at=cycle['started_at'],
            latency_seconds=float(np.median([r['latency'] for r in generated_items])) if generated_items else None,
            accounted_tokens=usage['accounted'], reported_tokens=usage['reported'],
            truncation_rate=sum(r['truncated'] for r in generated_items) / len(generated_items) if generated_items else None,
            **manual_recoveries(db, cycle['id'])))
    lightweight = lightweight_results(db, settings)
    coding = coding_results(db, settings)
    capabilities = merged_capabilities(rows, public_screens, lightweight, history)
    capabilities, coding_weights = expand_coding(db, settings, capabilities, rows, coding)
    comparisons += expanded_comparisons(db,settings,capabilities)
    generated = now()
    return Snapshot(snapshot_id=digest([generated, [r.model_dump() for r in rows],
                                       [r.model_dump() for r in public_screens], [r.model_dump() for r in pilots],
                                       [r.model_dump() for r in lightweight], [r.model_dump() for r in coding]])[:20],
                    repository=settings.repository, generated_at=generated, discovery_at=(discovery or {}).get("observed_at"),
                    discovery_ok=bool((discovery or {}).get("ok")), discovery_error=(discovery or {}).get("error"),
                    rows=rows, history=history, comparisons=comparisons, pilots=pilots, public_screens=public_screens,
                    lightweight=lightweight, capabilities=capabilities, seasons=manifests,
                    coding_evaluations=coding, coding_weights=coding_weights,
                    coding_expansion_status=coding_expansion_status(rows, coding, coding_weights),
                    budget=Budget(db, settings).summary(), queue_size=queue,
                    missed_deadlines=sum(r.deadline_missed for r in rows if r.tier == 'screen'), blockers=blockers)


def coding_expansion_status(rows: list[PublicRow], evaluations: list[CodingEvaluation],
                            weights: dict[str, float]) -> str:
    if any(weights.get(s['benchmark']) == 0.5 for s in SPECS.values()):
        return 'active'
    if not evaluations:
        return 'not-started'
    current = {r.model_id: r for r in rows if r.tier == 'screen'}
    pending = [e for e in evaluations if e.status == 'pending'
               and e.model_id in current and current[e.model_id].epoch == e.epoch
               and current[e.model_id].availability not in {'removed', 'paid', 'excluded', 'unsupported'}]
    restricted = {'client_access_restricted', 'authentication_failed'}
    if pending and all(current[e.model_id].availability in restricted for e in pending):
        return 'access-restricted'
    return 'pending'


def coding_results(db: DB, settings: Settings) -> list[CodingEvaluation]:
    result = []
    for cycle in db.rows("SELECT * FROM cycles WHERE kind IN ('coding','coding_pilot') ORDER BY started_at,model_id"):
        is_pilot = cycle['kind'] == 'coding_pilot'
        if is_pilot and db.one("SELECT id FROM cycles WHERE kind='coding' AND model_id=? AND epoch=? AND season=?",
                              (cycle['model_id'],cycle['epoch'],cycle['season'])):
            continue
        profile = json.loads(cycle['profile'])
        if (cycle['model_id'] in OMITTED_MODELS or profile.get('version') != VERSION
                or profile.get('protocol_revision') != REVISION or profile.get('transport') != 'local-opencode'):
            continue
        season = db.one('SELECT * FROM seasons WHERE id=?', (cycle['season'],))
        manifest = json.loads(season['manifest'])
        if manifest.get('protocol', {}).get('panel') not in SPECS:
            continue
        spec = coding_spec(manifest)
        benchmark, strata = spec['benchmark'], spec['strata']
        items = records(db,cycle,'coding_pilot' if is_pilot else 'coding')
        pilot = db.one("SELECT id FROM cycles WHERE kind='coding_pilot' AND model_id=? AND epoch=? AND season=? AND completed_at IS NOT NULL",
                       (cycle['model_id'],cycle['epoch'],cycle['season']))
        pilot_expected = len(strata)
        complete = (not is_pilot and season['validated']
            and len(items) == 24 and sum(i['status']=='graded' for i in items)==24 and pilot
            and profile.get('cap_verified') and all(i['score'] in {0,1} for i in items)
            and all(i['benchmark'] == benchmark for i in items)
            and all(sum(i['stratum']==key for i in items)==count for key,count in strata.items()))
        usage = db.one('SELECT COALESCE(SUM(accounted_tokens),0) accounted,COALESCE(SUM(reported_tokens),0) reported,MAX(finished_at) stamp FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE cycle_id=?)', (cycle['id'],))
        generated = [i for i in items if i['latency'] is not None]
        intervals = component_bootstrap(items,settings.bootstrap_samples) if complete else {}
        result.append(CodingEvaluation(model_id=cycle['model_id'],epoch=cycle['epoch'],season=cycle['season'],benchmark=benchmark,
            status='complete' if complete else 'pending',phase='pilot' if is_pilot else 'screen',
            pilot_progress=sum(i['status']=='graded' for i in items) if is_pilot else (pilot_expected if pilot else 0),
            pilot_expected=pilot_expected,
            progress=0 if is_pilot else sum(i['status']=='graded' for i in items),
            score=float(np.mean([i['score'] for i in items]))*100 if complete else None,
            interval=np.quantile(intervals[benchmark],[.025,.975]).tolist() if complete else None,
            evaluated_at=usage['stamp'] if complete else None, started_at=cycle['started_at'],
            pending_reasons={s:sum(i['status']==s for i in items) for s in {i['status'] for i in items if i['status']!='graded'}},
            accounted_tokens=usage['accounted'],reported_tokens=usage['reported'],
            truncation_rate=sum(i['truncated'] for i in generated)/len(generated) if generated else None))
    return result


def coding_records(db: DB, model_id: str, epoch: str, season: str, kind: str, tier: str, benchmark: str) -> list[dict]:
    for cycle in db.rows('SELECT * FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind=? AND completed_at IS NOT NULL ORDER BY started_at DESC',
                         (model_id,epoch,season,kind)):
        selected = [r for r in records(db,cycle,tier) if r['benchmark']==benchmark]
        if selected and all(r['status']=='graded' for r in selected):
            return selected
    return []


def expand_coding(db: DB, settings: Settings, capabilities: list[CapabilityResult], models: list[PublicRow],
                  evaluations: list[CodingEvaluation]) -> tuple[list[CapabilityResult], dict[str, float]]:
    eligible = {m.model_id for m in models if m.tier=='screen' and m.cap_verified
                and getattr(m, 'free_eligible', m.availability == 'eligible')}
    bases = [c for c in capabilities if c.category=='coding'
             and c.score is not None and c.sample_counts == {'livecodebench':40}]
    promoted = db.one("SELECT * FROM seasons WHERE validated=1 AND json_extract(manifest,'$.coding_promoted_at') IS NOT NULL ORDER BY created_at DESC LIMIT 1")
    # A replacement extension must cover the same currently free cohort before
    # replacing either the original panel or a previously promoted extension.
    required = [b for b in bases if b.model_id in eligible]
    chosen = None
    for candidate in db.rows('SELECT * FROM seasons WHERE validated=1 ORDER BY created_at DESC,id DESC'):
        manifest = json.loads(candidate['manifest'])
        if manifest.get('protocol', {}).get('panel') not in SPECS:
            continue
        complete = {(e.model_id,e.epoch):e for e in evaluations
                    if e.season==candidate['id'] and e.status=='complete'}
        if required and all((b.model_id,b.epoch) in complete and not b.stale
                and (datetime.now(timezone.utc)-datetime.fromisoformat(complete[(b.model_id,b.epoch)].started_at)).days<28
                for b in required):
            chosen = candidate
            break
    chosen = chosen or promoted
    if not chosen:
        return capabilities, {'livecodebench':1.0}
    benchmark = coding_spec(json.loads(chosen['manifest']))['benchmark']
    weights = {'livecodebench':0.5, benchmark:0.5}
    latest = {(e.model_id,e.epoch):e for e in evaluations if e.status=='complete' and e.season==chosen['id']}
    replacements, groups = {}, {}
    for base in bases:
        if (base.model_id,base.epoch) not in latest:
            continue
        extension = latest[(base.model_id,base.epoch)]
        stale = (datetime.now(timezone.utc)-datetime.fromisoformat(extension.started_at)).days >= 28 or base.stale
        if chosen != promoted and stale:
            return capabilities, {'livecodebench':1.0}
        kind = {'screen':'screen','public-screen':'public_screen'}.get(base.source_tier)
        if not kind:
            return capabilities, {'livecodebench':1.0}
        lcb = coding_records(db,base.model_id,base.epoch,base.season,kind,'screen','livecodebench')
        bcb = coding_records(db,base.model_id,base.epoch,extension.season,'coding','coding',benchmark)
        if len(lcb)!=40 or len(bcb)!=24:
            return capabilities, {'livecodebench':1.0}
        draws = component_bootstrap(lcb+bcb,settings.bootstrap_samples)
        combined = (draws['livecodebench']+draws[benchmark])/2
        dates = {'livecodebench':base.evaluated_at,benchmark:extension.evaluated_at}
        result = base.model_copy(update={'source_tier':'coding-expanded','season':extension.season,
            'score':(base.score+extension.score)/2,'interval':np.quantile(combined,[.025,.975]).tolist(),
            'interval_note':None,'sample_counts':{'livecodebench':40,benchmark:24},
            'benchmark_scores':{'livecodebench':base.score,benchmark:extension.score},
            'benchmark_weights':weights, 'benchmark_evaluated_at':dates,
            'source_seasons':{'livecodebench':base.season,benchmark:extension.season},
            'evaluated_at':max(d for d in dates.values() if d),'rank':None,'stale':stale,
            'refresh_pending':base.refresh_pending or any(e.model_id==base.model_id and e.epoch==base.epoch
                and e.status=='pending' and e.started_at>extension.started_at for e in evaluations)})
        # Rank only identical question panels. GPQA access is irrelevant to an
        # entirely public coding panel, including for public-only models.
        manifest = json.loads(db.one('SELECT manifest FROM seasons WHERE id=?',(base.season,))['manifest'])
        signature = digest(sorted((i['id'].split(':',1)[1] if i['id'].startswith(('s1-','public-pilot-')) else i['id'], i['hash'])
            for i in manifest['panels']['screen'] if i['benchmark']=='livecodebench'))
        if result.model_id in eligible:
            groups.setdefault(signature,[]).append(result)
        replacements[base.model_id] = result
    common = max(groups.values(),key=lambda group:(len(group),sorted(r.model_id for r in group))) if groups else []
    rank, prior = 0, None
    for position,row in enumerate(sorted(common,key=lambda r:(-r.score,r.model_id)),1):
        if row.score!=prior:
            rank,prior = position,row.score
        row.rank=rank
    return [replacements.get(c.model_id,c.model_copy(update={'score':None,'interval':None,'rank':None,'refresh_pending':True}))
            if c.category=='coding' else c for c in capabilities], weights


def expanded_comparisons(db: DB, settings: Settings, capabilities: list[CapabilityResult]) -> list[Comparison]:
    results = [r for r in capabilities if r.category=='coding' and r.source_tier=='coding-expanded' and r.rank is not None]
    draws = {}
    for row in results:
        lcb=[]
        for kind in ['screen','public_screen']:
            lcb=coding_records(db,row.model_id,row.epoch,row.source_seasons['livecodebench'],kind,'screen','livecodebench')
            if lcb:
                break
        benchmark=next(b for b in row.benchmark_weights if b!='livecodebench')
        bcb=coding_records(db,row.model_id,row.epoch,row.season,'coding','coding',benchmark)
        values=component_bootstrap(lcb+bcb,settings.bootstrap_samples)
        draws[row.model_id]=(values['livecodebench']+values[benchmark])/2
    comparisons=[]
    for a,b in itertools.combinations(results,2):
        interval=np.quantile(draws[a.model_id]-draws[b.model_id],[.025,.975]).tolist()
        comparisons.append(Comparison(a=a.model_id,b=b.model_id,tier='coding-expanded',season=a.season,view='coding',
            difference=a.score-b.score,interval=interval,unresolved=interval[0]<=0<=interval[1]))
    return comparisons


def lightweight_results(db: DB, settings: Settings) -> list[Lightweight]:
    result = []
    for cycle in db.rows("SELECT * FROM cycles WHERE kind='lightweight' ORDER BY started_at,model_id"):
        if cycle['model_id'] in OMITTED_MODELS:
            continue
        items = records(db, cycle, 'lightweight')
        manifest = json.loads(db.one('SELECT manifest FROM seasons WHERE id=?', (cycle['season'],))['manifest'])
        expected = manifest['counts']['lightweight']
        progress = {b: sum(r['benchmark'] == b and r['status'] == 'graded' for r in items) for b in expected}
        complete = len(items) == 12 and expected == {'livebench': 6, 'livecodebench': 6} and progress == expected
        profile = json.loads(cycle['profile'])
        model = db.one('SELECT * FROM models WHERE id=?', (cycle['model_id'],))
        health = db.one("""SELECT COUNT(*) graded,COALESCE(SUM(j.score),0) passed FROM jobs j
            WHERE j.cycle_id=(SELECT id FROM cycles WHERE kind='health' AND model_id=? AND epoch=? AND season=?
                ORDER BY started_at LIMIT 1) AND j.status='graded'""",
                        (cycle['model_id'], cycle['epoch'], cycle['season']))
        usage = db.one('SELECT COALESCE(SUM(accounted_tokens),0) accounted,COALESCE(SUM(reported_tokens),0) reported FROM attempts WHERE job_id IN (SELECT id FROM jobs WHERE cycle_id=?)', (cycle['id'],))
        dates = {b: db.one("""SELECT MAX(a.finished_at) stamp FROM attempts a JOIN jobs j ON j.id=a.job_id
            JOIN items i ON i.id=j.item_id WHERE j.cycle_id=? AND i.benchmark=? AND j.status='graded'
            AND a.status IN ('received','recovered')""", (cycle['id'], b))['stamp'] for b in expected}
        generated_items = [r for r in items if r['latency'] is not None]
        current = model['epoch'] == cycle['epoch']
        rejections = sum(bool(json.loads(Path(r['response_path']).read_text()).get('tool_request_rejected'))
                         for r in generated_items if r.get('response_path'))
        scores = {b: float(np.mean([r['score'] for r in items if r['benchmark'] == b])) * 100 for b in expected} if complete else {}
        intervals = {b: np.quantile(v, [.025, .975]).tolist() for b,v in component_bootstrap(items, settings.bootstrap_samples).items()} if complete else {}
        warnings = {b: 'Unresolved uncertainty: six-question bootstrap interval collapses'
                    for b, limits in intervals.items() if limits[0] == limits[1]}
        intervals = {b: limits for b, limits in intervals.items() if b not in warnings}
        cap = bool(json.loads(model['profile']).get('cap_verified')) if current else bool(complete)
        cap = cap and not any(r['status'] in {'failed','cap_unverified'} for r in items)
        reasons = {s: sum(r['status'] == s for r in items) for s in sorted({r['status'] for r in items if r['status'] != 'graded'})}
        manual = manual_recoveries(db, cycle['id'])
        result.append(Lightweight(model_id=cycle['model_id'], epoch=cycle['epoch'], season=cycle['season'],
            public_only_reason=public_only_reason(db, model),
            client_version=profile['version'], protocol_revision=profile['protocol_revision'],
            reasoning_setting=reasoning_label(profile), reasoning_variant=profile.get('reasoning',{}).get('variant'),
            availability=model['status'] if current else 'historical', cap_verified=cap,
            status='complete' if complete else 'pending', progress=progress, expected=expected,
            health_graded=health['graded'], health_passed=int(health['passed']), tool_rejections=rejections, pending_reasons=reasons,
            **manual,
            benchmark_scores=scores, intervals=intervals, interval_warnings=warnings, benchmark_evaluated_at=dates,
            evaluated_at=cycle['completed_at'] if complete else None,
            latency_seconds=float(np.median([r['latency'] for r in generated_items])) if generated_items else None,
            accounted_tokens=usage['accounted'], reported_tokens=usage['reported'],
            truncation_rate=sum(r['truncated'] for r in generated_items)/len(generated_items) if generated_items else None))
    return result
