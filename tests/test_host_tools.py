from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig, CoreSecrets
from cloud_storage.core.host_tools import HostToolsService


def test_managed_ssh_config_enforces_key_only_login_before_match_blocks() -> None:
    original = """# Port 22
PasswordAuthentication yes
PubkeyAuthentication no
Match Group administrators
    AuthorizedKeysFile __PROGRAMDATA__/ssh/administrators_authorized_keys
"""
    configured = HostToolsService._configure_sshd(original, username="serveradmin", port=2222)

    managed = HostToolsService._managed_ssh_values(configured)
    assert managed == {
        "port": "2222",
        "pubkeyauthentication": "yes",
        "passwordauthentication": "no",
        "allowusers": "serveradmin",
    }
    assert configured.index("# BEGIN CLOUD STORAGE MANAGED SSH") < configured.index(
        "Match Group administrators"
    )
    assert "# Cloud Storage replaced: PasswordAuthentication yes" in configured
    assert "# Cloud Storage replaced: PubkeyAuthentication no" in configured


def test_public_key_validation_rejects_private_or_broken_keys() -> None:
    key_type = b"ssh-ed25519"
    payload = len(key_type).to_bytes(4, "big") + key_type + (32).to_bytes(4, "big") + b"x" * 32
    valid = "ssh-ed25519 " + base64.b64encode(payload).decode() + " laptop"
    assert HostToolsService._validate_public_key(valid) == valid

    with pytest.raises(ValueError):
        HostToolsService._validate_public_key("-----BEGIN OPENSSH PRIVATE KEY-----")
    with pytest.raises(ValueError):
        HostToolsService._validate_public_key("ssh-ed25519 not-base64")


def test_host_tool_jobs_report_completion_and_failures(tmp_path: Path) -> None:
    service = HostToolsService(tmp_path, platform_name="Windows")
    completed = service._start_job("ok", lambda: None)
    assert completed["status"] == "queued"
    for _ in range(100):
        if service._job("ok")["status"] == "completed":
            break
        time.sleep(0.01)
    assert service._job("ok")["status"] == "completed"

    def fail() -> None:
        raise RuntimeError("expected failure")

    service._start_job("failed", fail)
    for _ in range(100):
        if service._job("failed")["status"] == "failed":
            break
        time.sleep(0.01)
    assert service._job("failed")["message"] == "expected failure"


def test_manager_api_requires_confirmation_for_docker_and_ssh(tmp_path: Path) -> None:
    config = CoreConfig(data_directory=tmp_path)
    app = create_app(config)
    client = TestClient(app)
    token = CoreSecrets.load_or_create(config).manager_token
    headers = {"Authorization": f"Bearer {token}"}

    docker = client.post(
        "/v1/admin/control-center/host-tools/docker/install",
        headers=headers,
        json={"confirmed": False},
    )
    assert docker.status_code == 422
    assert "confirmation" in docker.json()["detail"]

    ssh = client.post(
        "/v1/admin/control-center/host-tools/ssh/enable",
        headers=headers,
        json={
            "username": "serveradmin",
            "public_key": "ssh-ed25519 "
            + base64.b64encode(
                (11).to_bytes(4, "big") + b"ssh-ed25519" + (32).to_bytes(4, "big") + b"x" * 32
            ).decode(),
            "port": 22,
            "confirmed": False,
        },
    )
    assert ssh.status_code == 422
    assert "confirmation" in ssh.json()["detail"]
