"""Resume a proven quota rejection, never an unknown generation outcome."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .discovery import excluded


def unanswered_continuation(journal: dict, answers: list[dict], model: dict) -> bool:
    """Recognize a broken reasoning stream followed by an empty SDK continuation."""
    if (len(answers) != 2 or journal.get('assistant_turns_observed') != 2
            or journal.get('error') != 'OpenCode attempted another assistant turn; no replay'
            or journal.get('observed_retries') or journal.get('denied_permissions')):
        return False

    def zero_usage(tokens):
        return (isinstance(tokens, dict)
                and all(type(tokens.get(k)) is int and tokens[k] == 0 for k in ['input', 'output', 'reasoning'])
                and type(tokens.get('total', 0)) is int and tokens.get('total', 0) == 0
                and isinstance(tokens.get('cache'), dict)
                and all(type(value) is int and value == 0 for value in tokens['cache'].values()))

    first, second = answers
    for answer in answers:
        info = answer['info']
        if (info.get('agent') != 'benchmark' or info.get('providerID') != 'opencode'
                or info.get('modelID') != model['id'] or info.get('cost') != 0
                or info.get('error') is not None or not zero_usage(info.get('tokens'))):
            return False
    finishes = [p for p in first['parts'] if p.get('type') == 'step-finish']
    return (first['info'].get('finish') == 'unknown' and len(finishes) == 1
            and finishes[0].get('reason') == 'unknown' and finishes[0].get('cost') == 0
            and zero_usage(finishes[0].get('tokens'))
            and all(p.get('type') in {'step-start', 'reasoning', 'step-finish'} for p in first['parts'])
            and second['info'].get('finish') is None and not second['parts'])


def manual_retry_reason(db, job: dict, model: dict, additional_retry: bool = False) -> str | None:
    """Inspect unanswered evidence only; never select a retry by answer quality."""
    if job['status'] != 'ambiguous' or job['response_path']:
        return 'Request is not an unanswered unknown outcome'
    original = db.one('SELECT job_id FROM manual_retries WHERE job_id=?', (job['id'],))
    if additional_retry and not original:
        return 'Additional recovery requires an existing original manual retry'
    if (original and not additional_retry) or db.one('SELECT job_id FROM manual_retry_extensions WHERE job_id=?', (job['id'],)):
        return 'One-time manual retry already consumed'
    cycle = db.one('SELECT * FROM cycles WHERE id=?', (job['cycle_id'],))
    if excluded(model['id']) or model['status'] != 'eligible' or not json.loads(model['profile']).get('cap_verified'):
        return 'Current free eligibility and output cap are required'
    if cycle['epoch'] != model['epoch']:
        return 'Evaluation configuration changed'
    attempts = db.rows("SELECT * FROM attempts WHERE job_id=? AND kind='opencode_generation' ORDER BY id", (job['id'],))
    if (not attempts or attempts[-1]['status'] != 'ambiguous'
            or any(a['status'] in {'received', 'recovered'} for a in attempts)):
        return 'An accepted response or non-unknown attempt exists'
    try:
        journal = json.loads(Path(attempts[-1]['response_path']).read_text())
        transcript = native_transcript(db.state, journal['session_id'])
        user = next(m for m in transcript if m['info'].get('role') == 'user')
        answers = [m for m in transcript if m['info'].get('role') == 'assistant']
        item = json.loads(db.one('SELECT content FROM items WHERE id=?', (job['item_id'],))['content'])
        variant = json.loads(model['profile'])['reasoning']['variant']
        interrupted = (len(transcript) == 2 and len(answers) == 1
                       and answers[0]['info'].get('finish') is None
                       and not any(p.get('type') in {'step-finish', 'tool'} for p in answers[0]['parts']))
        continued = len(transcript) == 3 and unanswered_continuation(journal, answers, model)
        if (not (interrupted or continued) or journal.get('model_id') != model['id']
                or user['info'].get('agent') != 'benchmark'
                or user['info'].get('model') != {'providerID': 'opencode', 'modelID': model['id'],
                    **({'variant': variant} if variant else {})}
                or user['info'].get('system', '') != '\n\n'.join(m['content'] for m in item['messages'][:-1])
                or [p.get('text') for p in user['parts'] if p.get('type') == 'text' and not p.get('synthetic')]
                    != [item['messages'][-1]['content']]):
            return 'Native evidence does not establish an unanswered matching request'
    except (OSError, ValueError, KeyError, TypeError, StopIteration, sqlite3.Error):
        return 'Native interruption evidence could not be verified'
    return None


def quota_retry_at(journal: dict, transcript: list[dict], model: dict, item: dict) -> str | None:
    retries = journal.get('observed_retries') or []
    if (journal.get('model_id') != model['id'] or
            journal.get('error') != 'OpenCode retry allowance exhausted; no automatic replay' or
            journal.get('assistant_turns_observed', 1) not in {None, 1} or
            not retries or len(transcript) != 2):
        return None
    # The native free-tier action is an explicit rejection before generation.
    # Generic stream errors and missing responses cannot establish that fact.
    if any(r.get('action', {}).get('reason') != 'free_tier_limit' for r in retries):
        return None
    retry = retries[-1]
    if type(retry.get('next')) not in {int, float}:
        return None
    user = next((m for m in transcript if m['info'].get('role') == 'user'), None)
    answer = next((m for m in transcript if m['info'].get('role') == 'assistant'), None)
    if not user or not answer:
        return None
    profile = json.loads(model['profile'])
    if (user['info'].get('model') != {'providerID': 'opencode', 'modelID': model['id'],
            **({'variant': profile['reasoning']['variant']} if profile['reasoning']['variant'] else {})} or
            user['info'].get('agent') != 'benchmark' or
            [p.get('text') for p in user['parts'] if p.get('type') == 'text'] != [item['messages'][-1]['content']]):
        return None
    info = answer['info']
    tokens = info.get('tokens') or {}
    if (info.get('agent') != 'benchmark' or info.get('providerID') != 'opencode' or
            info.get('modelID') != model['id'] or info.get('finish') is not None or
            info.get('error') is not None or answer['parts'] or
            any(tokens.get(k) != 0 for k in ['input', 'output', 'reasoning']) or
            any(v != 0 for v in tokens.get('cache', {}).values()) or tokens.get('total', 0) != 0):
        return None
    try:
        return datetime.fromtimestamp(retry['next'] / 1000, timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


def native_transcript(state: Path, session: str) -> list[dict]:
    path = state / 'opencode-data-v3/opencode/opencode.db'
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as connection:
        return [{'info': json.loads(data), 'parts': [json.loads(p) for (p,) in connection.execute(
            'SELECT data FROM part WHERE message_id=?', (ident,))]}
            for ident, data in connection.execute('SELECT id,data FROM message WHERE session_id=?', (session,))]


def recover_quota_rejections(db) -> int:
    recovered = 0
    for job in db.rows("""SELECT j.*,c.model_id,c.epoch FROM jobs j JOIN cycles c ON c.id=j.cycle_id
        JOIN models m ON m.id=c.model_id AND m.epoch=c.epoch WHERE j.status='ambiguous'
        AND j.response_path IS NULL AND m.status='eligible'"""):
        attempts = db.rows("SELECT * FROM attempts WHERE job_id=? AND kind='opencode_generation' ORDER BY id", (job['id'],))
        if not attempts or len(attempts) >= 3 or any(a['status'] in {'received', 'recovered'} for a in attempts):
            continue
        latest = attempts[-1]
        if latest['status'] != 'ambiguous' or not latest['response_path']:
            continue
        try:
            journal = json.loads(Path(latest['response_path']).read_text())
            transcript = native_transcript(db.state, journal['session_id'])
            model = db.one('SELECT * FROM models WHERE id=?', (job['model_id'],))
            item = json.loads(db.one('SELECT content FROM items WHERE id=?', (job['item_id'],))['content'])
            retry_at = quota_retry_at(journal, transcript, model, item)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            continue
        if not retry_at:
            continue
        # Retain the original reservation and journal. A later dispatch must
        # reserve a new attempt and remains subject to the same three-attempt limit.
        audit = json.dumps({'source': 'verified-native-quota-rejection', 'session_id': journal['session_id'],
                            'retry_at': retry_at, 'no_response_parts': True})
        with db.conn:
            db.conn.execute("UPDATE attempts SET status='quota_limited',usage=? WHERE id=?", (audit, latest['id']))
            db.conn.execute("UPDATE jobs SET status='deferred',next_after=?,error='Verified quota rejection; awaiting retry window' WHERE id=?",
                            (retry_at, job['id']))
        recovered += 1
    return recovered
