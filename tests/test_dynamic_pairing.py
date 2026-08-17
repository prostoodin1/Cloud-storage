from __future__ import annotations

from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.pairing import (
    build_dynamic_pairing_code,
    parse_dynamic_pairing_code,
    verify_dynamic_pairing_code,
)


def test_dynamic_code_is_signed_and_expires() -> None:
    key = b"k" * 32
    code = build_dynamic_pairing_code(
        server_id="a" * 32,
        server_url="https://192.168.1.10:8766",
        certificate_fingerprint="b" * 64,
        expires_at=1_000_300,
        nonce="nonce_nonce_nonce_1234",
        signing_key=key,
        alternate_addresses=("https://cloud.example.test",),
    )

    parsed = parse_dynamic_pairing_code(code)
    assert parsed is not None
    assert parsed.server_id == "a" * 32
    assert parsed.addresses == (
        "https://192.168.1.10:8766",
        "https://cloud.example.test",
    )
    assert verify_dynamic_pairing_code(
        code, signing_key=key, expected_server_id="a" * 32, now=1_000_001
    ) == parsed

    tampered = code[:-1] + ("A" if code[-1] != "A" else "B")
    try:
        verify_dynamic_pairing_code(
            tampered,
            signing_key=key,
            expected_server_id="a" * 32,
            now=1_000_001,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("tampered dynamic pairing code was accepted")

    try:
        verify_dynamic_pairing_code(
            code, signing_key=key, expected_server_id="a" * 32, now=1_000_300
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expired dynamic pairing code was accepted")


def test_dynamic_pairing_creates_approved_device_and_persistent_token(tmp_path) -> None:
    app = create_app(
        CoreConfig(
            data_directory=tmp_path,
            lan_enabled=True,
            lan_port=18766,
            server_name="Pairing Test",
        )
    )
    manager_headers = {
        "Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"
    }

    with TestClient(app) as client:
        generated = client.get(
            "/v1/admin/dynamic-pairing-code", headers=manager_headers
        )
        assert generated.status_code == 200
        code = generated.json()["code"]
        assert code.startswith("CS3.")
        assert 0 < generated.json()["seconds_remaining"] <= 300

        paired = client.post(
            "/v1/pairing/redeem",
            json={
                "code": code,
                "device_name": "Первый компьютер",
                "platform": "Windows",
            },
        )
        assert paired.status_code == 201
        assert paired.json()["device"]["status"] == "trusted"
        token = paired.json()["device_token"]

        status = client.get(
            "/v1/pairing/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert status.status_code == 200
        assert status.json()["status"] == "trusted"

        users = client.get("/v1/admin/users", headers=manager_headers).json()
        paired_user = next(user for user in users if user["display_name"] == "Первый компьютер")
        assert paired_user["role"] == "member"

        promoted = client.patch(
            f"/v1/admin/users/{paired_user['id']}",
            headers=manager_headers,
            json={
                "display_name": paired_user["display_name"],
                "email": paired_user["email"],
                "quota_gib": 100,
                "role": "admin",
                "enabled": True,
            },
        )
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "admin"

        rejected = client.post(
            "/v1/pairing/redeem",
            json={
                "code": code[:-1] + ("A" if code[-1] != "A" else "B"),
                "device_name": "Чужой компьютер",
                "platform": "Windows",
            },
        )
        assert rejected.status_code == 422
