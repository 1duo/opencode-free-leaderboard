from __future__ import annotations

import plistlib
import os
import subprocess
from pathlib import Path

from .config import Settings


def scheduler_checkout(settings: Settings) -> Path:
    # launchd can stall when Python inspects protected Documents paths. Keep the
    # scheduled public checkout alongside the private runtime, outside Documents.
    target = settings.state / 'scheduler-checkout'
    remote = f'https://github.com/{settings.repository}.git'
    def git(*args: str) -> str:
        result = subprocess.run(['git', *args], capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError('Cannot prepare scheduled checkout; publish the source first and check GitHub access')
        return result.stdout.strip()
    if target.exists():
        if git('-C', str(target), 'remote', 'get-url', 'origin') != remote:
            raise ValueError('Scheduled checkout origin differs from publication repository')
        changed = git('-C', str(target), 'diff', '--name-only', 'HEAD').splitlines()
        untracked = git('-C', str(target), 'ls-files', '--others', '--exclude-standard').splitlines()
        if any(not p.startswith('site/') for p in changed) or untracked:
            raise RuntimeError('Scheduled checkout contains source changes; preserve and review them before reinstalling')
        # A blocked daily run can leave a generated local snapshot. Its durable
        # inputs are in SQLite, so discard only these reproducible site files.
        git('-C', str(target), 'restore', '--staged', '--worktree', '--', 'site')
        git('-C', str(target), 'fetch', 'origin', 'main')
        git('-C', str(target), 'merge', '--ff-only', 'origin/main')
    else:
        git('clone', '--single-branch', '--branch', 'main', remote, str(target))
    return target


def install(settings: Settings, checkout: Path) -> Path:
    private_runtime = settings.state / 'runtime/bin/python'
    binary = private_runtime if private_runtime.exists() else checkout / '.venv/bin/python'
    if not binary.exists():
        raise RuntimeError("Run uv sync before installing the launch agent")
    plist = Path.home() / "Library/LaunchAgents/ai.opencode.free-leaderboard.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    scheduled = scheduler_checkout(settings)
    data = {"Label": "ai.opencode.free-leaderboard",
            "ProgramArguments": [str(binary), '-m', 'freeboard.cli',
                                 "--state", str(settings.state), "daily"],
            "WorkingDirectory": str(scheduled), "StartCalendarInterval": {"Hour": 9, "Minute": 15},
            "RunAtLoad": True, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".docker/bin"),
                                     'PYTHONPATH': str(scheduled / 'src')},
            "StandardOutPath": str(settings.state / "logs/scheduler.log"),
            "StandardErrorPath": str(settings.state / "logs/scheduler-error.log")}
    for key in ['DOCKER_HOST', 'DOCKER_CONFIG']:
        if os.environ.get(key):
            data['EnvironmentVariables'][key] = os.environ[key]
    plist.write_bytes(plistlib.dumps(data))
    plist.chmod(0o600)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
    if result.returncode:
        raise RuntimeError("launchd bootstrap failed; launch agent file was saved")
    return plist
