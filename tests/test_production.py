"""Offline regressions for release and durable-state boundaries."""
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_core as fixtures
from freeboard.config import Settings, now
from freeboard.discovery import parse_evidence
from freeboard.publication import ensure_pages, export
from freeboard.scoring import make_row, merged_capabilities

context = fixtures.context


@pytest.mark.parametrize('blocked', ['authentication_failed', 'client_access_restricted'])
def test_catalog_refresh_preserves_access_rejection_until_explicit_recheck(context, monkeypatch, blocked):
    import httpx
    from freeboard.budget import Budget
    from freeboard.config import ZEN_BASE, ZEN_DOCS
    from freeboard.discovery import discover
    db, settings, _ = context
    monkeypatch.setattr('freeboard.opencode.client_version', lambda _: 'fixture')
    definition = {'api': {'id':'alias','url':ZEN_BASE},'providerID':'opencode',
        'cost':{'input':0,'output':0},'capabilities':{'reasoning':True},'options':{},
        'variants':{'high':{'reasoningEffort':'high'}}}
    monkeypatch.setattr('freeboard.opencode.inspect_models', lambda *_: {'alias':definition})
    monkeypatch.setattr('freeboard.discovery.credential', lambda _: None)
    price = ['Free']
    def source(request):
        html = f'<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr><tr><td>Alias</td><td>alias</td><td>{ZEN_BASE}/chat/completions</td></tr></table><table><tr><th>Model</th><th>Input</th><th>Output</th></tr><tr><td>Alias</td><td>{price[0]}</td><td>{price[0]}</td></tr></table>'
        return httpx.Response(200,text=html) if str(request.url)==ZEN_DOCS else httpx.Response(200,json={'data':[{'id':'alias'}]})
    with httpx.Client(transport=httpx.MockTransport(source)) as client:
        budget = Budget(db,settings)
        discover(db,budget,client)
        epoch = db.one('SELECT epoch FROM models')['epoch']
        db.execute('UPDATE models SET status=?',(blocked,))
        for catalog_only in [False,True]:
            discover(db,budget,client,catalog_only=catalog_only)
            assert db.one('SELECT status FROM models')['status']==blocked
        discover(db,budget,client,recheck_access=('alias',))
        assert db.one('SELECT status,epoch FROM models')=={'status':'eligible','epoch':epoch}
        evidence = json.loads(db.one('SELECT evidence FROM discovery ORDER BY id DESC LIMIT 1')['evidence'])
        assert evidence['recheck_access']==['alias']
        price[0]='$1.00'
        discover(db,budget,client,recheck_access=('alias',))
        assert db.one('SELECT status FROM models')['status']=='paid'
        assert all(a['kind']=='discovery' for a in db.rows('SELECT kind FROM attempts'))


def test_coding_pilot_progress_is_visible_without_a_provisional_score(context):
    from freeboard.scoring import coding_results
    runner, _, model, _ = fixtures.job_context(context)
    manifest = json.loads(runner.db.one('SELECT manifest FROM seasons')['manifest'])
    manifest['protocol']={'panel':'bigcodebench-cpu-v1'}
    runner.db.execute('UPDATE seasons SET manifest=?',(json.dumps(manifest),))
    season = runner.db.one('SELECT * FROM seasons')
    pilot = runner.cycle(model,season,'coding_pilot','coding-pilot-fixture')
    for index in range(2):
        fixtures.add_item(runner.db,ident=f'bcb-pilot-{index}',tier='coding_pilot',benchmark='bigcodebench',
                          content={'benchmark':'bigcodebench','messages':[{'role':'user','content':'Synthetic fixture.'}]})
    runner.add_panel(pilot,'coding_pilot')
    runner.db.execute("UPDATE jobs SET status='graded',score=1 WHERE item_id='bcb-pilot-0'")
    row = coding_results(runner.db,runner.settings)[0]
    assert row.phase=='pilot' and row.pilot_progress==1 and row.progress==0
    assert row.status=='pending' and row.score is None and row.interval is None
    runner.cycle(model,season,'coding','coding-screen-fixture')
    rows = coding_results(runner.db,runner.settings)
    assert len(rows)==1 and rows[0].phase=='screen' and rows[0].score is None
    runner.close()


def test_composite_coding_export_contains_both_components_and_dates(context, tmp_path):
    import csv
    from freeboard.publication import render_site,validate_site
    from freeboard.scoring import CapabilityResult,snapshot
    runner, _, model, _ = fixtures.job_context(context)
    report=snapshot(runner.db,runner.settings)
    dates={'livecodebench':'2026-10-15T18:00:00+00:00','bigcodebench':'2026-10-16T18:00:00+00:00'}
    composite=CapabilityResult(model_id=model['id'],epoch=model['epoch'],category='coding',score=75,
        source_tier='coding-expanded',sample_counts={'livecodebench':40,'bigcodebench':24},
        benchmark_scores={'livecodebench':50,'bigcodebench':100},
        benchmark_weights={'livecodebench':.5,'bigcodebench':.5},benchmark_evaluated_at=dates)
    report=report.model_copy(update={'capabilities':[composite],'coding_weights':composite.benchmark_weights})
    target=tmp_path/'preview'
    render_site(report,runner.checkout,target)
    assert validate_site(target).capabilities[0]==composite
    with (target/'leaderboard.csv').open() as handle:
        row=next(r for r in csv.DictReader(handle) if r['tier']=='capability')
    assert float(row['coding'])==75 and float(row['bigcodebench'])==100
    assert row['sample_n']=='64' and row['bigcodebench_n']=='24'
    assert row['basis']=='LiveCodeBench + BigCodeBench'
    assert all(row[f'{b}_evaluated_at']==date for b,date in dates.items())
    runner.close()


def test_conflicting_official_endpoint_rows_block_free_eligibility():
    rows = ('<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr>'
            '<tr><td>Alias</td><td>alias-free</td><td>/chat/completions</td></tr>'
            '<tr><td>Alias</td><td>alias-free</td><td>/responses</td></tr></table>'
            '<table><tr><th>Output</th><th>Model</th><th>Input</th></tr>'
            '<tr><td>Free</td><td>Alias</td><td>Free</td></tr></table>')
    proof = parse_evidence(rows)['alias-free']
    assert proof['ambiguous'] and not proof['free']


@pytest.mark.parametrize('total', [-1, 1, '30', True])
def test_inconsistent_native_usage_cannot_enter_scores(context, monkeypatch, total):
    runner, job, model, item = fixtures.job_context(context)
    response = fixtures.native_result()
    response['parts'][-1]['tokens']['total'] = total
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: response)
    try:
        runner.generate(job, model, item, 4096)
        saved = runner.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
        assert saved['status'] == 'ambiguous' and saved['score'] is None
        assert Path(saved['response_path']).exists()
        attempt = runner.db.one('SELECT * FROM attempts')
        assert attempt['reported_tokens'] is None and attempt['accounted_tokens'] == attempt['reserved_tokens']
    finally:
        runner.close()


def test_private_client_is_independent_of_interactive_upgrade(tmp_path, monkeypatch):
    from freeboard.opencode import VERSION, client_binary, client_version
    pinned = tmp_path / 'bin' / f'opencode-{VERSION}'
    pinned.parent.mkdir()
    pinned.touch()
    monkeypatch.setattr('freeboard.opencode.shutil.which', lambda _: '/interactive/opencode-v2')
    calls = []
    def version(args, **_):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, VERSION, '')
    monkeypatch.setattr('freeboard.opencode.subprocess.run', version)
    assert client_binary(tmp_path) == str(pinned)
    assert client_version(tmp_path) == VERSION and calls[0][0] == str(pinned)


def test_disk_pressure_blocks_generation_before_reservation(context, monkeypatch):
    runner, job, model, item = fixtures.job_context(context)
    monkeypatch.setattr('freeboard.runner.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: pytest.fail('Request dispatched'))
    try:
        with pytest.raises(RuntimeError, match='no generation dispatched'):
            runner.generate(job, model, item, 4096)
        assert not runner.db.rows('SELECT * FROM attempts')
        assert runner.db.one('SELECT status FROM jobs')['status'] == 'pending'
    finally:
        runner.close()


@pytest.mark.parametrize('explicit', [False, True])
def test_private_docker_engine_respects_explicit_configuration(tmp_path, monkeypatch, explicit):
    from freeboard.grading import docker_environment
    settings = Settings(state=tmp_path)
    path = tmp_path / 'colima/freeboard/docker.sock'
    monkeypatch.setattr(Path, 'is_socket', lambda self: self == path)
    for key in ['DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_CONFIG']:
        monkeypatch.delenv(key, raising=False)
    if explicit:
        monkeypatch.setenv('DOCKER_HOST', 'unix:///explicit.sock')
    env = docker_environment(settings)
    assert env['DOCKER_HOST'] == ('unix:///explicit.sock' if explicit else f'unix://{path}')


@pytest.mark.parametrize('failure', ['disk', 'sync'])
def test_atomic_backup_failure_keeps_previous_copy(context, monkeypatch, failure):
    db, settings, _ = context
    db.backup()
    target = settings.state / 'backups' / f'{now()[:10]}.sqlite3'
    previous = target.read_bytes()
    db.execute('INSERT INTO discovery(observed_at,week,ok,evidence) VALUES(?,?,?,?)',
               (now(), 'fixture', 1, '{}'))
    if failure == 'disk':
        monkeypatch.setattr('freeboard.db.shutil.disk_usage', lambda _: SimpleNamespace(free=0))
        error = RuntimeError
    else:
        def fail_sync(_):
            raise OSError('fixture: disk write failed')
        monkeypatch.setattr('freeboard.db.os.fsync', fail_sync)
        error = OSError
    with pytest.raises(error):
        db.backup()
    assert target.read_bytes() == previous
    assert not list(target.parent.glob('.backup-*'))
    assert db.one('SELECT COUNT(*) n FROM discovery')['n'] == 1


@pytest.mark.parametrize('status', [401, 403, 429, 500, 502])
def test_pages_errors_never_create_or_change_configuration(context, monkeypatch, status):
    _, settings, checkout = context
    calls = []
    def run(args, **_):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, json.dumps({'status': str(status)}), '')
    monkeypatch.setattr('freeboard.publication.subprocess.run', run)
    with pytest.raises(RuntimeError, match='verification failed'):
        ensure_pages(settings, checkout)
    assert len(calls) == 1 and '--method' not in calls[0]


@pytest.mark.parametrize('failure', ['timeout', 'invalid'])
def test_pages_unknown_response_preserves_configuration(context, monkeypatch, failure):
    _, settings, checkout = context
    def run(args, **_):
        if failure == 'timeout':
            raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(args, 1, 'not JSON', '')
    monkeypatch.setattr('freeboard.publication.subprocess.run', run)
    with pytest.raises(RuntimeError, match='Cannot verify'):
        ensure_pages(settings, checkout)


def test_pages_created_only_on_explicit_not_found(context, monkeypatch):
    _, settings, checkout = context
    monkeypatch.setattr('freeboard.publication.subprocess.run', lambda args, **_:
                        subprocess.CompletedProcess(args, 1, '{"status":"404"}', ''))
    created = []
    monkeypatch.setattr('freeboard.publication.command', lambda args, cwd: created.append(args))
    ensure_pages(settings, checkout)
    assert len(created) == 1 and created[0][3] == 'POST'


@pytest.mark.parametrize('partial', ['text', 'reasoning', 'tool', 'usage', 'multi_turn'])
def test_provider_error_after_partial_response_is_never_replayable(context, monkeypatch, partial):
    runner, job, model, item = fixtures.job_context(context)
    response = fixtures.native_error(429, 'Rate limited after streaming')
    response['parts'] = []
    if partial in {'text', 'reasoning', 'tool'}:
        response['parts'] = [{'type': partial, 'text': 'unfinished'}]
    elif partial == 'usage':
        response['info']['tokens'] = {'input': 10, 'output': 0, 'reasoning': 0}
    else:
        response['assistant_turns'] = 2
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: response)
    try:
        runner.generate(job, model, item, 4096)
        saved = runner.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
        assert saved['status'] == 'ambiguous' and saved['score'] is None
        assert Path(saved['response_path']).is_file()
        attempt = runner.db.one('SELECT * FROM attempts')
        assert attempt['accounted_tokens'] == attempt['reserved_tokens']
    finally:
        runner.close()


def test_completed_screen_survives_pending_refresh_with_original_date(context):
    base, _, _ = fixtures.capability_sources(context)
    date = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    complete = base.model_copy(update={'season': 'full', 'status': 'stale', 'evaluated_at': date,
        'cycle_started_at': date, 'progress': {'gpqa': 20, 'livebench': 20, 'livecodebench': 40},
        'scores': {'gpqa': 80, 'livebench': 40, 'livecodebench': 50},
        'intervals': {'reasoning': [45, 75], 'coding': [35, 65]}})
    pending = base.model_copy(update={'season': 'full', 'status': 'pending'})
    rows = merged_capabilities([pending], [], [], [complete])
    assert [r.score for r in rows] == [60, 50]
    assert all(r.stale and r.refresh_pending and r.evaluated_at == date and r.rank == 1 for r in rows)
    for changed in [{'season': 'new-season'}, {'epoch': 'new-epoch'}, {'cap_verified': False}]:
        assert all(r.score is None for r in merged_capabilities([pending.model_copy(update=changed)], [], [], [complete]))


def test_public_screen_refresh_creates_new_cycle_and_retains_completed_score(context):
    from freeboard.public_run import public_cycle
    runner, model, season = fixtures.public_screen_fixture(context)
    old = public_cycle(runner, model, season)
    assert public_cycle(runner, model, season)['id'] == old['id']
    date = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
    runner.db.execute('UPDATE cycles SET started_at=? WHERE id=?', (date, old['id']))
    fresh = public_cycle(runner, model, season)
    assert fresh['id'] != old['id'] and runner.db.one('SELECT COUNT(*) n FROM cycles')['n'] == 2
    assert public_cycle(runner, model, season)['id'] == fresh['id']
    runner.close()


def test_pending_public_refresh_retains_completed_score(context):
    base, public, _ = fixtures.capability_sources(context)
    date = (datetime.now(timezone.utc) - timedelta(days=29)).isoformat()
    complete = public.model_copy(update={'evaluated_at': date, 'cycle_started_at': date})
    pending = public.model_copy(update={'status': 'pending', 'cycle_started_at': now(), 'benchmark_scores': {}})
    rows = merged_capabilities([base], [complete, pending], [])
    assert [r.score for r in rows] == [30, 70]
    assert all(r.stale and r.refresh_pending and r.evaluated_at == date for r in rows)


def test_access_diagnostic_cannot_mask_revoked_cap_on_existing_cycle(context):
    runner, _, model, _ = fixtures.job_context(context)
    profile = {**json.loads(model['profile']), 'cap_verified': False}
    runner.db.execute("UPDATE models SET status='cap_violation',profile=?", (json.dumps(profile),))
    attempt = runner.budget.reserve('opencode_access_diagnostic', 100, model_id=model['id'])
    runner.budget.finish(attempt, 'client_access_restricted', 403)
    current = runner.db.one('SELECT * FROM models')
    cycle = runner.db.one('SELECT * FROM cycles')
    row, _ = make_row(runner.db, runner.settings, current, cycle, 'screen')
    assert row.availability == 'client_access_restricted' and not row.cap_verified
    runner.close()


def test_private_manifest_extension_cannot_enter_public_export(context, tmp_path):
    db, settings, checkout = context
    fixtures.model(db)
    fixtures.season(db)
    target = tmp_path / 'public'
    shutil.copytree(checkout / 'web', target / 'web')
    export(db, settings, target)
    previous = (target / 'site/snapshot.json').read_bytes()
    manifest = json.loads(db.one('SELECT manifest FROM seasons')['manifest'])
    manifest['protocol'] = {'prompt': 'PRIVATE_SENTINEL'}
    db.execute('UPDATE seasons SET manifest=?', (json.dumps(manifest),))
    with pytest.raises(ValueError):
        export(db, settings, target)
    assert (target / 'site/snapshot.json').read_bytes() == previous


def test_failed_schedule_preparation_never_stops_existing_agent(tmp_path, monkeypatch):
    from freeboard.scheduling import install
    monkeypatch.setattr('freeboard.scheduling.sys.platform', 'darwin')
    state, home = tmp_path / 'state', tmp_path / 'home'
    binary = state / 'runtime/bin/python'
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setattr(Path, 'home', lambda: home)
    def fail(_):
        raise RuntimeError('fixture: fetch failed')
    monkeypatch.setattr('freeboard.scheduling.scheduler_checkout', fail)
    monkeypatch.setattr('freeboard.scheduling.subprocess.run', lambda *_a, **_k: pytest.fail('Existing agent stopped'))
    with pytest.raises(RuntimeError, match='fetch failed'):
        install(Settings(state=state), tmp_path)


def test_failed_schedule_bootstrap_restores_previous_agent(tmp_path, monkeypatch):
    from freeboard.scheduling import install
    monkeypatch.setattr('freeboard.scheduling.sys.platform', 'darwin')
    state, home = tmp_path / 'state', tmp_path / 'home'
    binary = state / 'runtime/bin/python'
    binary.parent.mkdir(parents=True)
    binary.touch()
    plist = home / 'Library/LaunchAgents/ai.opencode.free-leaderboard.plist'
    plist.parent.mkdir(parents=True)
    plist.write_bytes(b'previous schedule fixture')
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr('freeboard.scheduling.scheduler_checkout', lambda _: tmp_path)
    commands = []
    def launch(args, **_):
        commands.append(args)
        return subprocess.CompletedProcess(args, 1 if len(commands) == 2 else 0)
    monkeypatch.setattr('freeboard.scheduling.subprocess.run', launch)
    with pytest.raises(RuntimeError, match='previous schedule restored'):
        install(Settings(state=state), tmp_path)
    assert plist.read_bytes() == b'previous schedule fixture'
    assert [args[1] for args in commands] == ['bootout', 'bootstrap', 'bootstrap']


def test_daily_keeps_public_progress_publishable_when_client_is_blocked(context, monkeypatch):
    from freeboard.cli import daily_work
    db, _, _ = context
    fixtures.model(db)
    season = fixtures.season(db)
    manifest = {**json.loads(season['manifest']), 'partial': True}
    db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)', ('public', now(), json.dumps(manifest), 1, 0))
    runner = fixtures.Runner(db, context[1], context[2])
    def blocked(**_):
        raise RuntimeError('Validated client required')
    monkeypatch.setattr(runner, 'run', blocked)
    result = daily_work(runner)
    assert result['blocked'] == 'Validated client required'
    assert result['pilot']['blocked'] == 'Validated client required'
    runner.close()
