"""Install the frozen native client from checksum-pinned official release assets."""
from __future__ import annotations

import hashlib
import io
import os
import platform
import subprocess
import tarfile
import tempfile
import zipfile
from pathlib import Path

import httpx

from .opencode import VERSION

ASSETS = {
    ('Darwin', 'arm64'): ('opencode-darwin-arm64.zip', 'c2d0fecd27acf6e08be02c1f5d384614d07495587766bcc4a6bff3971749bb64'),
    ('Darwin', 'x86_64'): ('opencode-darwin-x64-baseline.zip', '03f8c32f81a2c35429afedfb8f63978af32329febcebf800d52486f6ba568151'),
    ('Linux', 'aarch64'): ('opencode-linux-arm64.tar.gz', '7900818a6a98881edbbadab4b7525940eff3b9750040c200481f9f56cacee405'),
    ('Linux', 'x86_64'): ('opencode-linux-x64-baseline.tar.gz', '531cff2441129c30f0edd5878af494108ad07f5feb46d7ee29a2a5b830a5765d'),
}


def install_client(state: Path) -> dict:
    asset = ASSETS.get((platform.system(), platform.machine()))
    if asset is None:
        raise RuntimeError('Supported clients: macOS/Linux ARM64 or x86-64; use WSL2 on Windows')
    name, checksum = asset
    target = state / 'bin' / f'opencode-{VERSION}'
    if target.exists():
        result = subprocess.run([str(target), '--version'], capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip() == VERSION:
            return {'client': str(target), 'version': VERSION, 'existing': True}
        raise RuntimeError('Existing pinned client is invalid; preserve it and remove it explicitly before reinstalling')
    url = f'https://github.com/anomalyco/opencode/releases/download/v{VERSION}/{name}'
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        response = client.get(url)
        response.raise_for_status()
    payload = response.content
    if hashlib.sha256(payload).hexdigest() != checksum:
        raise RuntimeError('Official client checksum mismatch; nothing installed')
    if name.endswith('.zip'):
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            executable = archive.read('opencode')
    else:
        with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
            member = next((m for m in archive.getmembers() if m.name in {'opencode', './opencode'} and m.isfile()), None)
            if member is None:
                raise RuntimeError('Official archive has no regular opencode executable')
            executable = archive.extractfile(member).read()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.client-', dir=target.parent) as folder:
        staged = Path(folder) / 'opencode'
        with staged.open('wb') as handle:
            handle.write(executable)
            handle.flush()
            os.fsync(handle.fileno())
        staged.chmod(0o700)
        result = subprocess.run([str(staged), '--version'], capture_output=True, text=True, timeout=15)
        if result.returncode or result.stdout.strip() != VERSION:
            raise RuntimeError('Downloaded client version check failed; nothing installed')
        staged.replace(target)
    return {'client': str(target), 'version': VERSION, 'source': url, 'archive_sha256': checksum}
