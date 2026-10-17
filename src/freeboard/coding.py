"""Pinned, CPU-only BigCodeBench-Instruct expansion; legacy panels stay immutable."""
from __future__ import annotations

import ast
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq

from .budget import estimate
from .coding_protocol import coding_spec
from .config import SEED, digest, now, week
from .discovery import discover
from .grading import GradingUnavailable, docker_environment
from .panels import download, upstream

DATA_REVISION = 'b74c0d0bf70d2c0bc459be537895cca163007f1a'
GRADER_REVISION = '09dd993f46c3fbf3a799465bb96d524edcb0b199'
PANEL = 'bigcodebench-cpu-v1'
FILES = ['coding.Dockerfile', 'coding_worker.py', 'coding-requirements.txt']
STRATA = {'stdlib': 12, 'scientific': 12}
RUNTIME = ('Runtime: Python 3.11 with its standard library, NumPy 2.2.6, SciPy 1.15.3, '
           'SymPy 1.14.0 and Matplotlib 3.10.1. No network access. Return self-contained Python code.')


def select_panel(rows: list[dict]) -> dict[str, list[dict]]:
    pools = {key: [] for key in STRATA}
    for row in rows:
        try:
            tree = ast.parse(row['complete_prompt'] + '\n' + row['canonical_solution'] + '\n' + row['test'])
        except SyntaxError:
            continue
        imports = set()
        for part in ast.walk(tree):
            if isinstance(part, ast.Import):
                imports.update(name.name.split('.')[0] for name in part.names)
            elif isinstance(part, ast.ImportFrom):
                imports.add((part.module or '').split('.')[0])
        # This environment restriction is applied before deterministic sampling,
        # reference validation and any model answers; no failed task is replaced.
        if imports & {'socket', 'urllib', 'http', 'subprocess'}:
            continue
        external = imports - set(sys.stdlib_module_names) - {'__future__'}
        if not external <= {'numpy', 'scipy', 'sympy'}:
            continue
        pools['scientific' if external else 'stdlib'].append(row)
    panels = {'coding': [], 'coding_pilot': []}
    for stratum, count in STRATA.items():
        pool = sorted(pools[stratum], key=lambda row: digest([SEED, PANEL, row['task_id']]))
        if len(pool) < count + 1:
            raise ValueError('Insufficient CPU-compatible BigCodeBench tasks')
        for index, raw in enumerate(pool[:count + 1]):
            item = {'id': f"bigcodebench:cpu-v1:{DATA_REVISION[:12]}:{raw['task_id'].split('/')[-1]}",
                    'benchmark': 'bigcodebench', 'stratum': stratum,
                    'messages': [{'role': 'system', 'content': RUNTIME},
                                 {'role': 'user', 'content': raw['instruct_prompt']}],
                    'entry_point': raw['entry_point'], 'test': raw['test'],
                    'code_prompt': raw['code_prompt'],
                    'reference': raw['complete_prompt'] + '\n' + raw['canonical_solution']}
            item['id'] += ':' + digest(item)[:12]
            panels['coding_pilot' if index == 0 else 'coding'].append(item)
    return panels


def build_image(runner, manifest: dict, source: Path) -> str:
    for name, hashed in manifest['grading_files'].items():
        if digest((runner.checkout / 'grading' / name).read_text()) != hashed:
            raise GradingUnavailable('Frozen coding grader changed; prepare a new panel')
    env = docker_environment(runner.settings)
    base = manifest['base_grader_image']
    tag = 'freeboard-coding-base:' + base.split(':')[1][:16]
    inspected = subprocess.run(['docker', 'image', 'inspect', base, '--format', '{{.Id}}'],
        capture_output=True, text=True, timeout=15, env=env)
    if inspected.returncode or inspected.stdout.strip() != base:
        raise GradingUnavailable('Restore the frozen base grading image first')
    subprocess.run(['docker', 'tag', base, tag], check=True, capture_output=True, timeout=15, env=env)
    with tempfile.TemporaryDirectory(dir=runner.settings.state) as folder:
        context = Path(folder)
        for name in FILES:
            shutil.copy(runner.checkout / 'grading' / name, context / name)
        shutil.copytree(source / 'bigcodebench', context / 'bigcodebench/bigcodebench')
        shutil.copy(source / 'LICENSE', context / 'bigcodebench/LICENSE')
        result = subprocess.run(['docker', 'build', '--build-arg', f'BASE_IMAGE={tag}',
            '-f', str(context / 'coding.Dockerfile'), '-t', 'freeboard-coding:' + digest(manifest)[:16], str(context)],
            capture_output=True, text=True, timeout=600, env=env)
        (runner.settings.state / 'logs/coding-grader-build.log').write_text(result.stdout + result.stderr)
        if result.returncode:
            raise GradingUnavailable('Coding image build failed; see private logs/coding-grader-build.log')
    return subprocess.check_output(['docker', 'image', 'inspect', 'freeboard-coding:' + digest(manifest)[:16],
        '--format', '{{.Id}}'], text=True, env=env, timeout=15).strip()


def coding_base(db) -> dict:
    base = db.one("""SELECT * FROM seasons WHERE validated=1
        AND json_extract(manifest,'$.counts.screen.livecodebench')=40
        ORDER BY active DESC,created_at DESC LIMIT 1""")
    if not base:
        raise RuntimeError('Validate a common grading image first: prepare-panels --public-only needs no GPQA access')
    return base


def prepare_coding(runner) -> dict:
    base = coding_base(runner.db)
    base_manifest = json.loads(base['manifest'])
    source = upstream(runner.settings, 'bigcodebench', revision=GRADER_REVISION, repo='bigcode-project/bigcodebench')
    path = download(runner.settings, 'bigcode/bigcodebench', 'data/v0.1.4-00000-of-00001.parquet', DATA_REVISION)
    panels = select_panel(pq.read_table(path).to_pylist())
    public = {tier: [{'id': i['id'], 'hash': digest(i), 'benchmark': i['benchmark'], 'stratum': i['stratum']}
                     for i in items] for tier, items in panels.items()}
    manifest = {**base_manifest, 'partial': True, 'panels': public,
        'counts': {'coding': {'bigcodebench': 24}, 'coding_pilot': {'bigcodebench': 2}},
        'sources': {**base_manifest['sources'], 'bigcodebench_data': DATA_REVISION, 'bigcodebench_grader': GRADER_REVISION},
        'base_grader_image': base_manifest['grader_image'],
        'grading_files': {**base_manifest['grading_files'],
                          **{name: digest((runner.checkout / 'grading' / name).read_text()) for name in FILES}},
        'grader_image': None,
        'protocol': {'output_cap':4096, 'temperature':0, 'tools':False, 'completions':1,
                     'panel':PANEL, 'strata': {'bigcodebench':STRATA},
                     'purpose':'CPU library-use subset; equal-weight coding expansion'}}
    ident = 'coding-' + digest(manifest)[:16]
    saved = runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))
    if saved and saved['validated']:
        runner.grader.build(json.loads(saved['manifest']), runner.checkout)
        return saved
    runner.db.execute('INSERT OR IGNORE INTO seasons VALUES(?,?,?,?,?)', (ident,now(),json.dumps(manifest),0,0))
    for tier, items in panels.items():
        for item in items:
            prior = runner.db.one('SELECT question_hash FROM items WHERE id=?', (item['id'],))
            if prior and prior['question_hash'] != digest(item):
                raise ValueError('Frozen BigCodeBench task changed')
            runner.db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)',
                (item['id'],ident,'bigcodebench',item['stratum'],digest(item),json.dumps(item)))
            runner.db.execute('INSERT OR IGNORE INTO panels VALUES(?,?,?)', (ident,tier,item['id']))
    image = build_image(runner, manifest, source)
    # Verify every selected ground truth under exactly the scored limits. Never
    # resample after a validation failure and never execute task code on the host.
    for items in panels.values():
        for item in items:
            if runner.grader.grade({**item, 'reference_check':True}, item['reference'], image) != 1:
                raise GradingUnavailable(f"Reference validation failed for {item['id']}; no model calls authorized")
    fixture = {'benchmark':'bigcodebench', 'entry_point':'task_func',
               'test':'import unittest\nclass TestCases(unittest.TestCase):\n def test_value(self): self.assertEqual(task_func(2),4)\n'}
    if runner.grader.grade(fixture,'def task_func(x): return x*2',image) != 1:
        raise GradingUnavailable('Coding extraction/correct-answer fixture failed')
    for answer in ['def task_func(x): return 0', 'def task_func(x):\n while True: pass']:
        if runner.grader.grade(fixture, answer, image) != 0:
            raise GradingUnavailable('Coding wrong-answer/timeout fixture failed')
    manifest['grader_image'] = image
    runner.db.execute('UPDATE seasons SET manifest=?,validated=1 WHERE id=?', (json.dumps(manifest),ident))
    return runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))


def plan_coding_capacity(runner, season: dict, models: int) -> None:
    reserve = [estimate(json.loads(row['content'])['messages'],4096) for row in runner.db.rows(
        'SELECT i.content FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=?',(season['id'],))] * models
    used = runner.budget.summary()
    planned = runner.budget.plan(reserve,0,2)
    runner.db.execute('UPDATE budgets SET attempts_limit=MAX(attempts_limit,?),tokens_limit=MAX(tokens_limit,?) WHERE week=?',
        (used['attempts_used']+planned['planned_attempts'],used['accounted_tokens']+planned['planned_tokens'],week()))


def run_coding(runner, season: dict, selected: list[str] | None = None, limit: int | None = None) -> dict:
    runner.grader.build(json.loads(season['manifest']),runner.checkout)
    clean = subprocess.run(['git','status','--porcelain','--untracked-files=all','--','src','grading'],
        cwd=runner.checkout,capture_output=True,text=True,timeout=15)
    if clean.returncode or clean.stdout.strip():
        raise RuntimeError('Commit runner and grader changes before coding model requests')
    runner.db.recover()
    # Eligibility calls themselves consume attempts, so scale the new session
    # before discovery even when the prior week's allocation is fully spent.
    plan_coding_capacity(runner,season,len(set(selected)) if selected else max(1,len(runner.models())))
    evidence = discover(runner.db, runner.budget, runner.client)
    if not evidence['ok']:
        return {'blocked':evidence['error']}
    proof = json.loads(runner.db.one('SELECT evidence FROM discovery ORDER BY id DESC LIMIT 1')['evidence'])
    available = {m['id']:m for m in runner.db.rows('SELECT * FROM models ORDER BY id')
        if m['id'] in proof['catalog_ids'] and proof['pricing'].get(m['id'],{}).get('free')
        and m['status'] not in {'excluded','paid','removed','unsupported'}
        and json.loads(m['profile']).get('cap_verified')
        and runner.db.one("SELECT id FROM cycles WHERE model_id=? AND epoch=? AND kind='pilot' AND completed_at IS NOT NULL",
                          (m['id'],m['epoch']))}
    selected = sorted(set(selected)) if selected else sorted(available)
    if not selected:
        return {'blocked':'No currently accessible free model has a completed matching compatibility pilot',
                'season':season['id'],'processed':0}
    if any(model not in available for model in selected):
        raise ValueError('Coding expansion requires current free eligibility and a completed matching compatibility pilot')
    # Headline work has priority; this command never spends its capacity on an
    # expansion while eligible common-screen generations await execution.
    if runner.db.one("""SELECT j.id FROM jobs j JOIN cycles c ON c.id=j.cycle_id
        JOIN models m ON m.id=c.model_id AND m.epoch=c.epoch WHERE c.kind IN ('screen','public_screen')
        AND m.status='eligible' AND j.status IN ('pending','deferred') LIMIT 1"""):
        return {'blocked':'Finish scheduled common screens before coding expansion'}
    cycles = []
    for model_id in selected:
        model = available[model_id]
        pilot = runner.cycle(model, season, 'coding_pilot', 'coding-pilot:' + digest([model_id,model['epoch'],season['id']])[:24])
        runner.add_panel(pilot, 'coding_pilot')
        runner.finish_cycle(pilot['id'])
        cycles.append((model,pilot))
    # Plan both the held-out checks and the future screen before dispatch.
    plan_coding_capacity(runner,season,len(selected))
    processed = runner.drain(season, 'coding_pilot', limit, set(selected))['processed']
    for model, pilot in cycles:
        if runner.db.one('SELECT completed_at FROM cycles WHERE id=?',(pilot['id'],))['completed_at']:
            cycle = runner.db.one("SELECT * FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind='coding' ORDER BY started_at DESC LIMIT 1",
                                 (model['id'],model['epoch'],season['id']))
            if not cycle or cycle['completed_at'] and (datetime.now(timezone.utc)-datetime.fromisoformat(cycle['started_at'])).days>=28:
                cycle = runner.cycle(model, season, 'coding', 'coding:' + digest([model['id'],model['epoch'],season['id'],week()])[:24])
            runner.add_panel(cycle,'coding')
            runner.finish_cycle(cycle['id'])
    if limit is None or processed < limit:
        processed += runner.drain(season,'coding',None if limit is None else limit-processed,set(selected))['processed']
    from .scoring import snapshot
    report = snapshot(runner.db,runner.settings)
    spec = coding_spec(json.loads(season['manifest']))
    if (report.coding_weights.get(spec['benchmark']) == 0.5 and any(
            c.category=='coding' and c.score is not None and c.season==season['id'] for c in report.capabilities)):
        manifest = json.loads(runner.db.one('SELECT manifest FROM seasons WHERE id=?',(season['id'],))['manifest'])
        if not manifest.get('coding_promoted_at'):
            manifest['coding_promoted_at'] = now()
            runner.db.execute('UPDATE seasons SET manifest=? WHERE id=?',(json.dumps(manifest),season['id']))
    return {'season':season['id'],'models':selected,'questions_per_model':24,
            'held_out_per_model':len(spec['strata']), 'processed':processed,
            'blocked': None if any(m['status']=='eligible' for m in available.values()) else
                'OpenCode generation access is restricted; validated panels and queued jobs are retained',
            'budget':runner.budget.summary()}
