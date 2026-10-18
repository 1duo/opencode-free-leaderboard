"""Sanitized, reviewable community results. No response text crosses this boundary."""
from __future__ import annotations

import json
import platform
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .config import COUNTS, ZEN_CATALOG, ZEN_DOCS, digest
from .coding_protocol import coding_spec

Hash = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]
Handle = Annotated[str, Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}$')]
Effort = Literal['minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'thinking']
AvailableEffort = Literal['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'thinking']
Tier = Literal['screen', 'confirmation', 'public-screen', 'lightweight', 'coding']


class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class QuestionResult(Strict):
    id: str = Field(pattern=r'^(gpqa|livebench|livecodebench|bigcodebench|systems_c|humanevalpack):[A-Za-z0-9_.:-]+$', max_length=200)
    hash: Hash
    score: float = Field(ge=0, le=1)
    attempts: int = Field(ge=1, strict=True)
    manual_retries: int = Field(ge=0, strict=True)
    output_tokens: int = Field(ge=0, le=4096, strict=True)
    reasoning_tokens: int | None = Field(ge=0, strict=True)
    accounted_tokens: int = Field(ge=0, strict=True)
    reported_tokens: int | None = Field(ge=0, strict=True)
    latency_seconds: float = Field(ge=0)
    truncated: bool

    @model_validator(mode='after')
    def consistent_usage(self):
        if ((self.reasoning_tokens or 0) > self.output_tokens or self.manual_retries >= self.attempts
                or self.reported_tokens is not None and self.reported_tokens > self.accounted_tokens):
            raise ValueError('Inconsistent question usage or retry counts')
        return self


class ResultSubmission(Strict):
    schema_version: Literal[1] = 1
    submission_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    contributor: Handle
    model_id: str = Field(pattern=r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$')
    epoch: str = Field(pattern=r'^[0-9a-f]{16}$')
    season: str = Field(pattern=r'^(s1|public-pilot|lightweight|coding)-[0-9a-f]{16}$')
    tier: Tier
    panel_fingerprint: Hash
    runner_revision: str | None = Field(default=None, pattern=r'^[0-9a-f]{40}$')
    exporter_revision: str = Field(pattern=r'^[0-9a-f]{40}$')
    client_version: Literal['1.19.3'] = '1.19.3'
    protocol_revision: Literal[6] = 6
    reasoning_mode: Literal['highest-exposed', 'provider-managed']
    reasoning_variant: Effort | None
    available_variants: list[AvailableEffort]
    definition_hash: Hash
    output_cap: Literal[4096] = 4096
    temperature: Literal[0] = 0
    tools_permitted: Literal[False] = False
    completions_per_question: Literal[1] = 1
    grader_image: str = Field(pattern=r'^sha256:[0-9a-f]{64}$')
    platform: Literal['darwin-arm64', 'darwin-x86_64', 'linux-aarch64', 'linux-x86_64']
    started_at: datetime
    evaluated_at: datetime
    free_observed_at: datetime
    pricing_source: Literal[ZEN_DOCS] = ZEN_DOCS
    catalog_source: Literal[ZEN_CATALOG] = ZEN_CATALOG
    zero_price_verified: Literal[True] = True
    gpqa_privacy_verified: bool
    questions: list[QuestionResult] = Field(min_length=12, max_length=240)

    @model_validator(mode='after')
    def valid_protocol(self):
        dates = [self.started_at, self.evaluated_at, self.free_observed_at]
        if any(d.tzinfo is None for d in dates) or self.evaluated_at < self.started_at:
            raise ValueError('Evaluation dates must be timezone-aware and ordered')
        if max(dates) > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError('Evaluation dates cannot be in the future')
        if self.free_observed_at < self.started_at - timedelta(days=7):
            raise ValueError('Free eligibility evidence predates this evaluation by more than seven days')
        order = ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']
        highest = next((v for v in reversed(order) if v in self.available_variants),
                       'thinking' if 'thinking' in self.available_variants else None)
        if (len(set(self.available_variants)) != len(self.available_variants)
                or self.reasoning_variant != highest
                or (self.reasoning_mode == 'provider-managed') != (highest is None)):
            raise ValueError('Highest advertised reasoning setting must be selected')
        if self.tier in {'screen', 'confirmation'} and not self.gpqa_privacy_verified:
            raise ValueError('Full panels require private-dataset permission')
        return self


class CommunityResult(Strict):
    submission_id: str
    contributor: str
    model_id: str
    season: str
    tier: str
    verification: Literal['community-submitted'] = 'community-submitted'
    evaluated_at: str
    started_at: str
    reasoning_setting: str
    reasoning_variant: str | None
    sample_counts: dict[str, int]
    benchmark_scores: dict[str, float]
    scores: dict[str, float]
    intervals: dict[str, list[float]]
    interval_notes: dict[str, str] = {}
    accounted_tokens: int
    reported_tokens: int


def canonical_id(value: str) -> str:
    prefix, separator, suffix = value.partition(':')
    return suffix if separator and prefix.startswith(('s1-', 'public-pilot-', 'lightweight-')) else value


def panel(manifest: dict, tier: str) -> list[dict]:
    key = {'public-screen': 'screen'}.get(tier, tier)
    return sorted([{**item, 'id': canonical_id(item['id'])} for item in manifest['panels'].get(key, [])], key=lambda i: i['id'])


def fingerprint(manifest: dict, tier: str) -> str:
    return digest({**{k: manifest.get(k) for k in ['seed', 'sources', 'grading_files', 'livebench_release',
        'livecodebench_release', 'language']}, 'tier': tier, 'panel': panel(manifest, tier)})


def validate_submission(record: ResultSubmission, manifests: list[dict]) -> CommunityResult:
    from .scoring import bootstrap, component_bootstrap
    manifest = next((m for m in manifests if m['id'] == record.season and m['validated']), None)
    if not manifest or record.panel_fingerprint != fingerprint(manifest, record.tier):
        raise ValueError('Submission does not match a published, validated panel')
    selected = panel(manifest, record.tier)
    coding_benchmark = None
    if record.tier == 'coding':
        if manifest.get('protocol', {}).get('panel'):
            coding_benchmark = coding_spec(manifest)['benchmark']
        elif manifest.get('counts', {}).get('coding') == {'bigcodebench': 24}:
            coding_benchmark = 'bigcodebench'  # Original public submission format.
        else:
            raise ValueError('Unknown frozen coding protocol')
    expected = (COUNTS[record.tier] if record.tier in {'screen', 'confirmation'} else
                {'livebench': 20, 'livecodebench': 40} if record.tier == 'public-screen' else
                {coding_benchmark:24} if record.tier == 'coding' else {'livebench': 6, 'livecodebench': 6})
    counts = {b: sum(i['benchmark'] == b for i in selected) for b in expected}
    if counts != expected or len(selected) != sum(expected.values()):
        raise ValueError('Submission tier has incorrect benchmark coverage')
    if record.tier == 'coding' and manifest.get('protocol', {}).get('panel'):
        strata = coding_spec(manifest)['strata']
        if {s: sum(i['stratum'] == s for i in selected) for s in strata} != strata:
            raise ValueError('Submission tier has incorrect coding domain coverage')
    if len(record.questions) != len(selected) or len({q.id for q in record.questions}) != len(selected):
        raise ValueError('Complete panels with unique question IDs are required')
    reference = {i['id']: i for i in selected}
    graded = []
    for question in record.questions:
        item = reference.get(question.id)
        if not item or item['hash'] != question.hash:
            raise ValueError('Question IDs/hashes differ from the frozen panel')
        if item['benchmark'] in {'gpqa', 'livecodebench', 'bigcodebench', 'systems_c', 'humanevalpack'} and question.score not in {0, 1}:
            raise ValueError('GPQA and coding grades must be binary')
        graded.append({'item_id': question.id, 'benchmark': item['benchmark'], 'stratum': item['stratum'], 'score': question.score})
    draws = bootstrap(graded) if record.tier in {'screen', 'confirmation'} else component_bootstrap(graded)
    scores = {b: float(np.mean([i['score'] for i in graded if i['benchmark'] == b])) * 100 for b in expected}
    categories = ({'coding':scores[coding_benchmark]} if record.tier=='coding' else
                  {'reasoning': (scores['gpqa'] + scores['livebench']) / 2 if 'gpqa' in scores else scores['livebench'],
                   'coding': scores['livecodebench']})
    intervals, notes = {}, {}
    for category in categories:
        key = (coding_benchmark if record.tier=='coding' else category if 'gpqa' in scores else
               {'reasoning': 'livebench', 'coding': 'livecodebench'}[category])
        bounds = np.quantile(draws[key], [.025, .975]).tolist()
        if record.tier == 'lightweight' and bounds[0] == bounds[1]:
            notes[category] = 'Unresolved six-question interval'
        else:
            intervals[category] = bounds
    return CommunityResult(submission_id=record.submission_id, contributor=record.contributor, model_id=record.model_id,
        season=record.season, tier=record.tier, evaluated_at=record.evaluated_at.isoformat(), started_at=record.started_at.isoformat(),
        reasoning_setting=f'Highest exposed: {record.reasoning_variant}' if record.reasoning_variant else 'Provider-managed · no selectable level',
        reasoning_variant=record.reasoning_variant, sample_counts=counts, benchmark_scores=scores, scores=categories,
        intervals=intervals, interval_notes=notes, accounted_tokens=sum(q.accounted_tokens for q in record.questions),
        reported_tokens=sum(q.reported_tokens or 0 for q in record.questions))


def load_submissions(checkout: Path, manifests: list[dict]) -> list[CommunityResult]:
    folder = checkout / 'results'
    if not folder.exists():
        return []
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Result submissions must be a regular directory')
    results = []
    seen = set()
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1_000_000:
            raise ValueError('Unexpected or oversized result file')
        try:
            record = ResultSubmission.model_validate_json(path.read_text())
        except ValueError:
            raise ValueError(f'Invalid sanitized result submission: {path.name}') from None
        if path.name != f'{record.submission_id}.json':
            raise ValueError('Result filename must match its submission ID')
        identity = (record.contributor.lower(), record.model_id, record.epoch, record.tier,
                    record.started_at, record.evaluated_at, record.panel_fingerprint)
        if identity in seen:
            raise ValueError('Duplicate submitted evaluation')
        seen.add(identity)
        results.append(validate_submission(record, manifests))
    return results


def validate_pr_submissions(checkout: Path, base: str, author: str) -> None:
    """Bind new records to the GitHub PR author; preserve accepted records."""
    if not re.fullmatch(r'[0-9a-f]{40}', base):
        raise ValueError('Invalid trusted PR context')
    changed = subprocess.run(['git', 'diff', '--name-status', '--no-renames', base, '--', 'results'],
                             cwd=checkout, check=True, capture_output=True, text=True, timeout=30).stdout
    if changed and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}', author):
        raise ValueError('Result submissions need a valid GitHub contributor identity')
    for line in changed.splitlines():
        status, name = line.split('\t', 1)
        if status != 'A' or not re.fullmatch(r'results/[0-9a-f]{32}\.json', name):
            raise ValueError('Result PRs append new records; accepted submissions are immutable')
        record = ResultSubmission.model_validate_json((checkout / name).read_text())
        if record.contributor.lower() != author.lower():
            raise ValueError('New submission contributor must match the GitHub PR author')


def export_submission(db, checkout: Path, model_id: str, contributor: str, tier: str,
                      season_id: str | None = None, output: Path | None = None) -> dict:
    from .adapters import parse
    from .privacy import gpqa_allowed
    manifests = json.loads((checkout / 'site/manifest.json').read_text())
    model = db.one("SELECT * FROM models WHERE id=? AND status='eligible'", (model_id,))
    if not model or not json.loads(model['profile']).get('cap_verified'):
        raise ValueError('Submission requires current free eligibility and a verified cap')
    if datetime.now(timezone.utc) - datetime.fromisoformat(model['observed_at']) > timedelta(days=7):
        raise ValueError('Refresh official free eligibility before exporting a submission')
    kind = {'confirmation': 'screen', 'public-screen': 'public_screen'}.get(tier, tier)
    cycles = db.rows('SELECT * FROM cycles WHERE model_id=? AND epoch=? AND kind=? ORDER BY started_at DESC',
                     (model_id, model['epoch'], kind))
    cycle = next((c for c in cycles if not season_id or c['season'] == season_id), None)
    if not cycle:
        raise ValueError('No matching evaluation cycle')
    profile = json.loads(cycle['profile'])
    if (profile.get('version') != '1.19.3' or profile.get('protocol_revision') != 6
            or profile.get('protocol') != 'opencode' or profile.get('transport') != 'local-opencode'):
        raise ValueError('Only the validated native protocol can be submitted')
    source = json.loads(db.one('SELECT manifest FROM seasons WHERE id=? AND validated=1', (cycle['season'],))['manifest'])
    matching = next((m for m in manifests if m['validated'] and fingerprint(m, tier) == fingerprint(source, tier)), None)
    if matching is None:
        raise ValueError('Frozen panel is not published; propose a season change separately')
    pilot_kind = 'coding_pilot' if tier=='coding' else 'pilot'
    if tier != 'lightweight' and not db.one("SELECT id FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind=? AND completed_at IS NOT NULL",
                                          (model_id, model['epoch'], cycle['season'],pilot_kind)):
        raise ValueError('A completed matching pilot is required')
    panel_tier = {'public-screen': 'screen'}.get(tier, tier)
    jobs = db.rows("""SELECT j.*,i.question_hash FROM jobs j JOIN items i ON i.id=j.item_id
        JOIN panels p ON p.item_id=j.item_id AND p.season=? AND p.tier=? WHERE j.cycle_id=? ORDER BY j.item_id""",
        (cycle['season'], panel_tier, cycle['id']))
    questions, dates = [], []
    for job in jobs:
        if job['status'] != 'graded' or not job['response_path']:
            raise ValueError('All selected questions must have saved, graded answers')
        response = json.loads(Path(job['response_path']).read_text())
        completion = parse(response['protocol'], json.loads(response['body']))
        if (not completion.bounded(4096) or response.get('cap') != 4096
                or response.get('protocol_revision') != 6 or response['protocol'] != 'opencode'):
            raise ValueError('Saved response does not establish the frozen protocol and output cap')
        usage = db.one('SELECT COUNT(*) n,SUM(accounted_tokens) accounted,SUM(reported_tokens) reported,MAX(finished_at) finished FROM attempts WHERE job_id=?', (job['id'],))
        dates.append(usage['finished'])
        retries = db.one('SELECT COUNT(*) n FROM (SELECT job_id FROM manual_retries UNION ALL SELECT job_id FROM manual_retry_extensions) WHERE job_id=?', (job['id'],))['n']
        questions.append(QuestionResult(id=canonical_id(job['item_id']), hash=job['question_hash'], score=job['score'],
            attempts=usage['n'], manual_retries=retries, output_tokens=completion.output_tokens, reasoning_tokens=completion.reasoning_tokens,
            accounted_tokens=usage['accounted'], reported_tokens=usage['reported'], latency_seconds=job['latency'], truncated=bool(job['truncated'])))
    clean = subprocess.run(['git', 'diff', '--exit-code', 'HEAD', '--', 'src', 'grading'], cwd=checkout, capture_output=True, timeout=15)
    if clean.returncode:
        raise ValueError('Commit runner/grader source changes before recording its revision')
    revision = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=checkout, capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    control = profile['reasoning']
    ident = digest([contributor, model_id, model['epoch'], cycle['id'], cycle['started_at'], tier])[:32]
    record = ResultSubmission(submission_id=ident, contributor=contributor, model_id=model_id, epoch=model['epoch'], season=matching['id'], tier=tier,
        panel_fingerprint=fingerprint(matching, tier), runner_revision=profile.get('runner_revision'), exporter_revision=revision,
        reasoning_mode=control['mode'], reasoning_variant=control['variant'],
        available_variants=control['available_variants'], definition_hash=control['definition_hash'], grader_image=source['grader_image'],
        platform=f'{platform.system().lower()}-{platform.machine()}', started_at=cycle['started_at'], evaluated_at=max(dates, default=cycle['started_at']),
        free_observed_at=model['observed_at'], gpqa_privacy_verified=gpqa_allowed(db, model), questions=questions)
    validate_submission(record, manifests)
    folder = output.resolve() if output else checkout / 'results'
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = folder / f'{ident}.json'
    if target.exists():
        saved = ResultSubmission.model_validate_json(target.read_text())
        volatile = {'exporter_revision', 'free_observed_at'}
        if saved.model_dump(exclude=volatile) != record.model_dump(exclude=volatile):
            raise ValueError('Existing submission differs; preserve accepted records and use a new evaluation cycle')
        return {'submission': str(target), 'questions': len(questions), 'verification': 'community-submitted', 'existing': True}
    staged = target.with_suffix('.partial')
    staged.write_text(record.model_dump_json(indent=2) + '\n')
    staged.replace(target)
    return {'submission': str(target), 'questions': len(questions), 'verification': 'community-submitted',
            'next': 'Review this sanitized file and open a pull request; do not commit private state or replace site/.'}
