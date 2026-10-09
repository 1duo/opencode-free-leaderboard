from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict

ZEN_BASE = "https://opencode.ai/zen/v1"
ZEN_DOCS = "https://docs.opencode.ai/docs/zen/"
SEED = 20261008
OMITTED_MODELS = frozenset({'exo-free', 'ling-3.0-flash-fin-free', 'nemotron-3-ultra-free'})
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


def keychain_credential(name: str) -> str | None:
    if sys.platform != "darwin":
        return None
    # Match the native writer and its exact account. Spawning `security` uses a
    # different Keychain reader and can leave unattended runs awaiting a dialog.
    import ctypes

    try:
        security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        service, account = f"opencode-free-leaderboard.{name}".encode(), b"freeboard"
        length, data = ctypes.c_uint32(), ctypes.c_void_p()
        find = security.SecKeychainFindGenericPassword
        find.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_uint32,
                         ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint32),
                         ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        find.restype = ctypes.c_int32
        status = find(None, len(service), service, len(account), account,
                      ctypes.byref(length), ctypes.byref(data), None)
        if status:
            return None
        try:
            return ctypes.string_at(data, length.value).decode().strip() or None
        finally:
            free = security.SecKeychainItemFreeContent
            free.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            free(None, data)
    except (OSError, UnicodeError):
        return None


def credential(name: str) -> str | None:
    saved = keychain_credential(name)
    if saved:
        return saved
    value = os.environ.get("OPENCODE_API_KEY" if name == "zen" else "HF_TOKEN")
    if value:
        return value
    if name == "zen":
        # Reuse OpenCode's API credential when present. OAuth tokens are not Zen keys.
        path = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "opencode/auth.json"
        try:
            auth = json.loads(path.read_text()).get("opencode", {})
            if auth.get("type") == "api" and isinstance(auth.get("key"), str) and auth["key"]:
                return auth["key"]
        except (OSError, ValueError, AttributeError):
            pass
        # OpenCode's own provider uses this public credential for free models.
        # Eligibility still requires fresh catalog AND explicit zero-price evidence.
        return "public"
    hf_home = Path(os.environ.get("HF_HOME", str(Path.home() / ".cache/huggingface")))
    try:
        return (hf_home / "token").read_text().strip() or None
    except OSError:
        return None


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
