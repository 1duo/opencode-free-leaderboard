"""Read-only operational checks. Never dispatch a model request or read a secret."""
from __future__ import annotations

import os
import json
import shutil
import subprocess
from pathlib import Path

from .config import Settings
from .db import DB
from .grading import docker_environment
from .opencode import VERSION, client_version


def doctor(db: DB, settings: Settings, checkout: Path) -> dict:
    checks = []
    def add(name, ready, detail):
        checks.append({'check': name, 'ready': ready, 'detail': detail})
    def succeeds(args):
        try:
            return subprocess.run(args, cwd=checkout, capture_output=True, timeout=15,
                                  env=docker_environment(settings) if args[0] == 'docker' else None).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    add('database', db.conn.execute('PRAGMA quick_check').fetchone()[0] == 'ok',
        'SQLite integrity check')
    size = db.conn.execute('PRAGMA page_count').fetchone()[0] * db.conn.execute('PRAGMA page_size').fetchone()[0]
    free = shutil.disk_usage(settings.state).free
    add('disk', free >= max(1024**3, size + 64 * 1024**2),
        f'{free // 1024**2} MiB free; atomic backups need a full database copy plus headroom')
    try:
        client_version(settings.state)
        add('opencode', True, f'Validated client {VERSION}')
    except RuntimeError as exc:
        add('opencode', False, str(exc))
    docker_ready = succeeds(['docker', 'info', '--format', '{{.ServerVersion}}'])
    add('docker', docker_ready,
        'Docker engine must be running for isolated grading')
    seasons = db.rows('SELECT manifest FROM seasons WHERE validated=1')
    add('panels', bool(seasons), 'At least one validated frozen benchmark season')
    images = {json.loads(s['manifest']).get('grader_image') for s in seasons}
    add('graders', docker_ready and bool(images) and None not in images and all(
        succeeds(['docker', 'image', 'inspect', image]) for image in sorted(images)),
        'Immutable grader images referenced by validated seasons are present')
    add('github', succeeds(['gh', 'repo', 'view', settings.repository, '--json', 'name']),
        f'Authenticated access to {settings.repository}')
    plist = Path.home() / 'Library/LaunchAgents/ai.opencode.free-leaderboard.plist'
    add('schedule', plist.is_file() and succeeds(['launchctl', 'print', f'gui/{os.getuid()}/ai.opencode.free-leaderboard']),
        'macOS launch agent installed and loaded; deadlines require the machine to run')
    return {'ready': all(c['ready'] for c in checks), 'checks': checks,
            'note': 'Local prerequisites only; provider access is established by the compatibility pilot.'}
