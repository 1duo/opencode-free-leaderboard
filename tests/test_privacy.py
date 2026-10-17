import json

import pytest

from freeboard.discovery import parse_evidence
from freeboard.privacy import gpqa_allowed
import test_core as fixtures

context = fixtures.context


@pytest.mark.parametrize('statements,allowed', [
    ('<li>Alias Free is free. Its provider does not use your data for model training.</li>', True),
    ('<li>Alias Free: During its free period, collected data may be used to improve the model.</li>', False),
    ('<li>Alias Free (trial): Do not submit confidential data. Logs improve NVIDIA products.</li>', False),
    ('<li>Another Alias Free does not use your data for model training.</li>', False),
    ('', False),
    ('<li>Alias Free does not use your data for model training.</li>'
     '<li>Alias Free: Prompts and other inputs are not used for training. Retained for 30 days.</li>', True),
    ('<li>Alias Free does not use your data for model training.</li>'
     '<li>Alias Free: Data may support service optimization.</li>', False),
    ('<li>Alias Free does not use your data for model training.</li>'
     '<li>Alias Free: collected data may be used to improve the model.</li>', False),
])
def test_exact_current_no_training_evidence(statements, allowed):
    html = ('<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>'
            '<tr><td>Alias Free</td><td>alias-free</td><td>/chat/completions</td></tr></table>'
            '<table><tr><th>Model</th><th>Input</th><th>Output</th></tr>'
            '<tr><td>Alias Free</td><td>Free</td><td>Free</td></tr></table>')
    proof = parse_evidence(html + statements)['alias-free']
    assert proof['free'] and proof['gpqa_allowed'] is allowed


def test_gpqa_guard_blocks_before_reservation_or_dispatch(context, monkeypatch):
    runner, job, model, item = fixtures.job_context(context)
    assert gpqa_allowed(runner.db, model)
    proof = {'pricing': {model['id']: {'free': True, 'gpqa_allowed': False}}}
    runner.db.execute('UPDATE discovery SET evidence=? WHERE id=?', (json.dumps(proof), model['evidence_id']))
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: pytest.fail('Private question dispatched'))
    with pytest.raises(ValueError, match='privacy evidence'):
        runner.generate(job, model, item, 4096)
    assert not runner.db.rows('SELECT * FROM attempts')
    assert runner.db.one('SELECT * FROM jobs')['status'] == 'pending'
    runner.schedule(runner.active_season())
    assert not runner.db.rows("SELECT * FROM cycles WHERE id!='cycle'")


def test_missing_or_failed_privacy_verification_blocks_gpqa(context):
    runner, _, model, _ = fixtures.job_context(context)
    runner.db.execute('UPDATE discovery SET ok=0 WHERE id=?', (model['evidence_id'],))
    assert not gpqa_allowed(runner.db, model)
    model['evidence_id'] = None
    assert not gpqa_allowed(runner.db, model)


def test_daily_routes_restricted_models_to_public_panel(context, monkeypatch):
    from freeboard.cli import daily_work
    from freeboard.runner import Runner
    from freeboard.config import now
    db, settings, checkout = context
    safe = fixtures.model(db, 'safe-free')
    restricted = fixtures.model(db, 'restricted-free')
    db.execute('UPDATE discovery SET evidence=? WHERE id=?',
               (json.dumps({'pricing': {'restricted-free': {'free': True, 'gpqa_allowed': False}}}), restricted['evidence_id']))
    full = fixtures.season(db)
    manifest = {**json.loads(full['manifest']), 'partial': True}
    db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)', ('public', now(), json.dumps(manifest), 1, 0))
    public = db.one("SELECT * FROM seasons WHERE id='public'")
    runner = Runner(db, settings, checkout)
    for model, panel, ident in [(safe, full, 'safe-pilot'), (restricted, public, 'restricted-pilot')]:
        runner.cycle(model, panel, 'pilot', ident)
        db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), ident))
    calls = []
    monkeypatch.setattr(runner, 'run', lambda **kwargs: calls.append((runner.active_season()['id'], kwargs)) or {'processed': 0})
    monkeypatch.setattr('freeboard.public_run.run_public', lambda runner, panel, selected:
                        calls.append((panel['id'], selected)) or {'processed': 0})
    daily_work(runner)
    assert calls == [('season', {}), ('public', {'pilot': True, 'pilot_models': ['restricted-free']}),
                     ('public', ['restricted-free'])]


def test_public_capacity_excludes_archived_and_blocked_cycles(context):
    from freeboard.public_run import plan_public_screen
    from freeboard.runner import Runner
    db, settings, checkout = context
    model = fixtures.model(db)
    panel = fixtures.season(db)
    fixtures.add_item(db, benchmark='livebench')
    runner = Runner(db, settings, checkout)
    current = runner.cycle(model, panel, 'public_screen', 'current')
    old = runner.cycle({**model, 'epoch': 'archived'}, panel, 'public_screen', 'archived')
    blocked_model = fixtures.model(db, 'blocked-free')
    db.execute("UPDATE models SET status='cap_violation' WHERE id='blocked-free'")
    blocked = runner.cycle(blocked_model, panel, 'public_screen', 'blocked')
    for cycle in [current, old, blocked]:
        runner.add_panel(cycle, 'screen')
    # One current question plus two metadata calls, with 25% retry headroom.
    assert plan_public_screen(runner, current)['planned_attempts'] == 4
