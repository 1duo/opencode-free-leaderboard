import json
from types import SimpleNamespace

from freeboard.coding import select_panel
from freeboard.config import Settings, now
from freeboard.db import DB
from freeboard.scoring import CapabilityResult, CodingEvaluation, expand_coding, expanded_comparisons


def test_panel_is_deterministic_disjoint_and_rejects_unsupported_dependencies():
    rows = []
    for index in range(50):
        rows.append({'task_id':f'BigCodeBench/{index}', 'complete_prompt':
            ('import numpy\n' if index%2 else '')+'def task_func(x):\n',
            'canonical_solution':'    return x\n', 'test':'import unittest\n',
            'entry_point':'task_func','instruct_prompt':'Synthetic identity fixture.',
            'code_prompt':'def task_func(x):'})
    rows.append({**rows[0], 'task_id':'BigCodeBench/unsupported', 'complete_prompt':'import tensorflow\ndef task_func(x):\n'})
    first, second = select_panel(rows), select_panel(list(reversed(rows)))
    assert first == second
    assert len(first['coding'])==24 and len(first['coding_pilot'])==2
    assert {r['id'] for r in first['coding']}.isdisjoint(r['id'] for r in first['coding_pilot'])
    assert {r['stratum'] for r in first['coding']}=={'stdlib','scientific'}
    assert all('unsupported' not in r['id'] for values in first.values() for r in values)
    assert all(r['messages'][-1]['content']=='Synthetic identity fixture.' for values in first.values() for r in values)


def expansion_fixture(tmp_path):
    state = tmp_path/'state'
    state.mkdir()
    db = DB(state)
    stamp=now()
    for season,benchmark,count in [('base','livecodebench',40),('extension','bigcodebench',24)]:
        panel=[]
        for index in range(count):
            ident=f'{benchmark}:synthetic:{index}'
            panel.append({'id':ident,'hash':'a'*64,'benchmark':benchmark,'stratum':'easy'})
        manifest={'panels':{'screen' if season=='base' else 'coding':panel}}
        db.execute('INSERT INTO seasons VALUES(?,?,?,?,?)',(season,stamp,json.dumps(manifest),1,0))
        for item in panel:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?)',(item['id'],season,benchmark,'easy',item['hash'],'{}'))
            db.execute('INSERT INTO panels VALUES(?,?,?)',(season,'screen' if season=='base' else 'coding',item['id']))
    caps, models, evaluations = [],[],[]
    for model,base_correct,extra_correct in [('a',40,0),('b',20,24)]:
        models.append(SimpleNamespace(model_id=model,tier='screen',availability='eligible',cap_verified=True))
        caps.extend([CapabilityResult(model_id=model,epoch='epoch',category='reasoning',score=75),
            CapabilityResult(model_id=model,epoch='epoch',category='coding',source_tier='screen',season='base',
                score=base_correct/40*100,rank=1 if model=='a' else 2,sample_counts={'livecodebench':40},evaluated_at=stamp)])
        for season,kind,tier,count,correct,benchmark in [('base','screen','screen',40,base_correct,'livecodebench'),
                ('extension','coding','coding',24,extra_correct,'bigcodebench')]:
            cycle=model+season
            db.execute('INSERT INTO cycles(id,model_id,epoch,season,kind,started_at,due_at,completed_at,profile) VALUES(?,?,?,?,?,?,?,?,?)',
                       (cycle,model,'epoch',season,kind,stamp,stamp,stamp,'{}'))
            for index in range(count):
                db.execute('INSERT INTO jobs(cycle_id,item_id,status,score) VALUES(?,?,?,?)',
                           (cycle,f'{benchmark}:synthetic:{index}','graded',int(index<correct)))
        evaluations.append(CodingEvaluation(model_id=model,epoch='epoch',season='extension',status='complete',
            progress=24,score=extra_correct/24*100,evaluated_at=stamp,started_at=stamp))
    return db,Settings(state=state),caps,models,evaluations


def test_complete_expansion_equal_weights_and_reasoning_unchanged(tmp_path):
    db,settings,caps,models,evaluations=expansion_fixture(tmp_path)
    try:
        merged,weights=expand_coding(db,settings,caps,models,evaluations)
        assert weights=={'livecodebench':0.5,'bigcodebench':0.5}
        coding={r.model_id:r for r in merged if r.category=='coding'}
        assert coding['a'].score==50 and coding['b'].score==75
        assert coding['b'].rank==1 and coding['a'].rank==2
        assert coding['a'].sample_counts=={'livecodebench':40,'bigcodebench':24}
        assert all(0<=r.interval[0]<=r.score<=r.interval[1]<=100 for r in coding.values())
        assert [r for r in merged if r.category=='reasoning']==[r for r in caps if r.category=='reasoning']
        comparison=expanded_comparisons(db,settings,merged)[0]
        assert comparison.difference==-25 and comparison.view=='coding' and comparison.tier=='coding-expanded'
        assert comparison.interval[0]<=-25<=comparison.interval[1]
    finally:
        db.conn.close()


def test_coding_result_pr_recomputes_only_coding_and_rejects_fractional_grades():
    import pytest
    from test_contributions import submission_fixture
    from freeboard.submissions import ResultSubmission, fingerprint, validate_submission
    manifest,record=submission_fixture()
    items=[{'id':f'bigcodebench:synthetic:{i}','hash':'a'*64,'benchmark':'bigcodebench',
            'stratum':'stdlib' if i<12 else 'scientific'} for i in range(24)]
    manifest.update(id='coding-'+'a'*16, panels={'coding':items},counts={'coding':{'bigcodebench':24}})
    data=record.model_dump(mode='json')
    data.update(season=manifest['id'],tier='coding',panel_fingerprint=fingerprint(manifest,'coding'),
        gpqa_privacy_verified=False,questions=[{**q.model_dump(),'id':item['id'],'hash':item['hash']}
            for q,item in zip(record.questions,items)])
    result=validate_submission(ResultSubmission.model_validate(data),[manifest])
    assert result.scores=={'coding':50} and result.sample_counts=={'bigcodebench':24}
    data['questions'][0]['score']=0.5
    with pytest.raises(ValueError,match='binary'):
        validate_submission(ResultSubmission.model_validate(data),[manifest])


def test_exhausted_budget_can_revalidate_before_new_coding_session(tmp_path):
    from freeboard.budget import Budget
    from freeboard.coding import plan_coding_capacity
    from freeboard.config import week
    db,settings,*_=expansion_fixture(tmp_path)
    try:
        budget=Budget(db,settings)
        budget.plan([],0)
        budget.reserve('synthetic',settings.minimum_tokens)
        db.execute('UPDATE budgets SET attempts_limit=1 WHERE week=?',(week(),))
        db.execute('UPDATE items SET content=?', (json.dumps({'messages':[{'role':'user','content':'Synthetic fixture.'}]}),))
        runner=SimpleNamespace(db=db,budget=budget)
        plan_coding_capacity(runner,{'id':'extension'},2)
        attempt=budget.reserve('discovery')
        assert attempt and budget.summary()['attempts_limit']>=budget.summary()['planned_attempts']
        assert budget.reserve('synthetic-generation',14000)
    finally:
        db.conn.close()


def test_incomplete_expansion_keeps_existing_scores_and_ranks(tmp_path):
    db,settings,caps,models,evaluations=expansion_fixture(tmp_path)
    try:
        merged,weights=expand_coding(db,settings,caps,models,evaluations[:1])
        assert merged==caps and weights=={'livecodebench':1.0}
    finally:
        db.conn.close()


def test_promoted_policy_does_not_mix_legacy_scores_when_new_model_pending(tmp_path):
    db,settings,caps,models,evaluations=expansion_fixture(tmp_path)
    try:
        manifest=json.loads(db.one('SELECT manifest FROM seasons WHERE id=?',('extension',))['manifest'])
        manifest['coding_promoted_at']=now()
        db.execute('UPDATE seasons SET manifest=? WHERE id=?',(json.dumps(manifest),'extension'))
        merged,weights=expand_coding(db,settings,caps,models,evaluations[:1])
        assert weights['bigcodebench']==0.5
        pending=next(r for r in merged if r.model_id=='b' and r.category=='coding')
        assert pending.score is None and pending.rank is None and pending.refresh_pending
    finally:
        db.conn.close()
