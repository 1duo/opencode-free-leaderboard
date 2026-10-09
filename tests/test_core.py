import json
from pathlib import Path

import numpy as np
import pytest

from freeboard.adapters import parse
from freeboard.budget import Budget, BudgetExhausted, usage_total
from freeboard.config import COUNTS, Settings, now
from freeboard.db import DB
from freeboard.discovery import excluded, parse_evidence
from freeboard.grading import DockerGrader, GradingUnavailable, gpqa_score
from freeboard.panels import sample, prepare
from freeboard.publication import export, validate_site, public_source
from freeboard.runner import CAP_CALIBRATION, Runner
from freeboard.opencode import PERMISSIONS, configuration, completion_events, profile, prompt_body, reasoning_policy
from freeboard.scoring import bootstrap, is_complete, make_row


@pytest.fixture
def context(tmp_path):
    checkout = Path(__file__).resolve().parents[1]
    settings = Settings(state=tmp_path / "private")
    settings.initialize(checkout)
    db = DB(settings.state)
    return db, settings, checkout


def model(db, name="test-free"):
    settings_profile = {**profile("chat", {'mode':'provider-managed','variant':None,'options':{},'available_variants':[]}), "cap_verified": True}
    db.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?)", (name, name, "https://opencode.ai/zen/v1/chat/completions", "chat", "eligible", "epoch", 0, json.dumps(settings_profile), now(), None))
    return db.one("SELECT * FROM models WHERE id=?", (name,))


def season(db):
    manifest = {"seed": 20261008, "sources": {}, "livebench_release": "2024-11-25", "livecodebench_release": "release_v6",
                "language": "Python", "counts": COUNTS, "panels": {}, "grader_image": "sha256:fixture"}
    db.execute("INSERT INTO seasons VALUES(?,?,?,?,?)", ("season", now(), json.dumps(manifest), 1, 1))
    return db.one("SELECT * FROM seasons WHERE id='season'")


def add_item(db, ident="q", tier="screen", benchmark="gpqa", content=None):
    value = content or {"benchmark": benchmark, "answer": "A", "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
    db.execute("INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)", (ident, "season", benchmark, "stratum", "hash", json.dumps(value)))
    db.execute("INSERT OR IGNORE INTO panels VALUES(?,?,?)", ("season", tier, ident))


def job_context(context):
    db, settings, checkout = context
    item_model, active = model(db), season(db)
    runner = Runner(db, settings, checkout)
    cycle = runner.cycle(item_model, active, "screen", "cycle")
    add_item(db)
    runner.add_panel(cycle, "screen")
    job = db.one("SELECT * FROM jobs")
    item = json.loads(db.one("SELECT * FROM items")["content"])
    return runner, job, item_model, item


def response_body(tokens=10):
    return {"choices": [{"message": {"content": "FINAL: A"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": tokens, "total_tokens": 20 + tokens,
                      "completion_tokens_details": {"reasoning_tokens": 3}}}



def native_result(tokens=10, text='FINAL: A', reason='stop', missing_usage=False):
    parts = [{'type': 'text', 'messageID': 'm', 'text': text},
             {'type': 'step-finish', 'messageID': 'm', 'reason': reason, 'cost': 0,
              'tokens': None if missing_usage else {'input': 20, 'output': max(0,tokens-3),
                        'reasoning': min(3,tokens), 'total': 20+tokens, 'cache': {'read': 0, 'write': 0}}}]
    return {'info': {'id': 'm', 'role': 'assistant', 'agent': 'benchmark', 'providerID': 'opencode',
                     'modelID': 'test-free', 'cost': 0}, 'parts': parts}


def native_error(code, message):
    return {'info': {'error': {'name': 'APIError', 'data': {'statusCode': code, 'message': message}}}}

def test_fail_closed_prices_and_exact_join():
    html = '<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr><tr><td>Alias</td><td>alias</td><td>https://opencode.ai/zen/v1/chat/completions</td></tr></table>'
    prices = '<table><tr><th>Model</th><th>Input</th><th>Output</th><th>Cached Read</th></tr><tr><td>Alias</td><td>Free</td><td>Free</td><td>-</td></tr>{}</table>'
    assert parse_evidence(html)["alias"]["free"] is False
    assert parse_evidence(html + prices.format(""))["alias"]["free"] is True
    contradiction = '<tr><td>Alias</td><td>$0.10</td><td>Free</td><td>-</td></tr>'
    assert parse_evidence(html + prices.format(contradiction))["alias"]["free"] is False
    assert excluded("muse-spark-1.3-contributor-free")


def test_generation_endpoint_and_free_gates():
    value = {'id': 'alias', 'status': 'eligible', 'endpoint': 'https://opencode.ai/zen/v1/chat/completions',
             'profile': json.dumps(profile('chat', {'mode':'provider-managed','variant':None,'options':{},'available_variants':[]}))}
    messages = [{'role':'system','content':'EXACT SYSTEM'}, {'role':'user','content':'EXACT USER'}]
    body = prompt_body(value, messages)
    assert body['system']=='EXACT SYSTEM' and body['parts']==[{'type':'text','text':'EXACT USER'}]
    assert body['model']=={'providerID':'opencode','modelID':'alias'}
    cfg=configuration('alias',4096)
    assert 'steps' not in cfg['agent']['benchmark'] and cfg['agent']['benchmark']['permission']==PERMISSIONS
    assert set(PERMISSIONS.values()) == {'deny', 'ask'} and PERMISSIONS['*'] == 'deny'
    assert PERMISSIONS['external_directory'] == 'ask'
    assert cfg['experimental']['continue_loop_on_deny'] is False
    assert cfg['provider']['opencode']['whitelist']==['alias']
    for changed in [{'status':'paid'},{'id':'muse-spark-1.3-contributor-free'},
                    {'endpoint':'https://other.example/chat/completions'},
                    {'profile':json.dumps({'protocol':'chat'})}]:
        with pytest.raises(ValueError):
            prompt_body({**value,**changed},messages)
    with pytest.raises(ValueError):
        prompt_body(value,[{'role':'assistant','content':'repair'}])
    with pytest.raises(ValueError):
        completion_events({**native_result(),'info':{**native_result()['info'],'modelID':'paid'}},'test-free')
    for changed in [{'assistant_turns':2},{'tool_parts':1}]:
        with pytest.raises(ValueError):
            completion_events({**native_result(),**changed},'test-free')


def test_highest_reasoning_is_explicit_and_fail_closed(context):
    from freeboard.scoring import reasoning_label
    from freeboard.config import digest
    definition={'api': {}, 'capabilities': {'reasoning':True}, 'options':{},
                'variants':{k:{'reasoningEffort':k} for k in ['low','high','xhigh','max']}}
    selected=reasoning_policy(definition)
    assert selected['variant']=='max' and selected['options']=={'reasoningEffort':'max'}
    settings=profile('chat',selected)
    value={'id':'alias','status':'eligible','endpoint':'https://opencode.ai/zen/v1/chat/completions','profile':json.dumps(settings)}
    assert prompt_body(value,[{'role':'user','content':'Q'}])['variant']=='max'
    assert reasoning_label(settings)=='Highest exposed: max'
    lower=reasoning_policy({**definition,'variants':{'high':{'reasoningEffort':'high'}}})
    assert lower['variant']=='high' and digest(profile('chat',lower))!=digest(settings)
    managed=reasoning_policy({**definition,'variants':{}})
    assert managed['variant'] is None and 'Provider-managed' in reasoning_label(profile('chat',managed))
    with pytest.raises(ValueError):
        reasoning_policy({**definition,'variants':{'unknown':{'reasoningEffort':'unknown'}}})
    with pytest.raises(ValueError):
        prompt_body({**value,'profile':json.dumps(profile('chat'))},[{'role':'user','content':'Q'}])
    db,_,_=context
    m=model(db)
    db.execute('UPDATE models SET profile=? WHERE id=?',(json.dumps(settings),m['id']))
    row,_=make_row(db,context[1],db.one('SELECT * FROM models'),None,'screen')
    assert row.reasoning_variant=='max' and row.reasoning_setting=='Highest exposed: max'


def test_discovery_reasoning_epochs_and_cap_failure_persist(context, monkeypatch):
    import httpx
    from freeboard.discovery import discover
    from freeboard.config import ZEN_BASE, ZEN_DOCS
    db,settings,_=context
    definition={'api':{'id':'alias','url':ZEN_BASE},'providerID':'opencode','cost':{'input':0,'output':0},'capabilities':{'reasoning':True},'options':{},'variants':{'high':{'reasoningEffort':'high'}}}
    monkeypatch.setattr('freeboard.opencode.client_version',lambda _: '1.18.31')
    monkeypatch.setattr('freeboard.opencode.inspect_models',lambda *_:{'alias':definition})
    monkeypatch.setattr('freeboard.discovery.credential',lambda _:None)
    html=f'<table><tr><th>Model</th><th>Model ID</th><th>Endpoint</th></tr><tr><td>Alias</td><td>alias</td><td>{ZEN_BASE}/chat/completions</td></tr></table><table><tr><th>Model</th><th>Input</th><th>Output</th></tr><tr><td>Alias</td><td>Free</td><td>Free</td></tr></table>'
    client=httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200,text=html) if str(request.url)==ZEN_DOCS else httpx.Response(200,json={'data':[{'id':'alias'}]})))
    budget=Budget(db,settings)
    assert discover(db,budget,client)['ok']
    original=db.one('SELECT * FROM models')
    assert json.loads(original['profile'])['reasoning']['variant']=='high'
    db.execute("UPDATE models SET status='cap_violation' WHERE id='alias'")
    discover(db,budget,client)
    assert db.one('SELECT * FROM models')['status']=='cap_violation'
    definition['variants']['max']={'reasoningEffort':'max'}
    discover(db,budget,client)
    changed=db.one('SELECT * FROM models')
    assert changed['epoch']!=original['epoch'] and changed['status']=='eligible'
    assert not json.loads(changed['profile'])['cap_verified']
    definition['variants']={'unknown':{'reasoningEffort':'unknown'}}
    assert discover(db,budget,client)['ok']
    assert db.one('SELECT * FROM models')['status']=='configuration_error'


def test_rejected_tool_is_zero_without_repair_or_grading(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    result = native_result(reason='tool-calls')
    result['parts'].append({'type':'tool','messageID':'m','tool':'bash',
        'state':{'status':'error','error':'The user rejected permission to use this specific tool call.'}})
    result['denied_permissions'] = ['permission-test']
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: result)
    runner.generate(job, item_model, item, 4096)
    saved = runner.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
    assert saved['status']=='generated'
    runner.grade(saved, item, 'unused')
    assert runner.db.one('SELECT score FROM jobs WHERE id=?', (job['id'],))['score']==0
    assert len(runner.db.rows('SELECT * FROM attempts'))==1
    for state in [{'status':'completed','output':'executed'}, {'status':'error','error':'other failure'}]:
        result['parts'][-1]['state']=state
        with pytest.raises(ValueError):
            completion_events(result,'test-free')


def test_unrequested_sdk_turn_is_charged_and_never_replayed(context, monkeypatch):
    runner,job,item_model,item=job_context(context)
    journal=runner.settings.state/'logs/extra-turn.json'
    journal.write_text(json.dumps({'assistant_turns_observed':2}))
    def fail(*_):
        runner.opencode.failure_path=str(journal)
        raise RuntimeError('OpenCode attempted another assistant turn; no replay')
    monkeypatch.setattr(runner.opencode,'generate',fail)
    runner.generate(job,item_model,item,4096)
    attempts=runner.db.rows('SELECT * FROM attempts ORDER BY id')
    assert len(attempts)==2 and all(a['status']=='ambiguous' for a in attempts)
    assert runner.budget.summary()['accounted_tokens']==2*attempts[0]['reserved_tokens']
    assert runner.budget.record_unrequested_turn(attempts[0]['id'],2,str(journal))==attempts[1]['id']
    assert runner.drain(runner.active_season())['processed']==0
    assert runner.db.one('SELECT * FROM jobs')['score'] is None


@pytest.mark.parametrize('ambiguous_cap', [False, True])
def test_cap_calibration_is_separate_and_does_not_replay_probes(context, monkeypatch, ambiguous_cap):
    db,settings,checkout=context
    m=model(db)
    active=season(db)
    settings_profile=json.loads(m['profile'])
    settings_profile['cap_verified']=False
    db.execute('UPDATE models SET profile=? WHERE id=?',(json.dumps(settings_profile),m['id']))
    runner=Runner(db,settings,checkout)
    runner.health(db.one('SELECT * FROM models'),active)
    if ambiguous_cap:
        job=db.one('SELECT * FROM jobs ORDER BY id LIMIT 1')
        attempt=runner.budget.reserve('opencode_generation',1000,job['id'],m['id'])
        runner.budget.finish(attempt,'ambiguous')
        db.execute("UPDATE jobs SET status='ambiguous' WHERE id=?",(job['id'],))
    def generate(_,messages,cap,on_retry):
        if messages[-1]['content']==CAP_CALIBRATION['prompt']:
            return native_result(tokens=cap,reason='length',text='ABCDEFGHIJKLMNOPQRSTUVWXYZ'*20)
        return native_result(text='READY')
    monkeypatch.setattr(runner.opencode,'generate',generate)
    monkeypatch.setattr(runner.grader,'grade',lambda *_:0)
    assert runner.drain(active,kind='pilot')['processed']==(6 if ambiguous_cap else 7)
    assert json.loads(db.one('SELECT profile FROM models')['profile'])['cap_verified']
    assert len(db.rows('SELECT * FROM attempts'))==7
    assert db.one("SELECT COUNT(*) n FROM jobs WHERE status='ambiguous'")['n']==int(ambiguous_cap)
    assert db.one("SELECT score FROM jobs j JOIN cycles c ON c.id=j.cycle_id WHERE c.kind='cap_calibration'")['score']==1
    assert runner.drain(active,kind='pilot')['processed']==0
    assert len(db.rows('SELECT * FROM attempts'))==7


def test_usage_subsets_not_double_counted():
    assert usage_total({"total_tokens": 100, "input_tokens_details": {"cached_tokens": 90}, "output_tokens_details": {"reasoning_tokens": 40}}) == 100
    assert usage_total({"input_tokens": 60, "output_tokens": 40, "output_tokens_details": {"reasoning_tokens": 35}}) == 100
    assert usage_total(None) is None
    assert usage_total({'total_tokens': 1, 'input_tokens': 60, 'output_tokens': 40}) is None
    assert usage_total({'total_tokens': -1}) is None
    assert usage_total({'total_tokens': True}) is None


def test_cap_revoked_during_crash_recovery(context):
    runner, job, _, _ = job_context(context)
    attempt = runner.budget.reserve('generation', 500, job['id'])
    path = runner.settings.state / 'responses' / f'{attempt}.json'
    path.write_text(json.dumps({'protocol': 'chat', 'body': json.dumps(response_body(4097)), 'cap': 4096}))
    runner.db.recover()
    assert runner.db.one('SELECT status FROM jobs')['status'] == 'failed'
    value = runner.db.one('SELECT * FROM models')
    assert value['status'] == 'cap_violation'
    assert not json.loads(value['profile'])['cap_verified']


def test_missing_cap_usage_retained_without_rank_or_replay(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'public')
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: native_result(missing_usage=True))
    runner.generate(job, item_model, item, 4096)
    saved = runner.db.one('SELECT * FROM jobs')
    assert saved['status'] == 'cap_unverified' and saved['score'] is None
    assert Path(saved['response_path']).exists()
    assert len(runner.db.rows('SELECT * FROM attempts')) == 1
    assert runner.budget.summary()['accounted_tokens'] > 4096


def test_saved_answer_graded_after_removal(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'public')
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: native_result())
    runner.generate(job, item_model, item, 4096)
    runner.db.execute("UPDATE models SET status='removed',epoch='new' ")
    assert runner.drain(runner.active_season(), limit=1)['processed'] == 1
    assert runner.db.one('SELECT score FROM jobs')['score'] == 1
    assert len(runner.db.rows('SELECT * FROM attempts')) == 1


def test_historical_profile_is_immutable(context):
    runner, _, _, _ = job_context(context)
    cycle = runner.db.one('SELECT * FROM cycles')
    runner.db.execute("UPDATE models SET protocol='responses',endpoint='https://opencode.ai/zen/v1/responses',profile=?",
                      (json.dumps({'protocol': 'responses', 'cap_verified': False}),))
    current = runner.db.one('SELECT * FROM models')
    row, _ = make_row(runner.db, runner.settings, current, cycle, 'screen')
    assert row.protocol == 'opencode' and row.endpoint.endswith('/chat/completions') and row.cap_verified


def test_public_source_rejects_private_artifact_paths():
    assert public_source('src/freeboard/runner.py')
    assert public_source('grading/worker.py')
    for path in ['src/freeboard/answers.json', 'tests/gpqa.csv', 'docs/credentials.txt', 'grading/state.db', 'site/responses.json']:
        assert not public_source(path)


def test_reservation_reconciliation_and_week_rollover(context, monkeypatch):
    db, settings, _ = context
    budget = Budget(db, settings)
    budget.plan([], 0)
    a = budget.reserve("generation", 500)
    assert budget.summary()["accounted_tokens"] == 500
    budget.finish(a, "received", 200, {"total_tokens": 100})
    assert budget.summary()["accounted_tokens"] == 100
    db.execute("UPDATE budgets SET attempts_limit=1")
    with pytest.raises(BudgetExhausted):
        budget.reserve("generation", 1)
    monkeypatch.setattr("freeboard.budget.week", lambda: "2099-01-05")
    assert budget.summary()["attempts_used"] == 0


def test_ambiguous_request_is_not_replayed(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    monkeypatch.setattr(runner.opencode, 'generate', lambda *_: (_ for _ in ()).throw(RuntimeError('unknown')))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM jobs")["status"] == "ambiguous"
    assert runner.budget.summary()["accounted_tokens"] > 4096
    runner.db.recover()
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1


def test_retry_evidence_keeps_upstream_unavailability_unscored(context):
    runner,job,m,_=job_context(context)
    evidence=runner.settings.state/'logs'/'failed.json'
    evidence.write_text(json.dumps({'observed_retries':[{'message':'Upstream request failed: Endpoint is unavailable.'}]}))
    attempt=runner.budget.reserve('opencode_generation',1000,job['id'],m['id'])
    runner.budget.finish(attempt,'ambiguous',response_path=str(evidence))
    runner.db.execute("UPDATE jobs SET status='ambiguous' WHERE id=?",(job['id'],))
    runner.db.execute("UPDATE models SET observed_at='2099-01-01T00:00:00+00:00'")
    current=runner.db.one('SELECT * FROM models')
    row,_=make_row(runner.db,runner.settings,current,None,'screen')
    assert row.availability=='model_unavailable' and row.scores is None
    diagnostic=runner.budget.reserve('opencode_access_diagnostic',1000,model_id=m['id'])
    runner.budget.finish(diagnostic,'received',usage={'total_tokens':20})
    row,_=make_row(runner.db,runner.settings,current,None,'screen')
    assert row.availability=='eligible'
    assert runner.db.one('SELECT status,score FROM jobs')['status']=='ambiguous'


def test_saved_response_recovery_without_regeneration(context):
    runner, job, _, _ = job_context(context)
    attempt = runner.budget.reserve("generation", 500, job["id"])
    path = runner.settings.state / "responses" / f"{attempt}.json"
    path.write_text(json.dumps({"protocol": "chat", "body": json.dumps(response_body()), "cap": 4096, "latency": 1}))
    runner.db.recover()
    recovered = runner.db.one("SELECT * FROM jobs")
    assert recovered["status"] == "generated"
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1
    runner.grade(recovered, {"benchmark": "gpqa", "answer": "A"}, "unused")
    assert runner.db.one("SELECT score FROM jobs")["score"] == 1


def test_recovery_after_attempt_commit_before_job_commit(context):
    runner, job, _, _ = job_context(context)
    attempt = runner.budget.reserve("generation", 500, job['id'])
    path = runner.settings.state / "responses" / f"{attempt}.json"
    path.write_text(json.dumps({"protocol": "chat", "body": json.dumps(response_body()), "cap": 4096, "latency": 1}))
    runner.budget.finish(attempt, 'received', 200, response_body()['usage'], str(path))
    assert runner.db.one('SELECT status FROM jobs')['status'] == 'dispatching'
    runner.db.recover()
    assert runner.db.one('SELECT status FROM jobs')['status'] == 'generated'
    assert runner.budget.summary()['reported_tokens'] == 30


def test_explicit_retries_are_bounded(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    def retry(*args):
        import time
        for _ in range(3):
            args[-1]({'next':time.time()*1000+1000})
        pytest.fail('Retry limit must abort the client')
    monkeypatch.setattr(runner.opencode,'generate',retry)
    runner.generate(job, item_model, item, 4096)
    assert len(runner.db.rows("SELECT * FROM attempts")) == 3
    assert runner.db.one("SELECT status FROM jobs")["status"] == "ambiguous"


def test_quota_is_not_a_capability_failure(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    monkeypatch.setattr(runner.opencode,'generate',lambda *_:native_error(429,'Quota limited'))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status,score FROM jobs") == {"status": "deferred", "score": None}
    assert runner.db.one("SELECT status FROM models")["status"] == "quota_limited"


def test_native_free_limit_interruption_stays_unscored_and_visible(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    evidence = runner.settings.state / 'logs' / 'quota-interruption.json'
    evidence.write_text(json.dumps({'observed_retries': [{'message': 'Free usage exceeded',
        'action': {'reason': 'free_tier_limit'}}]}))
    def quota(*_):
        runner.opencode.failure_path = str(evidence)
        raise RuntimeError('Retry backoff exceeds allowance')
    monkeypatch.setattr(runner.opencode, 'generate', quota)
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one('SELECT status,score FROM jobs') == {'status': 'ambiguous', 'score': None}
    assert runner.db.one('SELECT status FROM models')['status'] == 'quota_limited'
    # Fresh pricing evidence cannot erase the last observed quota failure.
    runner.db.execute("UPDATE models SET status='eligible'")
    row, _ = make_row(runner.db, runner.settings, runner.db.one('SELECT * FROM models'), None, 'screen')
    assert row.availability == 'quota_limited' and row.scores is None
    assert len(runner.db.rows('SELECT * FROM attempts')) == 1


@pytest.mark.parametrize('blocker', [None, 'content', 'extra_turn', 'unknown', 'limit', 'wrong_prompt'])
def test_only_proven_quota_rejections_resume_within_original_allowance(context, blocker):
    import sqlite3
    from freeboard.recovery import recover_quota_rejections
    runner, job, item_model, item = job_context(context)
    path = runner.settings.state / 'logs' / 'quota.json'
    path.write_text(json.dumps({'model_id': item_model['id'], 'session_id': 'session',
        'error': 'outcome unknown' if blocker == 'unknown' else 'OpenCode retry allowance exhausted; no automatic replay',
        'observed_retries': [{'action': {'reason': 'free_tier_limit'}, 'next': 1791590400000}]}))
    for _ in range(3 if blocker == 'limit' else 1):
        attempt = runner.budget.reserve('opencode_generation', 1000, job['id'], item_model['id'])
        runner.budget.finish(attempt, 'ambiguous', response_path=str(path))
    runner.db.execute("UPDATE jobs SET status='ambiguous' WHERE id=?", (job['id'],))
    native = runner.settings.state / 'opencode-data-v3/opencode/opencode.db'
    native.parent.mkdir(parents=True)
    with sqlite3.connect(native) as conn:
        conn.executescript('CREATE TABLE message(id TEXT,session_id TEXT,data TEXT); CREATE TABLE part(message_id TEXT,data TEXT);')
        infos = [('user', {'role': 'user', 'agent': 'benchmark',
            'model': {'providerID': 'opencode', 'modelID': item_model['id']}}),
            ('assistant', {'role': 'assistant', 'agent': 'benchmark', 'providerID': 'opencode',
            'modelID': item_model['id'], 'tokens': {'input': 0, 'output': 0, 'reasoning': 0, 'cache': {'read': 0, 'write': 0}}})]
        if blocker == 'extra_turn':
            infos.append(('another', infos[-1][1]))
        conn.executemany('INSERT INTO message VALUES(?,?,?)', [(ident, 'session', json.dumps(info)) for ident, info in infos])
        conn.execute('INSERT INTO part VALUES(?,?)', ('user', json.dumps({'type': 'text',
            'text': 'different question' if blocker == 'wrong_prompt' else item['messages'][-1]['content']})))
        if blocker == 'content':
            conn.execute('INSERT INTO part VALUES(?,?)', ('assistant', json.dumps({'type': 'text', 'text': 'partial answer'})))
    before = runner.budget.summary()['accounted_tokens']
    assert recover_quota_rejections(runner.db) == int(blocker is None)
    assert runner.db.one('SELECT status,score FROM jobs') == {
        'status': 'deferred' if blocker is None else 'ambiguous', 'score': None}
    assert runner.budget.summary()['accounted_tokens'] == before
    assert recover_quota_rejections(runner.db) == 0
    if blocker is None:
        row, _ = make_row(runner.db, runner.settings, item_model, None, 'screen')
        assert row.availability == 'quota_limited' and row.next_retry_at == '2026-10-10T00:00:00+00:00'


@pytest.mark.parametrize('full_season', [False, True])
def test_daily_resumes_validated_public_work_only_when_full_season_is_blocked(context, monkeypatch, full_season):
    from freeboard.cli import daily_work
    db, settings, checkout = context
    active = season(db)
    if not full_season:
        manifest = {**json.loads(active['manifest']), 'partial': True}
        db.execute('UPDATE seasons SET manifest=?,active=0', (json.dumps(manifest),))
    runner = Runner(db, settings, checkout)
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return {'processed': 1} if kwargs else {'blocked': 'Setup incomplete'}
    monkeypatch.setattr(runner, 'run', run)
    monkeypatch.setattr('freeboard.public_run.run_public', lambda r, s: {'processed': 1, 'season': s['id']})
    result = daily_work(runner)
    assert result['blocked'] == 'Setup incomplete'
    assert calls == ([{}] if full_season else [{}, {'pilot': True, 'all_models': True}])
    assert ('public_screen' in result) is not full_season


def test_unavailable_catalog_id_does_not_block_three_completed_full_pilots(context, monkeypatch):
    db, settings, checkout = context
    active = season(db)
    runner = Runner(db, settings, checkout)
    model(db, 'a-unavailable')
    for ident in ['b-free', 'c-free', 'd-free']:
        m = model(db, ident)
        cycle = runner.cycle(m, active, 'pilot', ident)
        db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), cycle['id']))
    monkeypatch.setattr('freeboard.runner.discover', lambda *_: {'ok': True})
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'test-only')
    monkeypatch.setattr(runner, 'schedule', lambda *_: None)
    monkeypatch.setattr(runner, 'capacity', lambda: {})
    monkeypatch.setattr(runner, 'drain', lambda *_, **__: {'processed': 0})
    assert runner.run() == {'processed': 0}
    db.execute("UPDATE cycles SET completed_at=NULL WHERE id='d-free'")
    assert 'blocked' in runner.run()
    # A previous epoch's successful pilot cannot validate the current settings.
    db.execute("UPDATE cycles SET completed_at=?,epoch='old' WHERE id='d-free'", (now(),))
    assert 'blocked' in runner.run()


def test_malformed_success_never_retried(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    monkeypatch.setattr(runner.opencode,'generate',lambda *_:{})
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM jobs")["status"] == "ambiguous"
    assert len(runner.db.rows("SELECT * FROM attempts")) == 1


def test_nested_sampling_is_deterministic_and_held_out():
    pool = [{"id": str(i), "stratum": "a" if i < 60 else "b"} for i in range(120)]
    large = sample(pool, 60, "confirm", equal=True)
    small = sample(large, 20, "screen", equal=True)
    pilot = sample([i for i in pool if i not in large], 2, "pilot", equal=True)
    assert large == sample(list(reversed(pool)), 60, "confirm", equal=True)
    assert {i["id"] for i in small} <= {i["id"] for i in large}
    assert not {i["id"] for i in pilot} & {i["id"] for i in large}


def test_complete_panels_and_paired_draws():
    rows = [{"benchmark": b, "stratum": "x", "item_id": f"{b}:{i}", "score": i % 2, "status": "graded"}
            for b, n in COUNTS["screen"].items() for i in range(n)]
    assert is_complete(rows, "screen")
    assert not is_complete(rows[:-1], "screen")
    a, b = bootstrap(rows), bootstrap(list(reversed(rows)))
    np.testing.assert_array_equal(a["reasoning"], b["reasoning"])
    np.testing.assert_array_equal(a["coding"] - b["coding"], np.zeros(10000))


def public_screen_fixture(context):
    db, settings, checkout = context
    item_model, active = model(db), season(db)
    manifest = json.loads(active['manifest'])
    manifest['partial'] = True
    db.execute('UPDATE seasons SET manifest=?', (json.dumps(manifest),))
    active = db.one('SELECT * FROM seasons')
    for benchmark, n in [('livebench', 20), ('livecodebench', 40)]:
        for i in range(n):
            add_item(db, f'{benchmark}:{i}', benchmark=benchmark)
    return Runner(db, settings, checkout), item_model, active


def test_public_screen_gate_resume_and_no_headline_rank(context, monkeypatch):
    from freeboard.public_run import run_public
    from freeboard.scoring import snapshot
    runner, item_model, active = public_screen_fixture(context)
    monkeypatch.setattr('freeboard.public_run.discover', lambda *_: {'ok': True})
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'test-only')
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: None)
    calls = []
    def response(*args):
        calls.append(args)
        return native_result()
    monkeypatch.setattr(runner.opencode,'generate',response)
    monkeypatch.setattr(runner.grader, 'grade', lambda *_: 1)
    with pytest.raises(ValueError):
        run_public(runner, active, [item_model['id']])
    assert not calls
    pilot = runner.cycle(item_model, active, 'pilot', 'pilot-fixture')
    runner.db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), pilot['id']))
    assert run_public(runner, active, [item_model['id']], limit=1)['processed'] == 1
    assert run_public(runner, active, [item_model['id']])['processed'] == 59
    assert run_public(runner, active, [item_model['id']])['processed'] == 0
    assert len(calls) == 60
    report = snapshot(runner.db, runner.settings)
    assert not any(r.scores for r in report.rows)
    public = report.public_screens[0]
    assert public.status == 'complete' and public.unranked
    assert public.progress == {'gpqa': 0, 'livebench': 20, 'livecodebench': 40}
    assert public.benchmark_scores == {'livebench': 100, 'livecodebench': 100}
    assert public.intervals == {'livebench': [100, 100], 'livecodebench': [100, 100]}
    assert all(public.benchmark_evaluated_at[b] for b in public.benchmark_scores)
    assert report.rows[0].scores is None
    import csv
    import shutil
    target = runner.settings.state.parent / 'public-export'
    shutil.copytree(runner.checkout / 'web', target / 'web')
    export(runner.db, runner.settings, target)
    assert validate_site(target / 'site').public_screens == report.public_screens
    for path in (target / 'site').iterdir():
        assert 'PRIVATE_SENTINEL' not in path.read_text()
    rows = list(csv.DictReader((target / 'site/leaderboard.csv').open()))
    subset = next(r for r in rows if r['tier'] == 'public-screen')
    assert subset['transport'] == 'local-opencode' and subset['reasoning'] == '' and subset['overall'] == ''
    assert subset['livebench_n'] == '20' and subset['livecodebench_n'] == '40'
    assert subset['livebench_evaluated_at'] == public.benchmark_evaluated_at['livebench']
    cycle = runner.db.one("SELECT id FROM cycles WHERE kind='public_screen'")
    runner.db.execute("UPDATE jobs SET status='cap_unverified',error='Combined output cap unverified' WHERE id=(SELECT MIN(id) FROM jobs WHERE cycle_id=?)", (cycle['id'],))
    invalid = snapshot(runner.db, runner.settings).public_screens[0]
    assert invalid.status == 'pending' and not invalid.cap_verified and 'livebench' not in invalid.benchmark_scores
    assert invalid.benchmark_scores['livecodebench'] == 100


def test_grader_failure_preserves_answer_and_finishes_other_questions(context, monkeypatch):
    from freeboard.public_run import run_public
    from freeboard.scoring import snapshot
    runner, item_model, active = public_screen_fixture(context)
    monkeypatch.setattr('freeboard.public_run.discover', lambda *_: {'ok': True})
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'test-only')
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: None)
    monkeypatch.setattr(runner.opencode,'generate',lambda *_:native_result())
    pilot = runner.cycle(item_model, active, 'pilot', 'pilot-fixture')
    runner.db.execute('UPDATE cycles SET completed_at=? WHERE id=?', (now(), pilot['id']))
    calls = 0
    def grade(*_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise GradingUnavailable('Fixture grading failure')
        return 1
    monkeypatch.setattr(runner.grader, 'grade', grade)
    run_public(runner, active, [item_model['id']])
    saved = runner.db.one("SELECT * FROM jobs WHERE status='generated'")
    assert saved['response_path'] and saved['next_after'] and saved['score'] is None
    assert len(runner.db.rows("SELECT * FROM jobs WHERE status='graded'")) == 59
    assert run_public(runner, active, [item_model['id']])['processed'] == 0
    assert len(runner.db.rows('SELECT * FROM attempts')) == 60
    public = snapshot(runner.db, runner.settings).public_screens[0]
    assert public.status == 'pending' and public.pending_reasons == {'grading_blocked': 1}
    assert 'livebench' not in public.benchmark_scores and public.benchmark_scores['livecodebench'] == 100


@pytest.mark.parametrize('archived_revision', [3, 4, 5])
def test_opencode_only_runner_and_export_preserve_archived_records(context, monkeypatch, archived_revision):
    import shutil
    from freeboard.cli import parser
    from freeboard.scoring import PublicScreen, snapshot
    runner, item_model, active = public_screen_fixture(context)
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: None)
    for command in ['pilot', 'run']:
        with pytest.raises(SystemExit) as error:
            parser().parse_args([command, '--local-opencode'])
        assert error.value.code == 2
    # Include pending, saved and completed legacy jobs even on the current epoch.
    # None may be generated, graded, counted in the active queue, or published.
    for kind in ['native_health', 'native_pilot', 'native_public_screen']:
        cycle = runner.cycle(item_model, active, kind, kind)
        runner.add_panel(cycle, 'screen')
        ids = runner.db.rows('SELECT id FROM jobs WHERE cycle_id=? ORDER BY id', (cycle['id'],))
        runner.db.execute("UPDATE jobs SET status='generated' WHERE id=?", (ids[0]['id'],))
        runner.db.execute("UPDATE jobs SET status='graded',score=1 WHERE id=?", (ids[1]['id'],))
    legacy_model = {**item_model, 'profile': json.dumps({'protocol': 'chat', 'cap_verified': True})}
    api_cycle = runner.cycle(legacy_model, active, 'public_screen', 'api-archive')
    runner.add_panel(api_cycle, 'screen')
    previous_profile = {**json.loads(item_model['profile']), 'protocol_revision':archived_revision}
    previous = runner.cycle({**item_model,'profile':json.dumps(previous_profile)}, active, 'public_screen', 'previous-protocol')
    runner.add_panel(previous, 'screen')
    def forbidden(*_):
        pytest.fail('Archived OpenCode jobs must never be executed')
    monkeypatch.setattr(runner, 'generate', forbidden)
    monkeypatch.setattr(runner, 'grade', forbidden)
    assert runner.drain(active)['processed'] == 0
    legacy = runner.budget.reserve('native_generation', 500)
    runner.budget.finish(legacy, 'received', usage={'total_tokens': 100, 'source': 'opencode-normalized'})
    direct = runner.budget.reserve('opencode_generation', 500)
    runner.budget.finish(direct, 'received', usage={'total_tokens': 80})
    report = snapshot(runner.db, runner.settings)
    assert not report.pilots and not report.public_screens and report.queue_size == 0
    assert report.budget['attempts_used'] == 2 and report.budget['accounted_tokens'] == 180
    assert report.budget['opencode_attempts'] == 1 and report.budget['opencode_reported_tokens'] == 80
    assert report.budget['prior_attempts'] == 1 and report.budget['prior_accounted_tokens'] == 100
    with pytest.raises(ValueError):
        PublicScreen.model_validate({'transport': 'zen-api'})
    target = runner.settings.state.parent / 'archive-export'
    shutil.copytree(runner.checkout / 'web', target / 'web')
    export(runner.db, runner.settings, target)
    assert not validate_site(target / 'site').public_screens
    for filename in ['snapshot.json', 'leaderboard.csv']:
        text = (target / 'site' / filename).read_text()
        assert 'zen-api' not in text and 'native_' not in text and 'PRIVATE_SENTINEL' not in text
    assert len(runner.db.rows('SELECT * FROM jobs')) == 300
    assert len(runner.db.rows('SELECT * FROM attempts')) == 2


def test_confirmation_reuses_same_cycle(context):
    db, settings, checkout = context
    item_model, active = model(db), season(db)
    runner = Runner(db, settings, checkout)
    cycle = runner.cycle(item_model, active, "screen", "cycle")
    for i in range(240):
        add_item(db, str(i), tier="confirmation")
        if i < 80:
            add_item(db, str(i), tier="screen")
    runner.add_panel(cycle, "screen")
    db.execute("UPDATE jobs SET status='graded',score=1")
    db.execute("UPDATE cycles SET completed_at=?", (now(),))
    runner.confirm(item_model["id"])
    assert len(db.rows("SELECT * FROM cycles")) == 1
    assert len(db.rows("SELECT * FROM jobs WHERE status='graded'")) == 80
    assert len(db.rows("SELECT * FROM jobs WHERE status='pending'")) == 160


def test_gpqa_final_answer_and_refusal():
    assert gpqa_score("A", "Reasoning\nFINAL: A") == 1
    assert gpqa_score("A", "I refuse") == 0
    assert gpqa_score("A", "FINAL: B") == 0
    assert gpqa_score("A", "The answer could be A or B") == 0


def test_responses_usage_and_truncation():
    data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "answer"}]}],
            "usage": {"input_tokens": 10, "output_tokens": 256, "output_tokens_details": {"reasoning_tokens": 200}}}
    result = parse("responses", data)
    assert result.text == "answer" and result.truncated and result.reasoning_tokens == 200


def test_archived_native_response_accounting_and_validation():
    finish = {'type': 'step_finish', 'part': {'messageID': 'm', 'reason': 'stop', 'cost': 0,
              'tokens': {'total': 7713, 'input': 7637, 'output': 3, 'reasoning': 9, 'cache': {'read': 64, 'write': 0}}}}
    text = {'type': 'text', 'part': {'messageID': 'm', 'text': 'YES'}}
    events = [text, finish]
    result = parse('opencode', {'events': events})
    assert result.text == 'YES' and result.output_tokens == 12
    assert usage_total(result.usage) == 7713
    for invalid in [events + [finish], events + [{'type': 'tool_use'}], events + [{'type': 'error'}]]:
        with pytest.raises(ValueError):
            parse('opencode', {'events': invalid})
    for invalid in [{'type': 'step_finish', 'part': []},
                    {**finish, 'part': {**finish['part'], 'tokens': []}},
                    {**finish, 'part': {**finish['part'], 'tokens': {'cache': []}}}]:
        with pytest.raises(ValueError):
            parse('opencode', {'events': [text, invalid]})


def test_unavailable_provider_stops_model_without_capability_score(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr('freeboard.runner.credential', lambda _: 'public')
    monkeypatch.setattr(runner.opencode,'generate',lambda *_:native_error(400,'Model is unavailable.'))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one('SELECT status FROM models')['status'] == 'model_unavailable'
    assert runner.db.one('SELECT status,score FROM jobs') == {'status': 'deferred', 'score': None}
    assert len(runner.db.rows('SELECT * FROM attempts')) == 1


def test_completed_pilot_keeps_original_health_evidence(context, monkeypatch):
    from freeboard.scoring import snapshot
    db, settings, checkout = context
    monkeypatch.setattr('freeboard.scoring.credential', lambda _: None)
    runner = Runner(db, settings, checkout)
    item_model, active = model(db), season(db)
    health = runner.health(item_model, active)
    db.execute('UPDATE cycles SET started_at=? WHERE id=?', ('2026-10-01T00:00:00+00:00', health['id']))
    db.execute("UPDATE jobs SET status='graded',score=1 WHERE cycle_id=?", (health['id'],))
    pilot = runner.cycle(item_model, active, 'pilot', 'pilot-health-fixture')
    for i, benchmark in enumerate(['livebench', 'livebench', 'livecodebench', 'livecodebench']):
        add_item(db, f'pilot-{i}', 'pilot', benchmark)
    runner.add_panel(pilot, 'pilot')
    db.execute("UPDATE jobs SET status='graded',score=1 WHERE cycle_id=?", (pilot['id'],))
    db.execute('UPDATE cycles SET started_at=?,completed_at=? WHERE id=?',
               ('2026-10-01T01:00:00+00:00', '2026-10-02T00:00:00+00:00', pilot['id']))
    original = snapshot(db, settings).pilots[0]
    assert original.status == 'complete' and original.health_graded == 6 and original.cap_probe_verified
    future = runner.cycle(item_model, active, 'health', 'later-week')
    db.execute('UPDATE cycles SET started_at=? WHERE id=?', ('2026-10-03T00:00:00+00:00', future['id']))
    db.execute("INSERT INTO jobs(cycle_id,item_id,status,score) SELECT ?,item_id,'graded',0 FROM jobs WHERE cycle_id=?",
               (future['id'], health['id']))
    assert snapshot(db, settings).pilots[0] == original


def test_public_export_omits_private_material(context, tmp_path, monkeypatch):
    db, settings, checkout = context
    model(db)
    season(db)
    add_item(db)
    monkeypatch.setattr("freeboard.scoring.credential", lambda _: None, raising=False)
    # The public renderer receives strict schemas, not rows from the private items table.
    public_checkout = tmp_path / "public"
    public_checkout.mkdir()
    import shutil
    shutil.copytree(checkout / "web", public_checkout / "web")
    export(db, settings, public_checkout)
    validate_site(public_checkout / "site")
    for path in (public_checkout / "site").iterdir():
        assert "PRIVATE_SENTINEL" not in path.read_text()
        assert str(settings.state) not in path.read_text()
    content = json.loads((public_checkout / "site/snapshot.json").read_text())
    content["raw_answers"] = ["PRIVATE_SENTINEL"]
    (public_checkout / "site/snapshot.json").write_text(json.dumps(content))
    with pytest.raises(ValueError):
        validate_site(public_checkout / "site")


def test_container_isolation_and_timeout(context, monkeypatch):
    _, settings, _ = context
    grader = DockerGrader(settings)
    import subprocess
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, 180)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr("freeboard.grading.subprocess.run", call)
    with pytest.raises(GradingUnavailable):
        grader.grade({"benchmark": "synthetic_code"}, "code", "sha256:fixture")
    assert "--network=none" in calls[0] and "--read-only" in calls[0]
    assert "--cap-drop=ALL" in calls[0] and "--pids-limit=64" in calls[0]
    assert calls[1][1:3] == ["rm", "--force"]


def test_failed_export_preserves_previous_snapshot(context, tmp_path, monkeypatch):
    db, settings, checkout = context
    import shutil
    public = tmp_path / "public"
    public.mkdir()
    shutil.copytree(checkout / "web", public / "web")
    export(db, settings, public)
    previous = (public / "site/snapshot.json").read_text()
    monkeypatch.setattr("freeboard.publication.validate_site", lambda _: (_ for _ in ()).throw(ValueError("invalid staging")))
    with pytest.raises(ValueError):
        export(db, settings, public)
    assert (public / "site/snapshot.json").read_text() == previous


def test_private_state_rejects_public_checkout(tmp_path):
    with pytest.raises(ValueError):
        Settings(state=tmp_path / "private").initialize(tmp_path)


def test_prepared_manifest_and_panels(context, monkeypatch):
    db, settings, checkout = context
    monkeypatch.setattr('freeboard.panels.credential', lambda _: 'test-only')
    pools = {}
    for benchmark, count, strata in [("gpqa", 198, ["biology", "chemistry", "physics"]),
                                      ("livebench", 100, ["spatial", "zebra_puzzle"]),
                                      ("livecodebench", 600, ["easy", "medium", "hard"])]:
        pools[benchmark] = [{"id": f"{benchmark}:{i}", "benchmark": benchmark,
                             "stratum": strata[i % len(strata)],
                             "messages": [{"role": "user", "content": "PRIVATE_SENTINEL"}]}
                            for i in range(count)]
    monkeypatch.setattr("freeboard.panels.upstream", lambda settings, name: settings.state / name)
    monkeypatch.setattr("freeboard.panels.gpqa", lambda _: pools["gpqa"])
    monkeypatch.setattr("freeboard.panels.livebench", lambda _: pools["livebench"])
    monkeypatch.setattr("freeboard.panels.livecodebench", lambda *_: pools["livecodebench"])
    public = prepare(db, settings, checkout, public_only=True)
    assert public['counts'] == {'screen': 60, 'confirmation': 180, 'pilot': 4}
    public_ids = {r['item_id'] for r in db.rows('SELECT item_id FROM panels WHERE season=?', (public['season'],))}
    monkeypatch.setattr('freeboard.panels.livecodebench', lambda *_: (_ for _ in ()).throw(AssertionError('Unexpected public dataset download')))
    result = prepare(db, settings, checkout)
    assert result['reused_frozen_public_questions']
    assert result["counts"] == {"screen": 80, "confirmation": 240, "pilot": 6}
    full_public_ids = {r['item_id'] for r in db.rows('SELECT p.item_id FROM panels p JOIN items i ON i.id=p.item_id WHERE p.season=? AND i.benchmark!=?', (result['season'], 'gpqa'))}
    assert full_public_ids == public_ids
    def panel(tier):
        return {x["item_id"] for x in db.rows("SELECT item_id FROM panels WHERE tier=?", (tier,))}
    assert panel("screen") <= panel("confirmation")
    assert not panel("pilot") & panel("confirmation")
    manifest = json.loads(db.one("SELECT manifest FROM seasons WHERE id=?", (result['season'],))["manifest"])
    assert set(manifest["grading_files"]) == {"Dockerfile", "worker.py", "requirements.txt"}
    # A grader fix must preserve the frozen questions and hold-out separation.
    import shutil
    replacement = settings.state / 'replacement-source'
    shutil.copytree(checkout / 'grading', replacement / 'grading')
    with (replacement / 'grading/Dockerfile').open('a') as handle:
        handle.write('\n# Grader revision fixture\n')
    monkeypatch.setattr('freeboard.panels.livecodebench', lambda *_: (_ for _ in ()).throw(AssertionError('Unexpected resampling')))
    updated = prepare(db, settings, replacement)
    assert updated['season'] != result['season']
    assert updated['reused_frozen_questions']
    old = {x['item_id'] for x in db.rows('SELECT item_id FROM panels WHERE season=?', (result['season'],))}
    new = {x['item_id'] for x in db.rows('SELECT item_id FROM panels WHERE season=?', (updated['season'],))}
    assert old == new
    assert not db.rows('SELECT * FROM cycles WHERE season=?', (updated['season'],))
    # Public canonical IDs remain stable even when the frozen rows came from a prior season.
    from freeboard.scoring import records
    runner = Runner(db, settings, checkout)
    cycle = runner.cycle(model(db), db.one('SELECT * FROM seasons WHERE id=?', (updated['season'],)), 'screen', 'canonical')
    runner.add_panel(cycle, 'screen')
    assert {r['item_id'] for r in records(db, cycle, 'screen')} == {i['id'] for i in manifest['panels']['screen']}


def test_reported_cap_overrun_blocks_future_generation(context, monkeypatch):
    runner, job, item_model, item = job_context(context)
    monkeypatch.setattr("freeboard.runner.credential", lambda _: "test-only")
    monkeypatch.setattr(runner.opencode,'generate',lambda *_:native_result(tokens=5000))
    runner.generate(job, item_model, item, 4096)
    assert runner.db.one("SELECT status FROM models")["status"] == "cap_violation"
    assert runner.budget.summary()["reported_tokens"] == 5020
