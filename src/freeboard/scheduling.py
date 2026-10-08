from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from .config import Settings


def install(settings: Settings, checkout: Path) -> Path:
    binary = shutil.which("uv")
    if not binary:
        raise RuntimeError("uv is not installed")
    plist = Path.home() / "Library/LaunchAgents/ai.opencode.free-leaderboard.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    data = {"Label": "ai.opencode.free-leaderboard",
            "ProgramArguments": [binary, "run", "--frozen", "--no-editable", "--project", str(checkout), "leaderboard",
                                 "--state", str(settings.state), "daily"],
            "WorkingDirectory": str(checkout), "StartCalendarInterval": {"Hour": 9, "Minute": 15},
            "RunAtLoad": True, "ProcessType": "Background",
            "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + str(Path.home() / ".docker/bin")},
            "StandardOutPath": str(settings.state / "logs/scheduler.log"),
            "StandardErrorPath": str(settings.state / "logs/scheduler-error.log")}
    plist.write_bytes(plistlib.dumps(data))
    plist.chmod(0o600)
    import os
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(plist)], capture_output=True)
    result = subprocess.run(["launchctl", "bootstrap", domain, str(plist)], capture_output=True)
    if result.returncode:
        raise RuntimeError("launchd bootstrap failed; launch agent file was saved")
    return plist
