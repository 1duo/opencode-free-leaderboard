"""Clone, portable setup and sanitized result-PR regressions; no inference."""
import io
import json
import shutil
import subprocess
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import test_core as fixtures
from freeboard.config import COUNTS, PINS, Settings, digest
from freeboard.publication import build_site, public_source, render_site, validate_site, with_community
from freeboard.scoring import PublicManifest, snapshot
from freeboard.submissions import ResultSubmission, export_submission, fingerprint, load_submissions, validate_pr_submissions, validate_submission

context = fixtures.context


def submission_fixture():
    items = []
    for benchmark, count in COUNTS['screen'].items():
        for i in range(count):
            stratum = {'gpqa': ['biology', 'chemistry', 'physics'], 'livebench': ['spatial', 'zebra_puzzle'],
                       'livecodebench': ['easy', 'medium', 'hard']}[benchmark][i % (3 if benchmark != 'livebench' else 2)]
            ident = f'{benchmark}:fixture-{i}'
            items.append({'id': ident, 'hash': digest(ident), 'benchmark': benchmark, 'stratum': stratum})
    manifest = {'id': 's1-' + 'a' * 16, 'validated': True, 'active': True, 'seed': 20261008,
        'sources': PINS, 'grading_files': {}, 'livebench_release': '2024-11-25', 'livecodebench_release': 'release_v6',
        'language': 'Python', 'counts': COUNTS, 'panels': {'screen': items}, 'protocol': {},
        'grader_image': 'sha256:' + 'b' * 64}
    now = datetime.now(timezone.utc)
    record = ResultSubmission(submission_id='c' * 32, contributor='example-user', model_id='test-free', epoch='e' * 16,
        season=manifest['id'], tier='screen', panel_fingerprint=fingerprint(manifest, 'screen'), runner_revision='1' * 40, exporter_revision='1' * 40,
        reasoning_mode='highest-exposed', reasoning_variant='max', available_variants=['high', 'max'], definition_hash='d' * 64,
        grader_image=manifest['grader_image'], platform='linux-x86_64', started_at=now - timedelta(hours=1), evaluated_at=now,
        free_observed_at=now - timedelta(hours=1), gpqa_privacy_verified=True,
        questions=[{'id': i['id'], 'hash': i['hash'], 'score': n % 2, 'attempts': 1, 'manual_retries': 0,
            'output_tokens': 10, 'reasoning_tokens': 3, 'accounted_tokens': 30, 'reported_tokens': 30,
            'latency_seconds': .5, 'truncated': False} for n, i in enumerate(items)])
    return manifest, record


def test_complete_submission_recomputes_scores_and_preserves_provenance():
    manifest, record = submission_fixture()
    result = validate_submission(record, [manifest])
    assert result.scores == {'reasoning': 50, 'coding': 50}
    assert result.sample_counts == COUNTS['screen']
    assert result.verification == 'community-submitted' and result.contributor == 'example-user'
    assert result.intervals['reasoning'][0] <= 50 <= result.intervals['reasoning'][1]


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'hash', 'panel', 'fractional_code'])
def test_submission_cannot_select_questions_or_change_frozen_panel(mutation):
    manifest, record = submission_fixture()
    data = record.model_dump(mode='json')
    if mutation == 'missing':
        data['questions'].pop()
    elif mutation == 'duplicate':
        data['questions'][-1] = data['questions'][0]
    elif mutation == 'hash':
        data['questions'][0]['hash'] = 'f' * 64
    elif mutation == 'panel':
        data['panel_fingerprint'] = 'f' * 64
    else:
        next(q for q in data['questions'] if q['id'].startswith('livecodebench:'))['score'] = .5
    with pytest.raises(ValueError):
        validate_submission(ResultSubmission.model_validate(data), [manifest])


@pytest.mark.parametrize('mutation', ['private', 'nested_private', 'cap', 'reasoning', 'version', 'privacy', 'usage'])
def test_submission_rejects_private_fields_and_protocol_changes(mutation):
    _, record = submission_fixture()
    data = record.model_dump(mode='json')
    if mutation == 'private':
        data['raw_response'] = 'PRIVATE_SENTINEL'
    elif mutation == 'nested_private':
        data['questions'][0]['prompt'] = 'PRIVATE_SENTINEL'
    elif mutation == 'cap':
        data['questions'][0]['output_tokens'] = 4097
    elif mutation == 'reasoning':
        data['reasoning_variant'] = 'high'
    elif mutation == 'version':
        data['client_version'] = '2.0.32'
    elif mutation == 'privacy':
        data['gpqa_privacy_verified'] = False
    else:
        data['questions'][0]['reported_tokens'] = 31
    with pytest.raises(ValueError):
        ResultSubmission.model_validate(data)


def test_public_build_from_clone_needs_no_private_state_and_retains_base_results(context, tmp_path, monkeypatch):
    db, settings, checkout = context
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: None)
    report = snapshot(db, settings)
    manifest, record = submission_fixture()
    report.seasons = [PublicManifest.model_validate(manifest)]
    clone = tmp_path / 'clone'
    shutil.copytree(checkout / 'web', clone / 'web')
    render_site(report, clone, clone / 'site')
    (clone / 'results').mkdir()
    (clone / 'results' / f'{record.submission_id}.json').write_text(record.model_dump_json())
    original = (clone / 'site/snapshot.json').read_bytes()
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: pytest.fail('Public build read credentials'))
    built = build_site(clone, tmp_path / 'built', 'example-user/free-models')
    public = validate_site(Path(built['site']))
    assert public.rows == report.rows and public.history == report.history and public.capabilities == report.capabilities
    assert public.community_results[0].scores == {'reasoning': 50, 'coding': 50}
    assert public.repository == 'example-user/free-models'
    assert (clone / 'site/snapshot.json').read_bytes() == original
    assert 'https://github.com/example-user/free-models' in (tmp_path / 'built/index.html').read_text()
    csv = (tmp_path / 'built/leaderboard.csv').read_text()
    assert 'community-submitted' in csv and 'example-user' in csv
    first = with_community(public, clone)
    assert first.snapshot_id == with_community(first, clone).snapshot_id


def test_result_loader_rejects_symlinks_unknown_files_and_wrong_names(tmp_path):
    manifest, record = submission_fixture()
    folder = tmp_path / 'results'
    folder.mkdir()
    path = folder / 'wrong.json'
    path.write_text(record.model_dump_json())
    with pytest.raises(ValueError, match='filename'):
        load_submissions(tmp_path, [manifest])
    path.unlink()
    path = folder / f'{record.submission_id}.json'
    path.symlink_to(tmp_path / 'elsewhere.json')
    (tmp_path / 'elsewhere.json').write_text(record.model_dump_json())
    with pytest.raises(ValueError, match='Unexpected'):
        load_submissions(tmp_path, [manifest])
    assert public_source(f'results/{record.submission_id}.json')
    assert not public_source('results/private.sqlite3') and not public_source('results/private.json')
    assert public_source('.github/pull_request_template.md')


@pytest.mark.parametrize('case', ['own', 'impersonation', 'replace'])
def test_result_pr_attribution_and_append_only_audit(tmp_path, monkeypatch, case):
    _, record = submission_fixture()
    name = f'results/{record.submission_id}.json'
    (tmp_path / 'results').mkdir()
    (tmp_path / name).write_text(record.model_dump_json())
    status = 'M' if case == 'replace' else 'A'
    monkeypatch.setattr('freeboard.submissions.subprocess.run', lambda args, **_:
                        subprocess.CompletedProcess(args, 0, f'{status}\t{name}\n', ''))
    if case == 'own':
        validate_pr_submissions(tmp_path, '1' * 40, record.contributor)
    else:
        with pytest.raises(ValueError):
            validate_pr_submissions(tmp_path, '1' * 40, 'another-user' if case == 'impersonation' else record.contributor)


def test_manual_doctor_does_not_require_github_or_launchd(context, monkeypatch):
    from freeboard.diagnostics import doctor
    db, settings, checkout = context
    fixtures.season(db)
    monkeypatch.setattr('freeboard.diagnostics.client_version', lambda _: '1.19.3')
    monkeypatch.setattr('freeboard.diagnostics.shutil.disk_usage', lambda _: SimpleNamespace(free=10 * 1024**3))
    def command(args, **_):
        assert args[0] == 'docker'
        return subprocess.CompletedProcess(args, 0)
    monkeypatch.setattr('freeboard.diagnostics.subprocess.run', command)
    result = doctor(db, settings, checkout)
    assert result['ready'] and not {'github', 'schedule'} & {r['check'] for r in result['checks']}


def test_linux_state_and_auth_setup_are_explicit(tmp_path, monkeypatch):
    from freeboard.config import default_state, save_credential
    from freeboard.scheduling import install
    monkeypatch.setattr('freeboard.config.sys.platform', 'linux')
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path))
    assert default_state() == tmp_path / 'opencode-free-leaderboard'
    with pytest.raises(RuntimeError, match='Linux/WSL2'):
        save_credential('hf', 'synthetic')
    with pytest.raises(RuntimeError, match='Linux/WSL2'):
        install(Settings(state=tmp_path), tmp_path)
    with pytest.raises(ValueError):
        Settings(repository='../bad/repo')


@pytest.mark.parametrize('valid_checksum', [True, False])
def test_client_installer_verifies_checksum_before_execution(tmp_path, monkeypatch, valid_checksum):
    from freeboard import setup
    import hashlib
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, 'w') as archive:
        archive.writestr('opencode', b'synthetic executable fixture')
    data = payload.getvalue()
    checksum = hashlib.sha256(data).hexdigest() if valid_checksum else '0' * 64
    monkeypatch.setattr(setup.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(setup.platform, 'machine', lambda: 'arm64')
    monkeypatch.setitem(setup.ASSETS, ('Darwin', 'arm64'), ('fixture.zip', checksum))
    class Client:
        def __init__(self, **_): pass
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def get(self, _):
            return SimpleNamespace(content=data, raise_for_status=lambda: None)
    monkeypatch.setattr(setup.httpx, 'Client', Client)
    executed = []
    def version(args, **_):
        executed.append(args)
        return subprocess.CompletedProcess(args, 0, '1.19.3', '')
    monkeypatch.setattr(setup.subprocess, 'run', version)
    if valid_checksum:
        result = setup.install_client(tmp_path)
        assert Path(result['client']).read_bytes() == b'synthetic executable fixture' and len(executed) == 1
    else:
        with pytest.raises(RuntimeError, match='checksum'):
            setup.install_client(tmp_path)
        assert not executed and not (tmp_path / 'bin').exists()


def test_build_output_cannot_replace_source_or_unrelated_files(context, tmp_path):
    db, settings, checkout = context
    report = snapshot(db, settings)
    for target in [checkout / 'src', checkout, tmp_path / 'personal']:
        if target == tmp_path / 'personal':
            target.mkdir()
            (target / 'keep.txt').write_text('keep')
        with pytest.raises(ValueError):
            render_site(report, checkout, target)
    assert (tmp_path / 'personal/keep.txt').read_text() == 'keep'


def test_cycle_records_generation_revision_without_rewriting_history(context, monkeypatch):
    from freeboard.runner import Runner
    db, settings, checkout = context
    model, season = fixtures.model(db), fixtures.season(db)
    def git(args, **_):
        return subprocess.CompletedProcess(args, 0, '1' * 40 if 'rev-parse' in args else '', '')
    monkeypatch.setattr('freeboard.runner.subprocess.run', git)
    runner = Runner(db, settings, checkout)
    try:
        cycle = runner.cycle(model, season, 'screen', 'immutable-source')
        assert json.loads(cycle['profile'])['runner_revision'] == '1' * 40
        runner._runner_revision = '2' * 40
        assert runner.cycle(model, season, 'screen', 'immutable-source')['profile'] == cycle['profile']
    finally:
        runner.close()


def test_export_submission_is_complete_sanitized_and_idempotent(context, tmp_path, monkeypatch):
    from freeboard.runner import Runner
    from freeboard.opencode import completion_events
    from freeboard.config import now
    db, settings, checkout = context
    manifest, _ = submission_fixture()
    source = {k: v for k, v in manifest.items() if k not in {'id', 'active', 'validated'}}
    db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)', (manifest['id'], now(), json.dumps(source), 1, 1))
    model = fixtures.model(db)
    profile = json.loads(model['profile'])
    profile['reasoning']['definition_hash'] = 'd' * 64
    db.execute('UPDATE models SET epoch=?,profile=?', ('e' * 16, json.dumps(profile)))
    model = db.one('SELECT * FROM models')
    runner = Runner(db, settings, checkout)
    active = db.one('SELECT * FROM seasons')
    cycle = runner.cycle(model, active, 'screen', 'full-fixture')
    runner.cycle(model, active, 'pilot', 'pilot-fixture')
    db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), 'pilot-fixture'))
    for index, item in enumerate(manifest['panels']['screen']):
        private_id = f'{manifest["id"]}:{item["id"]}'
        content = {'messages': [{'role': 'user', 'content': 'PRIVATE_SENTINEL'}], 'answer': 'PRIVATE_KEY'}
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?)',
                   (private_id, manifest['id'], item['benchmark'], item['stratum'], item['hash'], json.dumps(content)))
        db.execute('INSERT INTO panels VALUES(?,?,?)', (manifest['id'], 'screen', private_id))
        job = db.execute('INSERT INTO jobs(cycle_id,item_id) VALUES(?,?)', (cycle['id'], private_id)).lastrowid
        attempt = runner.budget.reserve('opencode_generation', 100, job, model['id'])
        response = {'protocol': 'opencode', 'body': json.dumps({'events': completion_events(fixtures.native_result(), model['id'])}),
                    'cap': 4096, 'protocol_revision': 6}
        path = settings.state / 'responses' / f'{attempt}.json'
        path.write_text(json.dumps(response))
        runner.budget.finish(attempt, 'received', usage={'total_tokens': 30}, response_path=str(path))
        db.execute("UPDATE jobs SET status='graded',score=?,response_path=?,latency=.5 WHERE id=?",
                   (index % 2, str(path), job))
    public = tmp_path / 'clone'
    (public / 'site').mkdir(parents=True)
    (public / 'site/manifest.json').write_text(json.dumps([manifest]))
    monkeypatch.setattr('freeboard.submissions.subprocess.run', lambda args, **_:
                        subprocess.CompletedProcess(args, 0, '1' * 40 if 'rev-parse' in args else '', ''))
    try:
        first = export_submission(db, public, model['id'], 'example-user', 'screen')
        original = Path(first['submission']).read_bytes()
        monkeypatch.setattr('freeboard.submissions.subprocess.run', lambda args, **_:
                            subprocess.CompletedProcess(args, 0, '2' * 40 if 'rev-parse' in args else '', ''))
        second = export_submission(db, public, model['id'], 'example-user', 'screen')
        assert first['submission'] == second['submission'] and first['questions'] == 80
        assert second['existing'] and Path(second['submission']).read_bytes() == original
        text = Path(first['submission']).read_text()
        assert 'PRIVATE_SENTINEL' not in text and 'PRIVATE_KEY' not in text and str(settings.state) not in text
        assert load_submissions(public, [manifest])[0].scores == {'reasoning': 50, 'coding': 50}
        db.execute("UPDATE jobs SET status='pending' WHERE id=(SELECT MIN(id) FROM jobs)")
        with pytest.raises(ValueError, match='saved, graded'):
            export_submission(db, public, model['id'], 'example-user', 'screen')
    finally:
        runner.close()
