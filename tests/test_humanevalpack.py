"""Targeted multilingual sampling, scoring and public/private boundaries."""
import json
from types import SimpleNamespace

import pytest

from freeboard.cli import parser
from freeboard.config import Settings, now
from freeboard.grading import DockerGrader
from freeboard.humanevalpack import LANGUAGES, STRATA, select_humanevalpack
from freeboard.scoring import PublicProtocol, expand_coding, expanded_comparisons
from freeboard.submissions import ResultSubmission, fingerprint, validate_submission
from test_coding import expansion_fixture
from test_contributions import submission_fixture
import test_core as fixtures

context = fixtures.context


def test_panel_has_unique_families_balanced_tasks_and_private_oracles():
    datasets = {language: [{'task_id': f'{language}/{i}', 'prompt': 'PUBLIC_STARTER',
        'instruction': 'PUBLIC_INSTRUCTION', 'canonical_solution': 'PRIVATE_REFERENCE',
        'buggy_solution': 'PUBLIC_BUGGY', 'test_setup': 'PRIVATE_SETUP',
        'test': ('func TestFixture(t *testing.T) {}' if language == 'go' else
                 '#[test]\nfn fixture(){}' if language == 'rust' else 'assert(PRIVATE_TEST);')}
        for i in range(40)] for language in LANGUAGES}
    first = select_humanevalpack(datasets)
    assert first == select_humanevalpack({k: list(reversed(v)) for k, v in datasets.items()})
    assert len(first['coding']) == 24 and len(first['coding_pilot']) == 6
    all_items = first['coding'] + first['coding_pilot']
    assert len({i['family'] for i in all_items}) == 30
    assert {s: sum(i['stratum'] == s for i in first['coding']) for s in STRATA} == STRATA
    assert all(sum(i['stratum'] == s for i in first['coding_pilot']) == 1 for s in STRATA)
    for item in all_items:
        prompt = json.dumps(item['messages'])
        assert 'PRIVATE_REFERENCE' not in prompt and 'PRIVATE_SETUP' not in prompt and 'PRIVATE_TEST' not in prompt
        assert ('PUBLIC_BUGGY' in prompt) == (item['task'] == 'repair')


def test_multilingual_grader_omits_answers_and_uses_existing_sandbox(tmp_path, monkeypatch):
    captured = {}
    def run(command, **kwargs):
        captured.update(command=command, item=json.loads(kwargs['input'])['item'])
        return SimpleNamespace(returncode=0, stdout='{"score":1}\n')
    monkeypatch.setattr('freeboard.grading.subprocess.run', run)
    item = {'benchmark': 'humanevalpack', 'language': 'rust', 'test_setup': '',
        'test': 'synthetic tests', 'test_count': 1, 'reference': 'SECRET', 'buggy': 'SECRET', 'messages': []}
    assert DockerGrader(Settings(state=tmp_path)).grade(item, 'answer', 'image') == 1
    assert set(captured['item']) == {'benchmark', 'language', 'test_setup', 'test', 'test_count'}
    assert '--network=none' in captured['command'] and '--read-only' in captured['command']
    assert '--tmpfs=/work:rw,exec,nosuid,nodev,size=128m,mode=1777' in captured['command']


def test_humanevalpack_uses_matching_complete_cohort_and_paired_intervals(tmp_path):
    db, settings, caps, models, evaluations = expansion_fixture(tmp_path)
    try:
        manifest = json.loads(db.one("SELECT manifest FROM seasons WHERE id='extension'")['manifest'])
        manifest['protocol'] = {'panel': 'humanevalpack-multilingual-v1'}
        for i, item in enumerate(manifest['panels']['coding']):
            item.update(benchmark='humanevalpack', stratum=list(STRATA)[i // 4])
            db.execute('UPDATE items SET benchmark=?,stratum=? WHERE id=?',
                       (item['benchmark'], item['stratum'], item['id']))
        db.execute("UPDATE seasons SET manifest=? WHERE id='extension'", (json.dumps(manifest),))
        evaluations = [e.model_copy(update={'benchmark': 'humanevalpack', 'pilot_expected': 6}) for e in evaluations]
        unchanged, weights = expand_coding(db, settings, caps, models, evaluations[:1])
        assert unchanged == caps and weights == {'livecodebench': 1}
        merged, weights = expand_coding(db, settings, caps, models, evaluations)
        assert weights == {'livecodebench': .5, 'humanevalpack': .5}
        assert {r.model_id: r.score for r in merged if r.category == 'coding'} == {'a': 50, 'b': 75}
        assert expanded_comparisons(db, settings, merged)[0].difference == -25
    finally:
        db.conn.close()


def test_multilingual_results_pr_requires_all_six_strata_and_binary_grades():
    manifest, record = submission_fixture()
    items = [{'id': f'humanevalpack:fixture:{i}', 'hash': 'a' * 64,
        'benchmark': 'humanevalpack', 'stratum': list(STRATA)[i // 4]} for i in range(24)]
    manifest.update(id='coding-' + 'a' * 16, panels={'coding': items},
        counts={'coding': {'humanevalpack': 24}}, protocol={'panel': 'humanevalpack-multilingual-v1'})
    data = record.model_dump(mode='json')
    data.update(season=manifest['id'], tier='coding', panel_fingerprint=fingerprint(manifest, 'coding'),
        gpqa_privacy_verified=False, questions=[{**q.model_dump(), 'id': item['id'], 'hash': item['hash']}
            for q, item in zip(record.questions, items)])
    assert validate_submission(ResultSubmission.model_validate(data), [manifest]).scores == {'coding': 50}
    data['questions'][0]['score'] = .5
    with pytest.raises(ValueError, match='binary'):
        validate_submission(ResultSubmission.model_validate(data), [manifest])
    data['questions'][0]['score'] = 1
    manifest['panels']['coding'][0]['stratum'] = 'rust_generation'
    data['panel_fingerprint'] = fingerprint(manifest, 'coding')
    with pytest.raises(ValueError, match='domain coverage'):
        validate_submission(ResultSubmission.model_validate(data), [manifest])
    PublicProtocol(panel='humanevalpack-multilingual-v1', strata={'humanevalpack': STRATA})
    assert parser().parse_args(['coding']).benchmark == 'humanevalpack'


def test_backup_rotation_retains_seven_dates_across_compressed_and_raw(context):
    db, settings, _ = context
    folder = settings.state / 'backups'
    for day in range(1, 9):
        (folder / f'2000-01-{day:02}.sqlite3.gz').write_bytes(b'synthetic archive')
    db.backup()
    assert sorted(p.name[:10] for p in folder.iterdir()) == [
        *[f'2000-01-{day:02}' for day in range(3, 9)], now()[:10]]
