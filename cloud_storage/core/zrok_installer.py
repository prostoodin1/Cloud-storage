from __future__ import annotations

import hashlib
import io
import os
import platform
import tarfile
import urllib.request
from pathlib import Path

ZROK2_VERSION = "2.0.4"

_ASSETS = {
    ("Windows", "amd64"): (
        "zrok_2.0.4_windows_amd64.tar.gz",
        "8e4062a159f65c3735d67d82de0f6a6f59555e9f98a786e80c1e6ab22d92d8c9",
    ),
    ("Linux", "amd64"): (
        "zrok_2.0.4_linux_amd64.tar.gz",
        "1877981b9050c9d69c61bc12c0b92c2da7330e3fbb374faa78ffdbfa37f8a8e3",
    ),
    ("Linux", "arm64"): (
        "zrok_2.0.4_linux_arm64.tar.gz",
        "71a08d11058959a0b90e8f59d4a33612b5fc010fced8c65883995cf64e5502cc",
    ),
    ("Darwin", "amd64"): (
        "zrok_2.0.4_darwin_amd64.tar.gz",
        "d0d0882d84768081c7cbd45c03490bd13e305d19861eaf4811a11e6eb1db5924",
    ),
    ("Darwin", "arm64"): (
        "zrok_2.0.4_darwin_arm64.tar.gz",
        "ad90ee0730bdd066a0a95c1f57bb250bac1b2d5c474ba662043820ea0d2b7e86",
    ),
}


def managed_zrok2_path(data_directory: Path) -> Path:
    name = "zrok2.exe" if os.name == "nt" else "zrok2"
    return data_directory / "tools" / "zrok2" / name


def download_zrok2(data_directory: Path) -> Path:
    machine = platform.machine().casefold()
    architecture = "arm64" if machine in {"arm64", "aarch64"} else "amd64"
    try:
        asset, expected_sha256 = _ASSETS[(platform.system(), architecture)]
    except KeyError as exc:
        raise RuntimeError("automatic zrok2 installation is not available on this platform") from exc
    url = f"https://github.com/openziti/zrok/releases/download/v{ZROK2_VERSION}/{asset}"
    request = urllib.request.Request(url, headers={"User-Agent": "Cloud-Storage-Server"})
    with urllib.request.urlopen(request, timeout=90.0) as response:
        archive = response.read(150 * 1024**2 + 1)
    if len(archive) > 150 * 1024**2:
        raise RuntimeError("zrok2 package is unexpectedly large")
    if hashlib.sha256(archive).hexdigest() != expected_sha256:
        raise RuntimeError("zrok2 package checksum does not match the official release")
    executable_name = "zrok2.exe" if os.name == "nt" else "zrok2"
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as package:
        members = [
            member
            for member in package.getmembers()
            if member.isfile() and Path(member.name).name.casefold() == executable_name.casefold()
        ]
        if len(members) != 1 or members[0].size > 100 * 1024**2:
            raise RuntimeError("zrok2 executable was not found in the official package")
        source = package.extractfile(members[0])
        if source is None:
            raise RuntimeError("zrok2 executable could not be extracted")
        payload = source.read(100 * 1024**2 + 1)
    target = managed_zrok2_path(data_directory)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(payload)
    if os.name != "nt":
        temporary.chmod(0o700)
    os.replace(temporary, target)
    return target
