from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud_storage.updates import UpdateError, canonical_manifest_payload

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "verify_catalogs", ROOT / "scripts/verify-release-catalogs.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def write_catalog(folder, product, versions, key, repository="test/cloud"):
    entries = []
    for version in versions:
        payload = f"{product}:{version}".encode()
        filename = f"CloudStorage-{product.title()}-Setup-{version}-windows-x64.exe"
        (folder / filename).write_bytes(payload)
        entries.append(
            {
                "version": version,
                "channel": "beta" if "b" in version else "stable",
                "published_at": "2026-10-27T00:00:00Z",
                "release_notes": "test",
                "package": {
                    "url": f"https://github.com/{repository}/releases/download/v{version}/{filename}",
                    "filename": filename,
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        )
    value = {"schema_version": 2, "product": product, "versions": entries}
    value["signature"] = base64.b64encode(key.sign(canonical_manifest_payload(value))).decode()
    (folder / f"cloud-storage-{product}-catalog.json").write_text(
        json.dumps(value), encoding="utf-8"
    )


def test_feed_recovery_verifies_signature_payload_and_preserves_newer_versions(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    existing = tmp_path / "existing"
    existing.mkdir()
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    for product in ("client", "server"):
        write_catalog(release, product, ["1.1.0b1", "1.0.0"], key)
        write_catalog(existing, product, ["1.0.0"], key)
    module.verify(release, existing, "test/cloud", "1.1.0b1", public)
    payload = release / "CloudStorage-Client-Setup-1.1.0b1-windows-x64.exe"
    original = payload.read_bytes()
    payload.write_bytes(b"x" * len(original))
    with pytest.raises(UpdateError, match="SHA-256"):
        module.verify(release, existing, "test/cloud", "1.1.0b1", public)
    payload.write_bytes(original)
    write_catalog(existing, "server", ["1.2.0b1", "1.0.0"], key)
    with pytest.raises(UpdateError, match="remove or change"):
        module.verify(release, existing, "test/cloud", "1.1.0b1", public)
    write_catalog(existing, "server", ["1.0.0"], key)
    path = release / "cloud-storage-client-catalog.json"
    path.write_text(
        path.read_text().replace('"release_notes": "test"', '"release_notes": "tampered"')
    )
    with pytest.raises(UpdateError, match="подпись"):
        module.verify(release, existing, "test/cloud", "1.1.0b1", public)


def powershell():
    return shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(powershell() is None, reason="PowerShell not installed")
def test_publication_repair_recovers_half_published_feed_without_network(tmp_path):
    release = tmp_path / "release"
    release.mkdir()
    feed = tmp_path / "feed"
    feed.mkdir()
    for product in ("client", "server"):
        (release / f"cloud-storage-{product}-catalog.json").write_text("new:" + product)
        (feed / f"cloud-storage-{product}-catalog.json").write_text("old:" + product)
    fake = tmp_path / "verify.ps1"
    fake.write_text("$global:LASTEXITCODE=0\n", encoding="utf-8")
    harness = tmp_path / "mock.ps1"
    harness.write_text(
        r"""
param([string]$Repair,[string]$Fake)
$global:Failures=0
function global:gh {
    $a=@($args);$global:LASTEXITCODE=0
    $root=$env:REPAIR_FIXTURE
    if ($a[1] -eq 'view') {
        if ($a -contains '--jq') { return 'v1.0.0' }
        return '{"isDraft":false,"isPrerelease":true}'
    }
    if ($a[1] -eq 'download') {
        $source=Join-Path $root $(if($a[2] -eq 'v1.1.0b1'){'release'}else{'feed'})
        $destination=$a[[array]::IndexOf($a,'--dir')+1]
        $pattern=$a[[array]::IndexOf($a,'--pattern')+1]
        Get-ChildItem -LiteralPath $source -File | Where-Object {$_.Name -like $pattern} | ForEach-Object {Copy-Item -LiteralPath $_.FullName -Destination $destination}
        return
    }
    if ($a[1] -eq 'upload') {
        $file=$a[-1]
        if ($file -like '*-server-*' -and $global:Failures -lt [int]$env:REPAIR_FAILS) {$global:Failures++;$global:LASTEXITCODE=1;return}
        Copy-Item -LiteralPath $file -Destination (Join-Path $root 'feed') -Force
        [IO.File]::AppendAllText((Join-Path $root 'uploads.log'),([IO.Path]::GetFileName($file)+"`n"))
        return
    }
    throw 'Unexpected mock operation'
}
& $Repair -Version '1.1.0b1' -Repository 'test/cloud' -Python $Fake -RetryDelaySeconds 0
""",
        encoding="utf-8",
    )
    env = dict(os.environ, REPAIR_FIXTURE=str(tmp_path), REPAIR_FAILS="3")
    command = [
        powershell(),
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(harness),
        str(ROOT / "scripts/repair-release-catalogs.ps1"),
        str(fake),
    ]
    failed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert failed.returncode != 0, failed.stdout
    assert (feed / "cloud-storage-client-catalog.json").read_text() == "new:client"
    assert (feed / "cloud-storage-server-catalog.json").read_text() == "old:server"
    env["REPAIR_FAILS"] = "0"
    recovered = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert (feed / "cloud-storage-server-catalog.json").read_text() == "new:server"
    assert (tmp_path / "uploads.log").read_text().splitlines() == [
        "cloud-storage-client-catalog.json",
        "cloud-storage-server-catalog.json",
    ]
