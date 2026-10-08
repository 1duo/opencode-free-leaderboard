"""Explicitly unranked pilots through the user's real OpenCode client.

Native system/environment context differs from the headline protocol. Native
answers have separate cycles and are never reused in a screen or confirmation.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from .adapters import parse
from .budget import estimate
from .config import digest, now
from .discovery import discover
from .runner import PROBES
from .public_run import plan_public_screen, validate_public_screen


def run_native(runner, season: dict, selected: list[str] | None = None, limit: int | None = None, screen=False) -> dict:
    if screen:
        validate_public_screen(runner, season)
    runner.db.recover()
    discovered = discover(runner.db, runner.budget, runner.client)
    if not discovered['ok']:
        return {'blocked': discovered['error']}
    models = {m['id']: m for m in runner.models()}
    selected = sorted(set(selected)) if selected else sorted(models)[:3]
    if any(m not in models or models[m]['protocol'] != 'chat' for m in selected):
        raise ValueError('Native pilot model lacks verified free Chat Completions eligibility')
    binary = shutil.which('opencode') or str(Path.home() / '.opencode/bin/opencode')
    directory = runner.settings.state / 'opencode-session'
    directory.mkdir(mode=0o700, exist_ok=True)
    version = subprocess.run([binary, '--version'], cwd=directory, capture_output=True,
                             text=True, timeout=15, check=True).stdout.strip()
    summary, processed = [], 0
    for ident in selected:
        config = {'model': f'opencode/{ident}', 'small_model': f'opencode/{ident}',
                  'enabled_providers': ['opencode'], 'autoupdate': False, 'share': 'disabled',
                  'permission': {'*': 'deny'}, 'compaction': {'auto': False, 'prune': False},
                  'provider': {'opencode': {'whitelist': [ident], 'models': {ident: {
                      'name': ident, 'limit': {'context': 200000, 'output': 4096}}}}},
                  'agent': {'benchmark': {'mode': 'primary', 'temperature': 0, 'steps': 1,
                      'prompt': 'Follow the supplied evaluation instructions exactly. Use no tools.',
                      'permission': {'*': 'deny'}}, 'title': {'disable': True},
                      'summary': {'disable': True}, 'compaction': {'disable': True}}}
        profile = {'protocol': 'opencode', 'transport': 'native-cli', 'version': version,
                   'configuration_hash': digest(config), 'cap_verified': False,
                   'headline_eligible': False, 'extra_system_context': True}
        model = {**models[ident], 'profile': json.dumps(profile), 'epoch': digest([ident, profile])[:16]}
        if screen and not runner.db.one("SELECT id FROM cycles WHERE model_id=? AND epoch=? AND season=? AND kind='native_pilot' AND completed_at IS NOT NULL",
                                        (ident, model['epoch'], season['id'])):
            summary.append({'model': ident, 'headline_eligible': False, 'blocked': True,
                            'reason': 'Matching native compatibility pilot is incomplete', 'graded': 0})
            continue
        cycles = []
        panel_kind = 'native_public_screen' if screen else 'native_pilot'
        for kind in ['native_health', panel_kind]:
            cycle = runner.cycle(model, season, kind, kind + ':' + digest([ident, model['epoch'], season['id']])[:24])
            if kind == panel_kind:
                runner.add_panel(cycle, 'screen' if screen else 'pilot')
                if screen:
                    plan_public_screen(runner, cycle)
            else:
                for probe in PROBES:
                    item = {'benchmark': 'health', **probe, 'messages': [{'role': 'user', 'content': probe['prompt']}]}
                    item_id = 'health:' + digest(probe)
                    runner.db.execute('INSERT OR IGNORE INTO items VALUES(?,?,?,?,?,?)',
                        (item_id, None, 'health', probe['stratum'], digest(item), json.dumps(item)))
                    runner.db.execute('INSERT OR IGNORE INTO jobs(cycle_id,item_id) VALUES(?,?)', (cycle['id'], item_id))
            cycles.append(cycle)
        blocked = False
        for cycle in cycles:
            for job in runner.db.rows('SELECT j.*,i.content FROM jobs j JOIN items i ON i.id=j.item_id WHERE j.cycle_id=? ORDER BY j.id', (cycle['id'],)):
                if job['status'] == 'graded':
                    continue
                if job['status'] not in {'pending', 'generated'} or (limit is not None and processed >= limit):
                    blocked = True
                    break
                item = json.loads(job['content'])
                cap = 256 if cycle['kind'] == 'native_health' else 4096
                if job['status'] == 'pending':
                    attempt = runner.budget.reserve('native_generation', estimate(item['messages'], cap) + 8192,
                                                    job['id'], ident)
                    started = time.monotonic()
                    env = {**os.environ, 'OPENCODE_CONFIG_CONTENT': json.dumps(config),
                           'OPENCODE_DISABLE_EXTERNAL_SKILLS': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                           'OPENCODE_AUTO_SHARE': 'false', 'OPENCODE_EXPERIMENTAL': 'false',
                           'OPENCODE_ENABLE_PARALLEL': 'false',
                           'OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS': 'false',
                           'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': str(cap)}
                    command = [binary, 'run', '--format', 'json', '--model', f'opencode/{ident}',
                               '--agent', 'benchmark', '--title', 'Freeboard unranked native pilot']
                    prompt = '\n\n'.join(m['content'] for m in item['messages'])
                    path = runner.settings.state / 'responses' / f'{attempt}.json'
                    try:
                        result = subprocess.run(command, input=prompt, cwd=directory, env=env,
                                                capture_output=True, text=True, timeout=180)
                    except subprocess.TimeoutExpired as exc:
                        unknown = path.with_suffix('.unknown.json')
                        unknown.write_text(json.dumps({'stdout': str(exc.stdout or ''), 'stderr': str(exc.stderr or ''),
                                                       'outcome': 'unknown', 'headline_eligible': False}))
                        runner.budget.finish(attempt, 'ambiguous', response_path=str(unknown))
                        runner.db.execute("UPDATE jobs SET status='ambiguous',error='Native outcome unknown; no replay' WHERE id=?", (job['id'],))
                        blocked = True
                        break
                    events = []
                    for line in result.stdout.splitlines():
                        try:
                            events.append(json.loads(line))
                        except ValueError:
                            continue
                    record = {'protocol': 'opencode', 'body': json.dumps({'events': events}),
                              'cap': cap, 'latency': time.monotonic() - started,
                              'stderr': result.stderr, 'returncode': result.returncode,
                              'configuration': config, 'headline_eligible': False}
                    temporary = path.with_suffix('.partial')
                    temporary.write_text(json.dumps(record))
                    with temporary.open('rb') as handle:
                        os.fsync(handle.fileno())
                    temporary.replace(path)
                    directory_fd = os.open(path.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                    try:
                        completion = parse('opencode', {'events': events})
                        if result.returncode or 'Falling back to default agent' in result.stderr or not completion.bounded(cap):
                            raise ValueError('Native protocol/cap unverified')
                    except (ValueError, KeyError, TypeError):
                        runner.budget.finish(attempt, 'native_unverified', response_path=str(path))
                        runner.db.execute("UPDATE jobs SET status='ambiguous',response_path=?,error='Native protocol/access/cap unverified; no replay' WHERE id=?", (str(path), job['id']))
                        blocked = True
                        break
                    runner.budget.finish(attempt, 'received', usage=completion.usage, response_path=str(path))
                    runner.db.execute("UPDATE jobs SET status='generated',response_path=?,latency=?,truncated=? WHERE id=?",
                                      (str(path), record['latency'], int(completion.truncated), job['id']))
                    job = runner.db.one('SELECT * FROM jobs WHERE id=?', (job['id'],))
                runner.grade(job, item, json.loads(season['manifest'])['grader_image'])
                processed += 1
            if blocked:
                break
            if cycle['kind'] == 'native_health':
                values = runner.db.rows('SELECT j.*,i.content FROM jobs j JOIN items i ON i.id=j.item_id WHERE cycle_id=?', (cycle['id'],))
                # Every saved completion already passed the observed combined bound.
                # No observed truncation means the cap semantics remain unverified;
                # this explicitly unranked experiment may still measure its samples.
                cap_probe = any(json.loads(j['content']).get('check') == 'cap' and j['score'] == 1 for j in values)
                health_profile = {**profile, 'cap_probe_verified': cap_probe}
                runner.db.execute('UPDATE cycles SET profile=? WHERE id=?', (json.dumps(health_profile), cycle['id']))
            runner.db.execute('UPDATE cycles SET completed_at=COALESCE(completed_at,?) WHERE id=?', (now(), cycle['id']))
        summary.append({'model': ident, 'headline_eligible': False, 'blocked': blocked,
                        'graded': runner.db.one("SELECT COUNT(*) n FROM jobs j JOIN cycles c ON c.id=j.cycle_id WHERE c.epoch=? AND j.status='graded'", (model['epoch'],))['n']})
    return {'transport': 'local-opencode', 'version': version, 'unranked': True,
            'processed': processed, 'models': summary, 'budget': runner.budget.summary()}
