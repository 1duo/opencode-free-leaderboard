"""One fresh, tool-free session through the installed OpenCode client."""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

from .config import ZEN_BASE, credential, digest
from .discovery import excluded

VERSION = '1.18.31'
REVISION = 6  # Highest exposed reasoning variant; frozen one-turn permission protocol.
SYSTEM = 'Follow the supplied benchmark instructions exactly. Answer once. Do not use tools.'
TOOLS = ('bash', 'read', 'glob', 'grep', 'edit', 'write', 'apply_patch')
PERMISSIONS = {'*': 'deny', **{name: 'ask' for name in TOOLS}, 'external_directory': 'ask'}
EFFORTS = ('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max')


def reasoning_policy(model: dict | None) -> dict:
    """Select only controls advertised by the installed client; never infer alias identity."""
    if model is None:
        return {'mode': 'unverified', 'variant': None, 'options': {}, 'available_variants': []}
    variants = model.get('variants') or {}
    if not isinstance(variants, dict) or any(k not in (*EFFORTS, 'thinking') for k in variants):
        raise ValueError('Unrecognized reasoning controls require validation')
    enabled = [k for k in (*EFFORTS[1:], 'thinking') if k in variants and variants[k]]
    if variants and not enabled:
        raise ValueError('No enabled reasoning variant is advertised')
    # Effort variants follow the explicit order above. Toggle-only clients expose thinking.
    variant = next((k for k in reversed(EFFORTS[1:]) if k in enabled), 'thinking' if enabled else None)
    if variant and not model['capabilities'].get('reasoning'):
        raise ValueError('Reasoning capability and variants disagree')
    return {'mode': 'highest-exposed' if variant else 'provider-managed', 'variant': variant,
            'options': variants[variant] if variant else {}, 'available_variants': sorted(variants),
            'definition_hash': digest({k: model[k] for k in ['api', 'capabilities', 'options', 'variants']})}


def client_version(state) -> str:
    binary = shutil.which('opencode') or str(state.home() / '.opencode/bin/opencode')
    try:
        result = subprocess.run([binary, '--version'], cwd=state, capture_output=True, text=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        raise RuntimeError('The installed OpenCode client could not be inspected') from None
    if result.stdout.strip() != VERSION:
        raise RuntimeError(f'OpenCode {VERSION} is required; validate a changed client before evaluating')
    return VERSION


def configuration(ident: str, cap: int) -> dict:
    if excluded(ident) or cap not in {256, 4096}:
        raise ValueError('Excluded model or unsupported output limit')
    return {'model': f'opencode/{ident}', 'small_model': f'opencode/{ident}',
            'enabled_providers': ['opencode'], 'autoupdate': False, 'share': 'disabled',
            'permission': dict(PERMISSIONS), 'compaction': {'auto': False, 'prune': False},
            'experimental': {'continue_loop_on_deny': False},
            'provider': {'opencode': {'whitelist': [ident], 'models': {
                ident: {'limit': {'context': 200000, 'output': cap}}}}},
            'agent': {'benchmark': {'mode': 'primary', 'temperature': 0,
                'prompt': SYSTEM, 'permission': dict(PERMISSIONS)},
                'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'disable': True}}}


def profile(endpoint_protocol: str, reasoning: dict | None = None) -> dict:
    return {'protocol': 'opencode', 'transport': 'local-opencode', 'endpoint_protocol': endpoint_protocol,
            'version': VERSION, 'protocol_revision': REVISION, 'temperature': 0,
            'configuration_hash': digest(configuration('MODEL', 4096)),
            'tool_schemas': list(TOOLS), 'tool_policy': 'reject-and-score-zero',
            'extra_system_context': True, 'cap_verified': False,
            'reasoning': reasoning or reasoning_policy(None)}


def prompt_body(model: dict, messages: list[dict]) -> dict:
    settings = json.loads(model['profile'])
    endpoints = {'chat': f'{ZEN_BASE}/chat/completions', 'responses': f'{ZEN_BASE}/responses'}
    if (model['status'] != 'eligible' or excluded(model['id']) or
            model['endpoint'] != endpoints.get(settings.get('endpoint_protocol')) or
            settings.get('transport') != 'local-opencode' or settings.get('protocol_revision') != REVISION or
            settings.get('reasoning', {}).get('mode') not in {'highest-exposed', 'provider-managed'}):
        raise ValueError('Model lacks verified free eligibility for the current OpenCode protocol')
    if not messages or messages[-1]['role'] != 'user' or any(m['role'] != 'system' for m in messages[:-1]):
        raise ValueError('Benchmark must contain system instructions followed by one user question')
    body = {'agent': 'benchmark', 'model': {'providerID': 'opencode', 'modelID': model['id']},
            'system': '\n\n'.join(m['content'] for m in messages[:-1]),
            'parts': [{'type': 'text', 'text': messages[-1]['content']}]}
    if settings['reasoning']['variant']:
        body['variant'] = settings['reasoning']['variant']
    return body


def completion_events(result: dict, ident: str) -> list[dict]:
    if result.get('assistant_turns', 1) != 1:
        raise ValueError('OpenCode produced multiple turns')
    info = result.get('info', {})
    if not isinstance(info, dict) or not isinstance(result.get('parts'), list):
        raise ValueError('Malformed OpenCode message')
    tools = sum(isinstance(p, dict) and p.get('type') == 'tool' for p in result['parts'])
    if result.get('tool_parts', tools) != tools:
        raise ValueError('OpenCode transcript and tool count disagree')
    if (info.get('role') != 'assistant' or info.get('providerID') != 'opencode' or
            info.get('modelID') != ident or info.get('agent') != 'benchmark' or info.get('cost') != 0):
        raise ValueError('OpenCode model, agent, or zero-cost completion could not be verified')
    if info.get('error') and info['error'].get('name') != 'ContentFilterError':
        raise ValueError('OpenCode returned a provider error')
    events = []
    for part in result.get('parts', []):
        if not isinstance(part, dict):
            raise ValueError('Malformed OpenCode message part')
        if part.get('messageID') != info['id']:
            raise ValueError('Unexpected message in OpenCode response')
        if part['type'] == 'tool':
            state = part.get('state', {})
            if (part.get('tool') not in TOOLS or state.get('status') != 'error' or
                    'rejected permission' not in state.get('error', '').lower() or
                    not result.get('denied_permissions')):
                raise ValueError('Tool execution or rejection could not be verified')
        if part['type'] in {'text', 'step-finish'}:
            events.append({'type': 'step_finish' if part['type'] == 'step-finish' else 'text', 'part': part})
    return events


def failure_status(result: dict) -> tuple[str, str]:
    error = result.get('info', {}).get('error') or result.get('error') or {}
    data = error.get('data') or {}
    text = str(data.get('message', '')) + str(data.get('responseBody', ''))
    code = data.get('statusCode')
    if 'FreeTierError' in text or 'free tier can only' in text.lower():
        return 'client_access_restricted', 'Provider restricts free-tier access under this OpenCode configuration'
    if code in {401, 403}:
        return 'authentication_failed', 'Provider access rejected'
    if code == 429:
        return 'quota_limited', 'Provider quota limited'
    if any(s in text.lower() for s in ['model is unavailable', 'model not found', 'deprecated']):
        return 'model_unavailable', 'Provider reports this model unavailable or deprecated'
    return 'configuration_error', 'OpenCode response or client configuration could not be verified'


def interruption_status(evidence: dict) -> str:
    retries = evidence.get('observed_retries', [])
    messages = ' '.join(r.get('message', '') for r in retries).lower()
    if any(r.get('action', {}).get('reason') == 'free_tier_limit' for r in retries):
        return 'quota_limited'
    if any(s in messages for s in ['model is unavailable', 'endpoint is unavailable']):
        return 'model_unavailable'
    if 'overloaded' in messages:
        return 'provider_overloaded'
    if 'another assistant turn' in evidence.get('error', '').lower():
        return 'protocol_violation'
    return 'provider_error'


class OpenCode:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
        self.identity = None
        self.client = None
        self.log = None
        self.metadata = {}
        self.failure_path = None

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
        if self.client:
            self.client.close()
            self.client = None
        if self.log:
            self.log.close()
            self.log = None
        self.identity = None

    def start(self, ident: str, cap: int, catalog: tuple[str, ...] = ()):
        identity = (ident, cap, catalog)
        if self.identity == identity and self.process.poll() is None:
            return
        self.close()
        client_version(self.settings.state)
        directory = self.settings.state / 'opencode-session-v3'
        directory.mkdir(mode=0o700, exist_ok=True)
        config_home = self.settings.state / 'opencode-config-v3'
        config_home.mkdir(mode=0o700, exist_ok=True)
        data_home = self.settings.state / 'opencode-data-v3'
        cache_home = self.settings.state / 'opencode-cache-v3'
        for private_home in (directory, config_home, data_home, cache_home):
            private_home.mkdir(mode=0o700, exist_ok=True)
            private_home.chmod(0o700)
        config = configuration(ident, cap)
        if catalog:
            config['provider']['opencode']['whitelist'] = list(catalog)
            config['provider']['opencode']['models'] = {
                name: {'limit': {'context': 200000, 'output': cap}} for name in catalog}
        config['provider']['opencode']['options'] = {'apiKey': credential('zen')}
        env = {k: v for k, v in os.environ.items() if not k.startswith('OPENCODE_')}
        env.update({'OPENCODE_CONFIG_CONTENT': json.dumps(config), 'XDG_CONFIG_HOME': str(config_home),
                    'XDG_DATA_HOME': str(data_home), 'XDG_CACHE_HOME': str(cache_home),
                    'OPENCODE_CONFIG_DIR': str(config_home / 'opencode'), 'OPENCODE_DISABLE_PROJECT_CONFIG': '1',
                    'OPENCODE_DISABLE_EXTERNAL_SKILLS': '1', 'OPENCODE_DISABLE_CLAUDE_CODE': '1',
                    'OPENCODE_ENABLE_PARALLEL': 'false', 'OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS': 'false',
                    'OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX': str(cap), 'OPENCODE_PURE': 'true',
                    'OPENCODE_SERVER_PASSWORD': os.urandom(24).hex()})
        self.log = tempfile.NamedTemporaryFile(mode='w', prefix='opencode-server-v3-', suffix='.log',
                                              dir=self.settings.state / 'logs', delete=False)
        log_path = Path(self.log.name)
        binary = shutil.which('opencode') or str(directory.home() / '.opencode/bin/opencode')
        self.process = subprocess.Popen([binary, 'serve', '--pure', '--hostname', '127.0.0.1', '--port', '0'],
                                        cwd=directory, env=env, stdout=self.log, stderr=self.log)
        until = time.monotonic() + 15
        while time.monotonic() < until:
            match = re.search(r'http://127\.0\.0\.1:\d+', log_path.read_text())
            if match:
                self.client = httpx.Client(base_url=match.group(), auth=('opencode', env['OPENCODE_SERVER_PASSWORD']),
                                           timeout=self.settings.request_timeout, follow_redirects=False)
                agents = self.client.get('/agent')
                if not agents.is_success or not isinstance(agents.json(), list):
                    self.close()
                    raise RuntimeError('OpenCode rejected the isolated benchmark configuration')
                agent = next((a for a in agents.json() if a['name'] == 'benchmark'), None)
                if not agent or agent.get('prompt') != SYSTEM or agent.get('steps') is not None:
                    self.close()
                    raise RuntimeError('OpenCode benchmark agent or step configuration did not validate')
                permissions = agent.get('permission', [])
                for tool in (*TOOLS, 'external_directory', 'task', 'webfetch', 'skill', 'todowrite', 'batch'):
                    rule = next((p for p in reversed(permissions) if p.get('pattern') == '*'
                                 and p.get('permission') in {'*', tool}), {})
                    if rule.get('action') != ('ask' if tool in (*TOOLS, 'external_directory') else 'deny'):
                        self.close()
                        raise RuntimeError('OpenCode permission policy did not validate')
                if self.client.get('/config').json().get('experimental', {}).get('continue_loop_on_deny') is not False:
                    self.close()
                    raise RuntimeError('OpenCode continuation after rejection was not disabled')
                try:
                    providers = self.client.get('/config/providers').json()['providers']
                    selected = next(p for p in providers if p['id'] == 'opencode')['models'][ident]
                    if not catalog and (selected['api']['url'] != ZEN_BASE or selected['api']['id'] != ident or selected['providerID'] != 'opencode' or
                            selected['id'] != ident or selected['limit']['output'] != cap or
                            selected['cost']['input'] != 0 or selected['cost']['output'] != 0):
                        raise ValueError('Unexpected provider settings')
                    self.metadata = {'version': VERSION, 'protocol_revision': REVISION, 'output_cap': cap,
                                     'temperature': 0 if selected['capabilities']['temperature'] else None,
                                     'model_definition_hash': digest({k: selected[k] for k in ['api', 'capabilities', 'options']}),
                                     'reasoning': reasoning_policy(None) if catalog else reasoning_policy(selected)}
                except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration):
                    self.close()
                    raise RuntimeError('OpenCode model route, zero pricing, or output setting did not validate') from None
                self.identity = identity
                return
            if self.process.poll() is not None:
                break
            time.sleep(.05)
        self.close()
        raise RuntimeError('Dedicated OpenCode server did not start')

    def generate(self, model: dict, messages: list[dict], cap: int, on_retry) -> dict:
        self.failure_path = None
        body = prompt_body(model, messages)
        try:
            self.start(model['id'], cap)
            planned = json.loads(model['profile'])['reasoning']
            actual = self.metadata['reasoning']
            fields = ('mode', 'variant', 'available_variants') if cap == 256 else tuple(planned)
            if any(planned[k] != actual.get(k) for k in fields):
                raise RuntimeError('Reasoning controls changed; rediscover before generation')
            session = self.client.post('/session', json={'title': 'Freeboard controlled benchmark'}).json()
            session_id = session['id']
        except BaseException:
            self.close()
            raise
        events = queue.Queue()
        ready, done, stop = threading.Event(), threading.Event(), threading.Event()
        response = []

        def listen():
            try:
                with self.client.stream('GET', '/event') as stream:
                    stream.raise_for_status()
                    ready.set()
                    for line in stream.iter_lines():
                        if stop.is_set():
                            break
                        if line.startswith('data:'):
                            events.put(json.loads(line[5:]))
            except Exception:
                if not stop.is_set():
                    events.put({'type': 'listener.error'})
                ready.set()

        def request():
            try:
                # The local server returns the complete message, so its read
                # timeout must allow slower reasoning models to finish.
                result = self.client.post(f'/session/{session_id}/message', json=body,
                                          timeout=max(self.settings.request_timeout, 600))
                result.raise_for_status()
                response.append(result.json())
            except Exception:
                response.append(None)
            finally:
                done.set()

        threading.Thread(target=listen, daemon=True).start()
        if not ready.wait(5):
            stop.set()
            self.close()
            raise RuntimeError('OpenCode event stream did not connect; no prompt dispatched')
        threading.Thread(target=request, daemon=True).start()
        observed, retry_attempt, end_polls, assistant_ids, denied = [], 0, 0, set(), []
        try:
            while True:
                try:
                    event = events.get(timeout=.1)
                except queue.Empty:
                    if done.is_set():
                        end_polls += 1
                        if end_polls >= 2:
                            break
                    continue
                if event['type'] == 'listener.error':
                    raise RuntimeError('OpenCode event stream interrupted; outcome unknown')
                props = event.get('properties', {})
                part, info = props.get('part', {}), props.get('info', {})
                belongs = props.get('sessionID') or part.get('sessionID') or info.get('sessionID')
                if belongs != session_id:
                    continue
                if event['type'] == 'permission.asked':
                    rejected = self.client.post(f"/permission/{props['id']}/reply", json={'reply': 'reject'})
                    rejected.raise_for_status()
                    denied.append(props['id'])
                if (event['type'] == 'message.part.updated' and part.get('type') == 'tool'
                        and part.get('tool') not in TOOLS):
                    raise RuntimeError('Unexpected tool outside the frozen schema set')
                if event['type'] == 'message.updated' and info.get('role') == 'assistant':
                    assistant_ids.add(info['id'])
                    if len(assistant_ids) > 1:
                        raise RuntimeError('OpenCode attempted another assistant turn; no replay')
                if event['type'] == 'session.status' and props['status']['type'] == 'retry':
                    status = props['status']
                    if status['attempt'] > retry_attempt:
                        retry_attempt = status['attempt']
                        observed.append(status)
                        on_retry(status)
            if not response or response[0] is None:
                raise RuntimeError('OpenCode request outcome unknown; no replay')
            result = response[0]
            if not isinstance(result, dict):
                raise RuntimeError('Unrecognized OpenCode response; outcome unknown')
            messages = self.client.get(f'/session/{session_id}/message').json()
            users = [m['info'] for m in messages if m['info']['role'] == 'user']
            if len(users) != 1 or users[0]['model'].get('variant') != body.get('variant'):
                raise RuntimeError('OpenCode did not retain the requested reasoning variant')
            result['assistant_turns'] = sum(m['info']['role'] == 'assistant' for m in messages)
            result['tool_parts'] = sum(p['type'] == 'tool' for m in messages for p in m['parts'])
            result['observed_retries'] = observed
            result['denied_permissions'] = denied
            result['session_id'] = session_id
            result['effective_settings'] = self.metadata
            return result
        except BaseException as error:
            # Kill this private server immediately during retry backoff, before
            # another provider dispatch can occur. Unknown outcomes stay private.
            if self.process and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            with tempfile.NamedTemporaryFile(mode='w', prefix='opencode-failure-', suffix='.json',
                                             dir=self.settings.state / 'logs', delete=False) as evidence:
                json.dump({'session_id': session_id, 'model_id': model['id'], 'cap': cap,
                           'error_type': type(error).__name__, 'error': str(error),
                           'observed_retries': observed, 'assistant_turns_observed': len(assistant_ids),
                           'denied_permissions': denied, 'effective_settings': self.metadata}, evidence)
                self.failure_path = evidence.name
            raise
        finally:
            stop.set()
            # Closing the dedicated process also releases the blocking SSE reader.
            self.close()


def inspect_models(settings, ids: list[str]) -> dict[str, dict]:
    """Read native model metadata without dispatching any completion."""
    if not ids:
        return {}
    client = OpenCode(settings)
    try:
        client.start(ids[0], 4096, tuple(ids))
        providers = client.client.get('/config/providers').json()['providers']
        models = next(p for p in providers if p['id'] == 'opencode')['models']
        return {ident: models[ident] for ident in ids if ident in models}
    finally:
        client.close()
