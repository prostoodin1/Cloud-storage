from __future__ import annotations

import hashlib
import os
import platform
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# Pin to the official cloudflare/cloudflared v2026.9.1 release. Keep the hash
# synchronized with that release's published SHA256 Checksums block.
VERSION = "2026.9.1"
ASSETS = {
    ("Windows", "AMD64"): (
        "cloudflared-windows-amd64.exe",
        "2837888cc0f5d58f15b6dc478376de90b4d3ba5241c7947455d1e0a0df429712",
    ),
    ("Linux", "x86_64"): (
        "cloudflared-linux-amd64",
        "03f1f25d1cc93b9ad6c60569d44060bc4f17ed97075760ed8cfca4b12dcd68cc",
    ),
}
MAX_BYTES = 100 * 1024 * 1024


def download_cloudflared(data_directory: Path) -> Path:
    system = platform.system()
    machine = platform.machine()
    asset = ASSETS.get((system, machine))
    if asset is None:
        raise RuntimeError(f"cloudflared {VERSION} не поддерживает эту платформу ({system}/{machine})")
    filename, expected_sha256 = asset
    url = f"https://github.com/cloudflare/cloudflared/releases/download/{VERSION}/{filename}"
    folder = data_directory / "tools" / "cloudflared"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("cloudflared.exe" if system == "Windows" else "cloudflared")
    descriptor, temporary_name = tempfile.mkstemp(prefix="cloudflared-", suffix=".download", dir=folder)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    total = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "CloudStorage-Server/0.10.0"})
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(request, timeout=45) as response:
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise RuntimeError("cloudflared binary is larger than the allowed download limit")
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if total < 1024 * 1024 or digest.hexdigest() != expected_sha256:
            raise RuntimeError("SHA-256 cloudflared не совпадает с официальным release manifest")
        if system != "Windows":
            temporary.chmod(0o700)
        os.replace(temporary, target)
        return target
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Не удалось скачать cloudflared с GitHub: {exc.reason}") from exc
    finally:
        temporary.unlink(missing_ok=True)
