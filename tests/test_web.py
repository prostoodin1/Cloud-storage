import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.tunnels import ZrokTunnelService
from cloud_storage.core.web import COOKIE


@pytest.fixture
def web(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path, lan_enabled=True))
    with app.state.runtime.database.transaction() as connection:
        connection.execute("UPDATE storage_roots SET min_free_bytes = 0, max_fill_percent = 99")
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
        code = client.get("/v1/admin/dynamic-pairing-code", headers=headers).json()["code"]
        yield app, client, code


def pair(client, code):
    response = client.post("/v1/web/pair", json={"code": code}, headers={"Origin": "http://testserver"})
    assert response.status_code == 200, response.text
    assert "device_token" not in response.json()
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie
    return response.json(), {"Origin": "http://testserver", "X-CSRF-Token": response.json()["csrf"]}


def test_web_assets_and_anonymous_api(web):
    _, client, _ = web
    root = client.get("/")
    assert root.status_code == 200 and "text/html" in root.headers["content-type"]
    assert "Введите код подключения" in root.text
    assert "script-src 'self'" in root.headers["content-security-policy"]
    for file in ["app.js", "app.css"]:
        assert client.get("/web/assets/" + file).status_code == 200
    assert client.get("/web/assets/secret").status_code == 404
    assert client.get("/v1/spaces").status_code == 401
    assert client.get("/v1/admin/users").status_code == 401


def test_browser_files_csrf_logout_and_manager_boundary(web):
    _, client, code = web
    _, headers = pair(client, code)
    assert client.get("/v1/admin/users").status_code == 401
    space = client.get("/v1/spaces").json()[0]["id"]
    base = f"/v1/spaces/{space}"
    assert client.put(base + "/files/example.txt", content=b"hello").status_code == 403
    assert client.put(base + "/files/example.txt", content=b"hello", headers=headers).status_code == 201
    assert client.get(base + "/files/example.txt").content == b"hello"
    assert client.get(base + "/entries").json()[0]["name"] == "example.txt"
    assert client.post(base + "/directories", json={"logical_path": "Folder"}, headers=headers).status_code == 201
    assert client.post(base + "/moves", json={"source_path": "example.txt", "destination_path": "Folder/Renamed.txt", "kind": "file"}, headers=headers).status_code == 200
    assert client.delete(base + "/files/Folder/Renamed.txt", headers=headers).status_code == 200
    token = client.cookies.get(COOKIE)
    assert client.post("/v1/web/logout", headers={"Origin": "https://attacker.invalid"}).status_code == 403
    assert client.post("/v1/web/logout", headers=headers).status_code == 200
    client.cookies.set(COOKIE, token)
    assert client.get("/v1/spaces").status_code == 401


@pytest.mark.parametrize("origin", [None, "null", "http://attacker.invalid", "https://testserver"])
def test_cross_origin_pairing_rejected(web, origin):
    _, client, code = web
    headers = {} if origin is None else {"Origin": origin}
    assert client.post("/v1/web/pair", json={"code": code}, headers=headers).status_code == 403


def test_invalid_code_and_cookie_rejected(web):
    _, client, code = web
    bad = code[:-10] + "invalid123"
    assert client.post("/v1/web/pair", json={"code": bad}, headers={"Origin": "http://testserver"}).status_code == 422
    client.cookies.set(COOKIE, "random-invalid-session")
    assert client.get("/v1/web/session").status_code == 401


@pytest.mark.parametrize("revocation", ["device", "user", "expired", "policy"])
def test_browser_access_can_be_revoked(web, revocation):
    app, client, code = web
    result, _ = pair(client, code)
    runtime = app.state.runtime
    with runtime.database.transaction() as connection:
        if revocation == "device":
            connection.execute("UPDATE devices SET status='revoked' WHERE id=?", (result["device"]["id"],))
        elif revocation == "user":
            connection.execute("UPDATE users SET enabled=0 WHERE id=?", (result["device"]["user_id"],))
        elif revocation == "expired":
            connection.execute("UPDATE web_sessions SET expires_at=?", (int(time.time()) - 1,))
    if revocation == "policy":
        runtime.control.update_settings({"browser_access": "nobody"})
    assert client.get("/v1/spaces").status_code in {401, 403}


def test_session_persists_restart_without_plaintext_token(web):
    app, client, code = web
    _, _ = pair(client, code)
    token = client.cookies.get(COOKIE)
    with app.state.runtime.database.connection() as connection:
        row = connection.execute("SELECT * FROM web_sessions").fetchone()
        assert token not in str(dict(row))
    restarted = create_app(app.state.runtime.config)
    with TestClient(restarted) as other:
        other.cookies.set(COOKIE, token)
        assert other.get("/v1/spaces").status_code == 200


def test_each_file_capability_is_enforced_for_cookies(web):
    app, client, code = web
    result, headers = pair(client, code)
    repo = app.state.runtime.repository
    space = repo.create_shared_space(name="Shared", quota_bytes=1024**3, primary_storage_root_id=None, fallback_storage_root_id=None)
    user_id = result["device"]["user_id"]
    base = f"/v1/spaces/{space.id}"
    repo.set_space_member(space.id, user_id, {"read": True, "upload": True, "modify": True, "delete": True, "share": True})
    assert client.put(base+"/files/a.txt", content=b"original", headers=headers).status_code == 201
    repo.set_space_member(space.id, user_id, {"read": True})
    assert client.get(base+"/files/a.txt").content == b"original"
    assert client.put(base+"/files/b.txt", content=b"denied", headers=headers).status_code == 403
    assert client.post(base+"/directories", json={"logical_path":"denied"}, headers=headers).status_code == 403
    assert client.post(base+"/moves", json={"source_path":"a.txt", "destination_path":"b.txt", "kind":"file"}, headers=headers).status_code == 403
    assert client.delete(base+"/files/a.txt", headers=headers).status_code == 403
    repo.set_space_member(space.id, user_id, {"read": False})
    assert client.get(base+"/files/a.txt").status_code == 404


def test_external_browser_cookie_secure_and_remote_pairing_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(ZrokTunnelService, "start", lambda self: None)
    config = CoreConfig(data_directory=tmp_path, zrok_enabled=True, remote_pairing_enabled=True, zrok_port=18768)
    app = create_app(config)
    with TestClient(app, base_url="https://cloud.example:18768") as remote:
        tunnel = app.state.runtime.tunnels.providers["zrok"]
        tunnel._state, tunnel._public_url = "online", "https://cloud.example:18768"
        local = TestClient(app)
        manager_headers = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
        code = local.get("/v1/admin/dynamic-pairing-code", headers=manager_headers).json()["code"]
        response = remote.post("/v1/web/pair", json={"code":code}, headers={"Origin":"https://cloud.example:18768"})
        assert response.status_code == 200, response.text
        assert "Secure" in response.headers["set-cookie"]
        assert remote.get("/v1/spaces").status_code == 200
        assert remote.get("/v1/admin/users").status_code == 404
        app.state.runtime.config = replace(config, remote_pairing_enabled=False)
        assert remote.post("/v1/web/pair", json={"code":code}, headers={"Origin":"https://cloud.example:18768"}).status_code == 403
