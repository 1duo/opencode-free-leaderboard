"""Offline C-panel and publication boundaries; executable fixtures run at preparation."""
import json
from types import SimpleNamespace

import pytest

from freeboard.config import Settings
from freeboard.grading import DockerGrader
from freeboard.scoring import expand_coding, expanded_comparisons
from freeboard.submissions import ResultSubmission, fingerprint, validate_submission
from freeboard.systems import POPULATION, select_systems
from test_coding import expansion_fixture
from test_contributions import submission_fixture


def test_systems_panel_has_nested_disjoint_domains_and_no_test_answers_in_prompts(tmp_path):
    for slugs in POPULATION.values():
        for slug in slugs:
            directory = tmp_path / 'exercises/practice' / slug
            for name in ['.meta', '.docs', 'test-framework']:
                (directory / name).mkdir(parents=True, exist_ok=True)
            stem = slug.replace('-', '_')
            (directory / '.meta/example.h').write_text('int fixture(int x);\n')
            (directory / '.meta/example.c').write_text('REFERENCE_ONLY_SENTINEL\n')
            (directory / '.docs/instructions.md').write_text('Synthetic public task instructions.')
            (directory / f'test_{stem}.c').write_text('TEST_IGNORE(); // enable full test\nRUN_TEST(fixture);\nTEST_ONLY_SENTINEL')
            for name in ['unity.c', 'unity.h', 'unity_internals.h']:
                (directory / 'test-framework' / name).write_text('Synthetic framework.')
    panel = select_systems(tmp_path)
    assert panel == select_systems(tmp_path)
    assert len(panel['coding']) == 24 and len(panel['coding_pilot']) == 4
    assert {i['id'] for i in panel['coding']}.isdisjoint(i['id'] for i in panel['coding_pilot'])
    for domain in POPULATION:
        assert sum(i['stratum'] == domain for i in panel['coding']) == 6
        assert sum(i['stratum'] == domain for i in panel['coding_pilot']) == 1
    for item in panel['coding'] + panel['coding_pilot']:
        assert item['test_count'] == 1 and 'TEST_IGNORE' not in item['test']
        assert 'TEST_ONLY_SENTINEL' not in json.dumps(item['messages'])
        assert 'REFERENCE_ONLY_SENTINEL' not in json.dumps(item['messages'])
        assert 'int fixture(int x);' in item['messages'][0]['content']


def test_compiled_grading_has_only_one_executable_scratch_mount_and_omits_oracles(tmp_path, monkeypatch):
    captured = {}
    def run(command, **kwargs):
        captured.update(command=command, payload=json.loads(kwargs['input']))
        return SimpleNamespace(returncode=0, stdout='{"score":1}\n')
    monkeypatch.setattr('freeboard.grading.subprocess.run', run)
    item = {'benchmark': 'systems_c', 'header_name': 'fixture.h', 'header': 'interface',
            'test': 'tests', 'test_count': 1, 'framework_hash': 'a' * 64,
            'reference': 'PRIVATE_REFERENCE', 'messages': ['PRIVATE_PROMPT']}
    assert DockerGrader(Settings(state=tmp_path)).grade(item, 'code', 'sha256:fixture') == 1
    cmd = captured['command']
    assert '--network=none' in cmd and '--read-only' in cmd and '--user=65534:65534' in cmd
    assert '--cap-drop=ALL' in cmd and '--security-opt=no-new-privileges' in cmd
    assert '--tmpfs=/work:rw,exec,nosuid,nodev,size=128m,mode=1777' in cmd
    assert all('noexec' in value for value in cmd if value.startswith(('--tmpfs=/tmp:', '--tmpfs=/dev/shm:')))
    assert captured['payload']['item'] == {k: v for k, v in item.items() if k not in {'reference', 'messages'}}
    assert cmd[-1] == 'sha256:fixture'


def systems_fixture(tmp_path):
    db, settings, caps, models, evaluations = expansion_fixture(tmp_path)
    manifest = json.loads(db.one("SELECT manifest FROM seasons WHERE id='extension'")['manifest'])
    manifest['protocol'] = {'panel': 'systems-c-v1'}
    for i, item in enumerate(manifest['panels']['coding']):
        item['benchmark'] = 'systems_c'
        item['stratum'] = list(POPULATION)[i // 6]
        db.execute('UPDATE items SET benchmark=?,stratum=? WHERE id=?', ('systems_c', item['stratum'], item['id']))
    db.execute("UPDATE seasons SET manifest=? WHERE id='extension'", (json.dumps(manifest),))
    evaluations = [e.model_copy(update={'benchmark': 'systems_c', 'pilot_expected': 4}) for e in evaluations]
    for model in models:
        model.free_eligible = True
        model.availability = 'client_access_restricted'
    return db, settings, caps, models, evaluations


def test_systems_composite_weights_pairing_and_missing_cohort_member(tmp_path):
    db, settings, caps, models, evaluations = systems_fixture(tmp_path)
    try:
        unchanged, weights = expand_coding(db, settings, caps, models, evaluations[:1])
        assert unchanged == caps and weights == {'livecodebench': 1}
        merged, weights = expand_coding(db, settings, caps, models, evaluations)
        assert weights == {'livecodebench': .5, 'systems_c': .5}
        coded = {c.model_id: c for c in merged if c.category == 'coding'}
        assert coded['a'].score == 50 and coded['b'].score == 75
        assert coded['b'].rank == 1 and coded['a'].rank == 2
        assert coded['a'].sample_counts == {'livecodebench': 40, 'systems_c': 24}
        assert coded['a'].source_seasons == {'livecodebench': 'base', 'systems_c': 'extension'}
        comparison = expanded_comparisons(db, settings, merged)[0]
        assert comparison.difference == -25 and comparison.interval[0] <= -25 <= comparison.interval[1]
    finally:
        db.conn.close()


def test_systems_result_pr_requires_fixed_sources_domains_and_binary_grades():
    manifest, record = submission_fixture()
    items = [{'id': f'systems_c:synthetic:{i}', 'hash': 'a' * 64, 'benchmark': 'systems_c',
              'stratum': list(POPULATION)[i // 6]} for i in range(24)]
    manifest.update(id='coding-' + 'a' * 16, panels={'coding': items},
        counts={'coding': {'systems_c': 24}}, protocol={'panel': 'systems-c-v1'})
    data = record.model_dump(mode='json')
    data.update(season=manifest['id'], tier='coding', panel_fingerprint=fingerprint(manifest, 'coding'),
        gpqa_privacy_verified=False, questions=[{**q.model_dump(), 'id': item['id'], 'hash': item['hash']}
            for q, item in zip(record.questions, items)])
    assert validate_submission(ResultSubmission.model_validate(data), [manifest]).scores == {'coding': 50}
    # Rebuilding identical source on another host produces a different image ID.
    # Community records retain that ID; matching source/panel hashes are required.
    other_host = {**data, 'grader_image': 'sha256:' + 'f' * 64}
    assert validate_submission(ResultSubmission.model_validate(other_host), [manifest]).scores == {'coding': 50}
    with pytest.raises(ValueError, match='validated panel'):
        validate_submission(ResultSubmission.model_validate({**data, 'panel_fingerprint': 'f' * 64}), [manifest])
    data['questions'][0]['score'] = .5
    with pytest.raises(ValueError, match='binary'):
        validate_submission(ResultSubmission.model_validate(data), [manifest])


def test_daily_resumes_prepared_c_extension_only_when_generation_is_runnable(tmp_path, monkeypatch):
    import test_core as fixtures
    from freeboard.cli import daily_work
    from freeboard.config import now
    from freeboard.db import DB
    from freeboard.runner import Runner
    state = tmp_path / 'state'
    state.mkdir()
    db = DB(state)
    settings = Settings(state=state)
    model = fixtures.model(db)
    base = fixtures.season(db)
    runner = Runner(db, settings, tmp_path)
    pilot = runner.cycle(model, base, 'pilot', 'fixture-pilot')
    db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), pilot['id']))
    db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)',
        ('c-extension', now(), json.dumps({'protocol': {'panel': 'systems-c-v1'}}), 1, 0))
    calls = []
    monkeypatch.setattr(runner, 'run', lambda **_: {'processed': 0})
    monkeypatch.setattr('freeboard.coding.run_coding', lambda _, season: calls.append(season['id']) or {'processed': 0})
    try:
        daily_work(runner)
        assert calls == ['c-extension']
        db.execute("UPDATE models SET status='client_access_restricted'")
        daily_work(runner)
        assert calls == ['c-extension']
    finally:
        runner.close()
        db.conn.close()
