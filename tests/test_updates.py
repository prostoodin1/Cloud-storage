from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud_storage.updates import (
    UpdateError,
    UpdateService,
    canonical_manifest_payload,
    parse_signed_manifest,
    version_key,
)


def signed_manifest(payload: bytes = b"installer") -> tuple[bytes, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    value: dict[str, object] = {
        "schema_version": 1,
        "product": "client",
        "version": "9.8.7",
        "channel": "stable",
        "published_at": "2026-08-04T10:00:00Z",
        "release_notes": "Проверка обновления",
        "package": {
            "url": "https://updates.example/CloudStorage-Client-Setup.exe",
            "filename": "CloudStorage-Client-Setup.exe",
            "size_bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        },
    }
    value["signature"] = base64.b64encode(
        private_key.sign(canonical_manifest_payload(value))
    ).decode("ascii")
    return json.dumps(value, ensure_ascii=False).encode(), public_key


def test_signed_update_manifest_rejects_tampering() -> None:
    raw, public_key = signed_manifest()
    info = parse_signed_manifest(raw, expected_product="client", public_key=public_key)
    assert info.version == "9.8.7"
    assert info.newer_than_current is True

    tampered = raw.replace(b"9.8.7", b"9.8.8")
    with pytest.raises(UpdateError, match="подпись"):
        parse_signed_manifest(tampered, expected_product="client", public_key=public_key)


def test_update_installer_is_reverified_immediately_before_launch(
    tmp_path, monkeypatch
) -> None:
    payload = b"installer"
    raw, public_key = signed_manifest(payload)
    info = parse_signed_manifest(raw, expected_product="client", public_key=public_key)
    service = UpdateService(
        "client",
        tmp_path,
        feed_url="https://updates.example/client.json",
        public_key=public_key,
    )
    service.download_directory.mkdir(parents=True)
    installer = service.download_directory / info.package.filename
    installer.write_bytes(payload)
    launched: list[list[str]] = []

    monkeypatch.setattr(
        "cloud_storage.updates.subprocess.Popen",
        lambda arguments, **_kwargs: launched.append(arguments),
    )
    service.launch_installer(installer, info)
    assert launched and launched[0][0] == str(installer.resolve())

    installer.write_bytes(b"modified!")
    with pytest.raises(UpdateError, match="изменён"):
        service.launch_installer(installer, info)


@pytest.mark.parametrize(
    ("older", "newer"),
    [("0.7.0a2", "0.7.0b1"), ("0.7.0rc1", "0.7.0"), ("0.7.0", "0.7.1")],
)
def test_update_versions_are_ordered(older: str, newer: str) -> None:
    assert version_key(older) < version_key(newer)
