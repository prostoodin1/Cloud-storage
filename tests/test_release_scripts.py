import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_storage.updates import parse_signed_catalog


def _catalog_module():
    path = Path(__file__).parents[1] / "scripts" / "sign-update-catalog.py"
    spec = importlib.util.spec_from_file_location("sign_update_catalog", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_catalog_version_key_supports_beta_releases() -> None:
    version_key = _catalog_module().version_key
    versions = ["0.10.7", "0.10.8a2", "0.10.8b1", "0.10.8rc1", "0.10.8"]
    assert sorted(versions, key=version_key) == versions


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI key storage")
def test_release_signers_accept_encrypted_key_without_plaintext_file(tmp_path):
    import win32crypt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption())
    encrypted = win32crypt.CryptProtectData(
        pem, "QA key", b"CloudStorage-Update-Signing-v2", None, None, 1,
    )
    key_path = tmp_path / "key.dpapi"
    key_path.write_bytes(encrypted)
    assert b"BEGIN PRIVATE KEY" not in key_path.read_bytes()
    setup = tmp_path / "test.exe"
    setup.write_bytes(b"synthetic installer; never executed")
    manifest = tmp_path / "manifest.json"
    catalog = tmp_path / "catalog.json"
    scripts = Path(__file__).parents[1] / "scripts"
    subprocess.run([
        sys.executable, str(scripts / "sign-update-manifest.py"), "--key", str(key_path),
        "--product", "client", "--version", "99.0.0", "--installer", str(setup),
        "--url", "https://example.com/test.exe", "--output", str(manifest),
    ], check=True, capture_output=True)
    subprocess.run([
        sys.executable, str(scripts / "sign-update-catalog.py"), "--key", str(key_path),
        "--product", "client", "--manifest", str(manifest), "--output", str(catalog),
    ], check=True, capture_output=True)
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )).decode()
    assert parse_signed_catalog(catalog.read_bytes(), expected_product="client",
                                public_key=public)[0].version == "99.0.0"
