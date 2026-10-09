"""Finish the frozen public screen without admitting it to headline rankings."""
from __future__ import annotations

import json

from .budget import estimate
from .config import COUNTS, digest, week
from .discovery import discover


def validate_public_screen(runner, season: dict) -> None:
    manifest = json.loads(season['manifest'])
    if not season['validated'] or not manifest.get('partial'):
        raise ValueError('Public screens require a validated partial season')
    panel = runner.db.rows('SELECT i.benchmark FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=? AND p.tier=?',
                           (season['id'], 'screen'))
    counts = {b: sum(i['benchmark'] == b for i in panel) for b in COUNTS['screen']}
    if counts != {'gpqa': 0, 'livebench': 20, 'livecodebench': 40} or len(panel) != 60:
        raise ValueError('Frozen public screen must contain exactly 20 LiveBench and 40 LiveCodeBench questions')


def plan_public_screen(runner, cycle: dict) -> dict:
    items = runner.db.rows("""SELECT i.content FROM jobs j JOIN items i ON i.id=j.item_id JOIN cycles c ON c.id=j.cycle_id
        WHERE c.season=? AND c.kind='public_screen' AND j.status IN ('pending','deferred')
        AND json_extract(c.profile,'$.transport')='local-opencode'
        AND json_extract(c.profile,'$.protocol_revision')=3""",
                           (cycle['season'],))
    estimates = [estimate(json.loads(i['content'])['messages'], 4096) for i in items]
    used = runner.budget.summary()
    planned = runner.budget.plan(estimates, 0)
    runner.db.execute('UPDATE budgets SET attempts_limit=MAX(attempts_limit,?),tokens_limit=MAX(tokens_limit,?) WHERE week=?',
                      (used['attempts_used'] + planned['planned_attempts'], used['accounted_tokens'] + planned['planned_tokens'], week()))
    return runner.budget.summary()


def run_public(runner, season: dict, selected: list[str] | None = None, limit: int | None = None) -> dict:
    validate_public_screen(runner, season)
    runner.db.recover()
    evidence = discover(runner.db, runner.budget, runner.client)
    if not evidence['ok']:
        return {'blocked': evidence['error']}
    available = {m['id']: m for m in runner.models()}
    runnable = {ident: m for ident, m in available.items() if json.loads(m['profile']).get('cap_verified')
                and runner.db.one("SELECT id FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind='pilot' AND completed_at IS NOT NULL",
                                  (ident, m['epoch'], season['id']))}
    selected = sorted(set(selected)) if selected else sorted(runnable)
    if not selected:
        return {'blocked': 'No free model has a completed public pilot and verified output cap'}
    if any(ident not in runnable for ident in selected):
        raise ValueError('Public screen requires current free eligibility, a verified cap, and a completed matching pilot')
    for ident in selected:
        m = runnable[ident]
        cycle_id = 'public_screen:' + digest([ident, m['epoch'], season['id']])[:24]
        cycle = runner.cycle(m, season, 'public_screen', cycle_id)
        runner.add_panel(cycle, 'screen')
        plan_public_screen(runner, cycle)
    result = runner.drain(season, 'public_screen', limit, set(selected))
    return {'unranked': True, 'panel': 'public-screen', 'models': selected, **result}
