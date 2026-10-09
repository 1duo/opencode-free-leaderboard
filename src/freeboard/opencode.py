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
REVISION = 3  # Earlier native experiments flattened roles and used steps=1.
SYSTEM = 'Follow the supplied benchmark instructions exactly. Answer once. Do not use tools.'


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
            'permission': {'*': 'deny'}, 'compaction': {'auto': False, 'prune': False},
            'provider': {'opencode': {'whitelist': [ident], 'models': {
                ident: {'limit': {'context': 200000, 'output': cap}}}}},
            'agent': {'benchmark': {'mode': 'primary', 'temperature': 0,
                'prompt': SYSTEM, 'permission': {'*': 'deny'}},
                'title': {'disable': True}, 'summary': {'disable': True}, 'compaction': {'disable': True}}}


def profile(endpoint_protocol: str) -> dict:
    return {'protocol': 'opencode', 'transport': 'local-opencode', 'endpoint_protocol': endpoint_protocol,
            'version': VERSION, 'protocol_revision': REVISION, 'temperature': 0,
            'configuration_hash': digest(configuration('MODEL', 4096)),
            'extra_system_context': True, 'cap_verified': False}


def prompt_body(model: dict, messages: list[dict]) -> dict:
    settings = json.loads(model['profile'])
    endpoints = {'chat': f'{ZEN_BASE}/chat/completions', 'responses': f'{ZEN_BASE}/responses'}
    if (model['status'] != 'eligible' or excluded(model['id']) or
            model['endpoint'] != endpoints.get(settings.get('endpoint_protocol')) or
            settings.get('transport') != 'local-opencode' or settings.get('protocol_revision') != REVISION):
        raise ValueError('Model lacks verified free eligibility for the current OpenCode protocol')
    if not messages or messages[-1]['role'] != 'user' or any(m['role'] != 'system' for m in messages[:-1]):
        raise ValueError('Benchmark must contain system instructions followed by one user question')
    return {'agent': 'benchmark', 'model': {'providerID': 'opencode', 'modelID': model['id']},
            'system': '\n\n'.join(m['content'] for m in messages[:-1]),
            'parts': [{'type': 'text', 'text': messages[-1]['content']}]}


def completion_events(result: dict, ident: str) -> list[dict]:
    if result.get('assistant_turns', 1) != 1 or result.get('tool_parts', 0):
        raise ValueError('OpenCode produced multiple turns or attempted tool use')
    info = result.get('info', {})
    if not isinstance(info, dict) or not isinstance(result.get('parts'), list):
        raise ValueError('Malformed OpenCode message')
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
            raise ValueError('Tool use is outside the benchmark protocol')
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


class OpenCode:
    def __init__(self, settings):
        self.settings = settings
        self.process = None
        self.identity = None
        self.client = None
        self.log = None
        self.metadata = {}

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

    def start(self, ident: str, cap: int):
        identity = (ident, cap)
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
                try:
                    providers = self.client.get('/config/providers').json()['providers']
                    selected = next(p for p in providers if p['id'] == 'opencode')['models'][ident]
                    if (selected['api']['url'] != ZEN_BASE or selected['providerID'] != 'opencode' or
                            selected['id'] != ident or selected['limit']['output'] != cap or
                            selected['cost']['input'] != 0 or selected['cost']['output'] != 0):
                        raise ValueError('Unexpected provider settings')
                    self.metadata = {'version': VERSION, 'protocol_revision': REVISION, 'output_cap': cap,
                                     'temperature': 0 if selected['capabilities']['temperature'] else None,
                                     'model_definition_hash': digest({k: selected[k] for k in ['api', 'capabilities', 'options']})}
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
        body = prompt_body(model, messages)
        try:
            self.start(model['id'], cap)
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
                result = self.client.post(f'/session/{session_id}/message', json=body)
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
        observed, retry_attempt, end_polls, assistant_ids = [], 0, 0, set()
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
                if event['type'] == 'message.part.updated' and part.get('type') == 'tool':
                    raise RuntimeError('OpenCode attempted a tool; aborting without a repair turn')
                if event['type'] == 'message.updated' and info.get('role') == 'assistant':
                    assistant_ids.add(info['id'])
                    if len(assistant_ids) > 1:
                        raise RuntimeError('OpenCode attempted another assistant turn; no replay')
                if event['type'] == 'session.status' and props['status']['type'] == 'retry':
                    status = props['status']
                    if status['attempt'] > retry_attempt:
                        retry_attempt = status['attempt']
                        on_retry(status)
                        observed.append(status)
            if not response or response[0] is None:
                raise RuntimeError('OpenCode request outcome unknown; no replay')
            result = response[0]
            if not isinstance(result, dict):
                raise RuntimeError('Unrecognized OpenCode response; outcome unknown')
            messages = self.client.get(f'/session/{session_id}/message').json()
            result['assistant_turns'] = sum(m['info']['role'] == 'assistant' for m in messages)
            result['tool_parts'] = sum(p['type'] == 'tool' for m in messages for p in m['parts'])
            result['observed_retries'] = observed
            result['session_id'] = session_id
            result['effective_settings'] = self.metadata
            return result
        except BaseException:
            # Kill this private server immediately during retry backoff, before
            # another provider dispatch can occur. Unknown outcomes stay private.
            if self.process and self.process.poll() is None:
                self.process.kill()
                self.process.wait()
            raise
        finally:
            stop.set()
            # Closing the dedicated process also releases the blocking SSE reader.
            self.close()
