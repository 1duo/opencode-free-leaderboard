import json
from pathlib import Path

from freeboard.config import SEED, Settings, digest, now
from freeboard.db import DB
from freeboard.lightweight import STRATA, prepare_lightweight
from freeboard.opencode import profile
from freeboard.runner import Runner
from freeboard.scoring import lightweight_results


def test_lightweight_frozen_nested_public_panel_and_complete_only_scores(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    settings = Settings(state=tmp_path / 'private')
    settings.initialize(checkout)
    db = DB(settings.state)
    source = 'public-pilot-fixture'
    manifest = {'seed': SEED, 'sources': {}, 'panels': {}, 'counts': {}, 'partial': True,
                'livebench_release': '2024-11-25', 'livecodebench_release': 'release_v6',
                'language': 'Python', 'grader_image': 'sha256:frozen', 'grading_files': {}}
    db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)', (source, now(), json.dumps(manifest), 1, 0))
    for benchmark, strata in STRATA.items():
        for stratum, count in strata.items():
            for i in range(count + 2):
                ident = f'{benchmark}:{stratum}:{i}'
                content = {'messages': [{'role': 'user', 'content': f'PRIVATE {ident}'}]}
                db.execute('INSERT INTO items VALUES(?,?,?,?,?,?)',
                           (ident, source, benchmark, stratum, digest(content), json.dumps(content)))
                db.execute('INSERT INTO panels VALUES(?,?,?)', (source, 'screen', ident))
    # A private question accidentally present in the source never enters this tier.
    db.execute('INSERT INTO items VALUES(?,?,?,?,?,?)', ('private', source, 'gpqa', 'physics', 'hash', '{}'))
    db.execute('INSERT INTO panels VALUES(?,?,?)', (source, 'screen', 'private'))
    runner = Runner(db, settings, checkout)
    try:
        season = prepare_lightweight(runner)
        again = prepare_lightweight(runner)
        assert again['id'] == season['id'] and again['manifest'] == season['manifest']
        selected = db.rows('SELECT i.* FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=?', (season['id'],))
        assert len(selected) == 12 and not any(i['benchmark'] == 'gpqa' for i in selected)
        for benchmark, strata in STRATA.items():
            for stratum, count in strata.items():
                assert sum(i['benchmark'] == benchmark and i['stratum'] == stratum for i in selected) == count
        assert not db.one('SELECT * FROM seasons WHERE active=1')
        model_profile = {**profile('chat'), 'cap_verified': True}
        db.execute('INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?)',
                   ('test-free', 'Test', None, 'opencode', 'eligible', 'epoch', 0, json.dumps(model_profile), now(), None))
        model = db.one('SELECT * FROM models')
        cycle = runner.cycle(model, season, 'lightweight', 'mini')
        runner.add_panel(cycle, 'lightweight')
        runner.add_panel(cycle, 'lightweight')
        assert db.one('SELECT COUNT(*) n FROM jobs')['n'] == 12
        jobs = db.rows('SELECT * FROM jobs ORDER BY id')
        for j in jobs[:-1]:
            db.execute("UPDATE jobs SET status='graded',score=1 WHERE id=?", (j['id'],))
        runner.finish_cycle(cycle['id'])
        result = lightweight_results(db, settings)[0]
        assert not result.benchmark_scores and result.status == 'pending'
        assert not db.one('SELECT completed_at FROM cycles')['completed_at']
        db.execute("UPDATE jobs SET status='graded',score=1 WHERE id=?", (jobs[-1]['id'],))
        runner.finish_cycle(cycle['id'])
        result = lightweight_results(db, settings)[0]
        assert result.status == 'complete' and result.progress == {'livebench': 6, 'livecodebench': 6}
        assert result.benchmark_scores == {'livebench': 100, 'livecodebench': 100}
        # Uniform six-question strata must not suggest perfect certainty.
        assert result.intervals == {} and set(result.interval_warnings) == {'livebench', 'livecodebench'}
        assert result.unranked and result.tier == 'lightweight'
        assert db.one('SELECT completed_at FROM cycles')['completed_at']
    finally:
        runner.opencode.close()
        runner.client.close()
