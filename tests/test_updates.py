from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from cloud_storage.updates import (
    UpdateError,
    UpdatePreferences,
    UpdateService,
    _launch_elevated_windows,
    canonical_manifest_payload,
    parse_signed_catalog,
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


def signed_catalog() -> tuple[bytes, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.b64encode(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    versions = []
    for version, channel in (("9.8.8", "stable"), ("9.9.0b1", "beta"), ("9.8.7", "stable")):
        payload = version.encode()
        versions.append(
            {
                "version": version,
                "channel": channel,
                "published_at": "2026-08-04T10:00:00Z",
                "release_notes": f"Версия {version}",
                "package": {
                    "url": f"https://updates.example/client-{version}.exe",
                    "filename": f"client-{version}.exe",
                    "size_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                },
            }
        )
    value: dict[str, object] = {
        "schema_version": 2,
        "product": "client",
        "versions": versions,
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


def test_signed_catalog_lists_versions_newest_first_and_rejects_tampering() -> None:
    raw, public_key = signed_catalog()
    versions = parse_signed_catalog(raw, expected_product="client", public_key=public_key)
    assert [item.version for item in versions] == ["9.9.0b1", "9.8.8", "9.8.7"]

    tampered = raw.replace(b"9.8.8", b"9.8.9")
    with pytest.raises(UpdateError, match="подпись"):
        parse_signed_catalog(tampered, expected_product="client", public_key=public_key)


def test_update_preferences_are_persistent_and_invalid_values_are_safe(tmp_path) -> None:
    service = UpdateService(
        "client", tmp_path, feed_url="https://updates.example/client.json"
    )
    service.save_preferences(UpdatePreferences(policy="download", channel="beta"))
    assert service.load_preferences() == UpdatePreferences(policy="download", channel="beta")

    service.preferences_path.write_text('{"schema_version":1,"policy":"broken"}')
    assert service.load_preferences() == UpdatePreferences()


def test_download_reuses_an_existing_verified_setup(tmp_path, monkeypatch) -> None:
    payload = b"installer still open by Windows"
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

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("a verified cached installer must not be downloaded again")

    monkeypatch.setattr(service, "_open_verified", unexpected_download)

    assert service.download(info) == installer.resolve()
    state = json.loads((service.download_directory / "update-state.json").read_text())
    assert state["sha256"] == info.package.sha256
    assert not list(service.download_directory.glob("update-state-*.tmp"))


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

    if os.name == "nt":
        monkeypatch.setattr(
            "cloud_storage.updates._launch_elevated_windows",
            lambda arguments: launched.append(arguments),
        )
    else:
        monkeypatch.setattr(
            "cloud_storage.updates.subprocess.Popen",
            lambda arguments, **_kwargs: launched.append(arguments),
        )
    service.launch_installer(installer, info)
    assert launched and launched[0][0] == str(installer.resolve())
    assert "/VERYSILENT" in launched[0]

    installer.write_bytes(b"modified!")
    with pytest.raises(UpdateError, match="изменён"):
        service.launch_installer(installer, info)


def test_permanent_installer_can_open_interactive_setup(tmp_path, monkeypatch) -> None:
    payload = b"interactive installer"
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
    if os.name == "nt":
        monkeypatch.setattr(
            "cloud_storage.updates._launch_elevated_windows",
            lambda arguments: launched.append(arguments),
        )
    else:
        monkeypatch.setattr(
            "cloud_storage.updates.subprocess.Popen",
            lambda arguments, **_kwargs: launched.append(arguments),
        )

    service.launch_installer(installer, info, interactive=True)

    assert launched
    assert "/VERYSILENT" not in launched[0]
    assert "/SUPPRESSMSGBOXES" not in launched[0]
    assert "/NORESTART" in launched[0]


def test_both_setup_packages_offer_an_optional_desktop_shortcut() -> None:
    project = Path(__file__).resolve().parents[1]
    for name in ("CloudStorageClient.iss", "CloudStorageServer.iss"):
        source = (project / "packaging" / name).read_text(encoding="utf-8")
        assert 'Name: "desktopicon"' in source
        assert 'Tasks: desktopicon' in source
        assert 'Flags: unchecked' in source


def test_server_setup_stops_all_components_before_retrying_protected_backup() -> None:
    project = Path(__file__).resolve().parents[1]
    source = (project / "packaging" / "CloudStorageServer.iss").read_text(
        encoding="utf-8"
    )
    prepare = source.index("function PrepareToInstall")
    stop = source.index("StopServerComponents;", prepare)
    backup = source.index("BackupServerData", stop)
    assert stop < backup
    for executable in (
        "CloudStorageServerManager.exe",
        "CloudStorageContainerManager.exe",
        "CloudStorageServerCore.exe",
        "CloudStorageLegacyCore.exe",
        "CloudStorageServerService.exe",
    ):
        assert executable in source
    assert "function CopyFileWithRetry" in source
    assert "for Attempt := 1 to 40" in source


@pytest.mark.skipif(os.name != "nt", reason="Windows ShellExecute API")
def test_windows_installer_launch_uses_runas_and_quotes_parameters(monkeypatch, tmp_path) -> None:
    calls: list[tuple[object, ...]] = []

    class ShellExecute:
        argtypes: object = None
        restype: object = None

        def __call__(self, *arguments: object) -> int:
            calls.append(arguments)
            return 42

    shell_execute = ShellExecute()
    monkeypatch.setattr(
        "cloud_storage.updates.ctypes.windll.shell32.ShellExecuteW",
        shell_execute,
    )
    installer = tmp_path / "Cloud Storage Setup.exe"
    _launch_elevated_windows([str(installer), "/VERYSILENT", "/LOG=install log.txt"])

    assert calls
    assert calls[0][1] == "runas"
    assert calls[0][2] == str(installer.resolve())
    assert '"/LOG=install log.txt"' in str(calls[0][3])


@pytest.mark.parametrize(
    ("older", "newer"),
    [("0.7.0a2", "0.7.0b1"), ("0.7.0rc1", "0.7.0"), ("0.7.0", "0.7.1")],
)
def test_update_versions_are_ordered(older: str, newer: str) -> None:
    assert version_key(older) < version_key(newer)
