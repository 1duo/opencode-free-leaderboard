from __future__ import annotations

import plistlib
import os
import subprocess
from pathlib import Path

from .config import Settings


def install(settings: Settings, checkout: Path) -> Path:
    private_runtime = settings.state / 'runtime/bin/python'
    binary = private_runtime if private_runtime.exists() else checkout / '.venv/bin/python'
    if not binary.exists():
        raise RuntimeError("Run uv sync before installing the launch agent")
    plist = Path.home() / "Library/LaunchAgents/ai.opencode.free-leaderboard.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": "ai.opencode.free-leaderboard",
            "ProgramArguments": [str(binary), '-m', 'freeboard.cli',
                                 "--state", str(settings.state), "daily"],
            "WorkingDirectory": str(checkout), "StartCalendarInterval": {"Hour": 9, "Minute": 15},
            "RunAtLoad": True, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".docker/bin"),
                                     'PYTHONPATH': str(checkout / 'src')},
            "StandardOutPath": str(settings.state / "logs/scheduler.log"),
            "StandardErrorPath": str(settings.state / "logs/scheduler-error.log")}
    for key in ['DOCKER_HOST', 'DOCKER_CONFIG']:
        if os.environ.get(key):
            data['EnvironmentVariables'][key] = os.environ[key]
    plist.write_bytes(plistlib.dumps(data))
    plist.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
    if result.returncode:
        raise RuntimeError("launchd bootstrap failed; launch agent file was saved")
    return plist
