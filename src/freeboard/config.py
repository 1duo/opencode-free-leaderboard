from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

ZEN_BASE = "https://opencode.ai/zen/v1"
ZEN_DOCS = "https://docs.opencode.ai/docs/zen/"
SEED = 20261008
COUNTS = {"screen": {"gpqa": 20, "livebench": 20, "livecodebench": 40},
          "confirmation": {"gpqa": 60, "livebench": 60, "livecodebench": 120},
          "pilot": {"gpqa": 2, "livebench": 2, "livecodebench": 2}}
PINS = {
    "gpqa": "83022cefff930aea54f654c0b282e74b9eeda5c6",
    "livebench_data": "6fc6498a5dfba553f69f4413feabade1f1a2d384",
    "livebench_grader": "24364d65076429adfcba7be18af4d44fddc43dce",
    "livecodebench_data": "0fe84c3912ea0c4d4a78037083943e8f0c4dd505",
    "livecodebench_grader": "28fef95ea8c9f7a547c8329f2cd3d32b92c1fa24",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def week(at: str | None = None) -> str:
    dt = datetime.fromisoformat(at) if at else datetime.now(timezone.utc)
    return (dt - timedelta(days=dt.weekday())).date().isoformat()


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Path = Path.home() / "Library/Application Support/OpenCodeFreeLeaderboard"
    repository: str = "1duo/opencode-free-leaderboard"
    max_output_tokens: int = 4096
    probe_tokens: int = 256
    bootstrap_samples: int = 10000
    request_timeout: float = 180
    minimum_attempts: int = 300
    minimum_tokens: int = 1_000_000

    def initialize(self, checkout: Path) -> None:
        self.state = self.state.expanduser().resolve()
        if self.state.is_relative_to(checkout.resolve()):
            raise ValueError("Private state must be outside the public checkout")
        os.umask(0o077)
        for folder in [self.state, self.state / "responses", self.state / "datasets",
                       self.state / "upstream", self.state / "backups", self.state / "logs"]:
            folder.mkdir(parents=True, exist_ok=True, mode=0o700)
            folder.chmod(0o700)


def credential(name: str) -> str | None:
    service = f"opencode-free-leaderboard.{name}"
    try:
        result = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                                capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    # Environment is useful on other platforms; installed launchd uses Keychain.
    return os.environ.get("OPENCODE_API_KEY" if name == "zen" else "HF_TOKEN")


def save_credential(name: str, secret: str) -> None:
    if not secret.strip():
        raise ValueError("Empty credential")
    # Call Security.framework directly so credentials never appear in process arguments.
    import ctypes
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    service, account, password = f"opencode-free-leaderboard.{name}".encode(), b"freeboard", secret.encode()
    item = ctypes.c_void_p()
    find = security.SecKeychainFindGenericPassword
    find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                     ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    status = find(None, len(service), service, len(account), account, None, None, ctypes.byref(item))
    if status == 0:
        modify = security.SecKeychainItemModifyAttributesAndData
        modify.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p]
        status = modify(item, None, len(password), password)
        core.CFRelease.argtypes = [ctypes.c_void_p]
        core.CFRelease(item)
    elif status == -25300:
        add = security.SecKeychainAddGenericPassword
        add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                        ctypes.c_char_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_void_p]
        status = add(None, len(service), service, len(account), account, len(password), password, None)
    if status:
        raise RuntimeError("Could not save credential in macOS Keychain")
