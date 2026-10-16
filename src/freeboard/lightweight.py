"""A frozen, public 12-question panel, separate from headline screens."""
from __future__ import annotations

import json

from .budget import estimate
from .config import SEED, digest, now, week
from .discovery import discover

STRATA = {'livebench': {'spatial': 3, 'zebra_puzzle': 3},
          'livecodebench': {'easy': 2, 'medium': 2, 'hard': 2}}


def prepare_lightweight(runner) -> dict:
    source = runner.db.one("""SELECT * FROM seasons WHERE validated=1
        AND json_extract(manifest,'$.partial')=1 AND id LIKE 'public-pilot-%'
        ORDER BY created_at DESC LIMIT 1""")
    if not source:
        raise ValueError('Prepare and validate the public benchmark season first')
    manifest = json.loads(source['manifest'])
    candidates = runner.db.rows("""SELECT i.id,i.benchmark,i.stratum,i.question_hash FROM panels p
        JOIN items i ON i.id=p.item_id WHERE p.season=? AND p.tier='screen'""", (source['id'],))
    selected = []
    for benchmark, strata in STRATA.items():
        for stratum, count in strata.items():
            pool = sorted((i for i in candidates if i['benchmark'] == benchmark and i['stratum'] == stratum),
                          key=lambda i: digest([SEED, 'lightweight-v1', i['id']]))
            if len(pool) < count:
                raise ValueError(f'Insufficient frozen questions for {benchmark}/{stratum}')
            selected.extend(pool[:count])
    panel = [{'id': i['id'], 'hash': i['question_hash'], 'benchmark': i['benchmark'], 'stratum': i['stratum']}
             for i in selected]
    ident = 'lightweight-' + digest([source['id'], panel, STRATA])[:16]
    manifest.update({'panels': {'lightweight': panel},
                     'counts': {'lightweight': {b: sum(s.values()) for b, s in STRATA.items()}},
                     'partial': True,
                     'protocol': {**manifest.get('protocol', {}), 'panel': 'lightweight-v1',
                                  'source_season': source['id'], 'strata': STRATA,
                                  'purpose': 'Public subset; no headline or overall ranking'}})
    runner.db.execute('INSERT OR IGNORE INTO seasons VALUES(?,?,?,?,?)',
                      (ident, now(), json.dumps(manifest), 1, 0))
    for item in selected:
        runner.db.execute('INSERT OR IGNORE INTO panels VALUES(?,?,?)', (ident, 'lightweight', item['id']))
    return runner.db.one('SELECT * FROM seasons WHERE id=?', (ident,))


def run_lightweight(runner, selected: list[str] | None = None, limit: int | None = None) -> dict:
    # Prior discovery alone does not count as evaluation. Client/endpoint
    # updates alone must not turn previously evaluated IDs into new models.
    known = {r['model_id'] for r in runner.db.rows('SELECT DISTINCT model_id FROM attempts WHERE job_id IS NOT NULL')}
    # Existing runs remain resumable after the first invocation discovered them.
    unfinished = {r['model_id'] for r in runner.db.rows("SELECT model_id FROM cycles WHERE kind='lightweight' AND completed_at IS NULL")}
    runner.db.recover()
    evidence = discover(runner.db, runner.budget, runner.client)
    if not evidence['ok']:
        return {'blocked': evidence['error']}
    available = {m['id']: m for m in runner.models()}
    selected = sorted(set(selected)) if selected else sorted((set(available) - known) | (unfinished & set(available)))
    if any(i not in available for i in selected):
        raise ValueError('Lightweight runs require currently verified free, supported models')
    if not selected:
        return {'models': [], 'processed': 0, 'message': 'No new or unfinished eligible models'}
    season = prepare_lightweight(runner)
    # Validate the frozen image and local grader sources before dispatching.
    runner.grader.build(json.loads(season['manifest']), runner.checkout)
    for ident in selected:
        model = available[ident]
        runner.health(model, season)
        cycle = runner.cycle(model, season, 'lightweight',
                             'lightweight:' + digest([ident, model['epoch'], season['id']])[:24])
        runner.add_panel(cycle, 'lightweight')
        # A crash after the final grade must not require another generation
        # merely to recover the cycle's completion marker.
        runner.finish_cycle(cycle['id'])
    jobs = runner.db.rows("""SELECT i.content,i.benchmark FROM jobs j JOIN items i ON i.id=j.item_id
        JOIN cycles c ON c.id=j.cycle_id JOIN models m ON m.id=c.model_id AND m.epoch=c.epoch
        WHERE c.season=? AND m.status='eligible' AND j.status IN ('pending','deferred')""", (season['id'],))
    used = runner.budget.summary()
    planned = runner.budget.plan([estimate(json.loads(j['content'])['messages'],
                                           256 if j['benchmark'] == 'health' else 4096) for j in jobs], 0)
    runner.db.execute('UPDATE budgets SET attempts_limit=MAX(attempts_limit,?),tokens_limit=MAX(tokens_limit,?) WHERE week=?',
                      (used['attempts_used'] + planned['planned_attempts'], used['accounted_tokens'] + planned['planned_tokens'], week()))
    try:
        result = runner.drain(season, 'lightweight', limit, set(selected))
        return {'models': selected, 'season': season['id'], 'questions_per_model': 12, 'probes_per_model': 6, **result}
    finally:
        runner.opencode.close()
