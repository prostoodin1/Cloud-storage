"""Folder refresh, identity, quota and revocation regressions after 0.9.21."""
import base64
import json
import os

import pytest
from fastapi.testclient import TestClient
from PySide6.QtTest import QSignalSpy
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from cloud_storage.client.settings import ClientSettingsStore
from cloud_storage.client.window import ClientWindow
from cloud_storage.core.api import create_app
from cloud_storage.core.config import CoreConfig
from cloud_storage.models import AppSettings
from cloud_storage.ui.connection_page import ConnectionCodePanel
from cloud_storage.ui.dialogs import UserEditDialog
from cloud_storage.ui.pages import DisksPage

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def core(tmp_path):
    app = create_app(CoreConfig(data_directory=tmp_path, lan_enabled=True))
    with app.state.runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    with TestClient(app) as client:
        manager = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
        yield app, client, manager


def join(client, manager, user_id=""):
    generated = client.get("/v1/admin/dynamic-pairing-code", params={"user_id": user_id}, headers=manager)
    assert generated.status_code == 200, generated.text
    code = generated.json()["code"]
    result = client.post("/v1/pairing/redeem", json={"code": code, "device_name": "QA", "platform": "Windows"})
    assert result.status_code == 201, result.text
    value = result.json()
    return value["device"]["user_id"], {"Authorization": f"Bearer {value['device_token']}"}, code


def test_targeted_code_shares_identity_between_browser_and_desktop(core):
    _, client, manager = core
    user_id, desktop, _ = join(client, manager)
    space = client.get("/v1/spaces", headers=desktop).json()[0]["id"]
    base = f"/v1/spaces/{space}"
    assert client.put(base + "/files/private.txt", content=b"same person", headers=desktop).status_code == 201
    same_id, second, code = join(client, manager, user_id)
    assert same_id == user_id
    assert client.get(base + "/files/private.txt", headers=second).content == b"same person"
    web = client.post("/v1/web/pair", json={"code": code}, headers={"Origin": "http://testserver"})
    assert web.status_code == 200, web.text
    assert web.json()["device"]["user_id"] == user_id
    assert client.get(base + "/files/private.txt").content == b"same person"
    assert len(client.get("/v1/admin/users", headers=manager).json()) == 1
    assert len(client.get("/v1/admin/spaces", headers=manager).json()) == 1
    client.cookies.clear()
    _, stranger, _ = join(client, manager)
    assert client.get(base + "/files/private.txt", headers=stranger).status_code == 404
    parts = code.split(".")
    body = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    body["u"] = "00000000-0000-0000-0000-000000000000"
    parts[1] = base64.urlsafe_b64encode(json.dumps(body).encode()).decode().rstrip("=")
    tampered = client.post("/v1/pairing/redeem", json={"code": ".".join(parts), "device_name": "QA", "platform": "Web"})
    assert tampered.status_code == 422


def test_quota_archive_restore_and_shared_link_revocation(core):
    _, client, manager = core
    user, device, _ = join(client, manager)
    space = client.get("/v1/spaces", headers=device).json()[0]["id"]
    base = f"/v1/spaces/{space}"
    admin = f"/v1/admin/spaces/{space}"
    assert client.patch(f"/v1/admin/users/{user}", headers=manager, json={"display_name": "Personal QA", "quota_gib": 3}).status_code == 200
    payload = b"archived content must survive" * 100
    assert client.put(base + "/files/data.txt", content=payload, headers=device).status_code == 201
    quota = client.get("/v1/spaces", headers=device).json()[0]
    assert quota["quota_bytes"] == 3 * 1024**3
    assert quota["used_bytes"] == len(payload)
    assert quota["free_bytes"] == 3 * 1024**3 - len(payload)
    share = client.post("/v1/shares", headers=device, json={"space_id": space, "logical_path": "data.txt", "kind": "file"}).json()
    assert client.get(share["url_path"]).status_code == 200
    assert client.delete(admin, headers=device).status_code in {401, 403}
    assert client.delete(admin, headers=manager).json()["files_preserved"]
    assert client.get("/v1/spaces", headers=device).json() == []
    assert client.get("/v1/admin/spaces", headers=manager).json() == []
    archived = client.get("/v1/admin/spaces?include_archived=true", headers=manager).json()
    assert archived[0]["archived_at"]
    for route in [base + "/entries", base + "/files/data.txt", share["url_path"], share["url_path"] + "/download"]:
        assert client.get(route, headers=device).status_code == 404
    assert client.put(base + "/files/new.txt", content=b"denied", headers=device).status_code == 404
    assert client.post(admin + "/restore", headers=manager).status_code == 200
    assert client.get(base + "/files/data.txt", headers=device).content == payload
    assert client.get(share["url_path"]).status_code == 404


def test_each_right_and_complete_read_revocation(core):
    _, client, manager = core
    user, device, _ = join(client, manager)
    space = client.post("/v1/admin/spaces", headers=manager, json={"name": "Team", "quota_gib": 2}).json()["id"]
    base = f"/v1/spaces/{space}"
    membership = f"/v1/admin/spaces/{space}/members/{user}"

    def rights(**values):
        payload = dict.fromkeys(["read", "upload", "modify", "delete", "share"], False)
        payload.update(values)
        result = client.put(membership, headers=manager, json=payload)
        assert result.status_code == 200, result.text

    rights(read=True, upload=True)
    assert client.put(base + "/files/a.txt", content=b"first", headers=device).status_code == 201
    assert client.put(base + "/files/a.txt", content=b"overwrite", headers=device).status_code == 403
    rights(read=True)
    assert client.get(base + "/files/a.txt", headers=device).content == b"first"
    assert client.put(base + "/files/new.txt", content=b"denied", headers=device).status_code == 403
    assert client.post(base + "/directories", json={"logical_path": "denied"}, headers=device).status_code == 403
    move = {"source_path": "a.txt", "destination_path": "b.txt", "kind": "file"}
    assert client.post(base + "/moves", json=move, headers=device).status_code == 403
    assert client.delete(base + "/files/a.txt", headers=device).status_code == 403
    share_request = {"space_id": space, "logical_path": "a.txt", "kind": "file"}
    assert client.post("/v1/shares", json=share_request, headers=device).status_code == 403
    rights(read=True, share=True)
    shared = client.post("/v1/shares", json=share_request, headers=device).json()
    assert client.get(shared["url_path"]).status_code == 200
    rights(read=True, modify=True)
    assert client.get(shared["url_path"]).status_code == 404
    assert client.post(base + "/moves", json=move, headers=device).status_code == 200
    rights(read=True, delete=True)
    assert client.delete(base + "/files/b.txt", headers=device).status_code == 200
    # Turning off viewing must revoke even if other checkboxes remain selected.
    rights(read=False, upload=True, modify=True, delete=True, share=True)
    assert space not in {s["id"] for s in client.get("/v1/spaces", headers=device).json()}
    assert client.get(base + "/entries", headers=device).status_code == 404
    assert client.put(base + "/files/bypass.txt", content=b"denied", headers=device).status_code == 404


def test_upload_cannot_commit_after_revocation(core, monkeypatch):
    app, client, manager = core
    user, device, _ = join(client, manager)
    space = client.post("/v1/admin/spaces", headers=manager, json={"name": "Race"}).json()["id"]
    repo = app.state.runtime.repository
    repo.set_space_member(space, user, {"read": True, "upload": True})
    storage = app.state.runtime.storage
    original = storage._commit_upload

    def revoke_then_commit(**kwargs):
        repo.set_space_member(space, user, {"read": False})
        return original(**kwargs)

    monkeypatch.setattr(storage, "_commit_upload", revoke_then_commit)
    result = client.put(f"/v1/spaces/{space}/files/race.txt", content=b"must not commit", headers=device)
    assert result.status_code in {403, 404}, result.text
    with app.state.runtime.database.connection() as db:
        assert db.execute("SELECT count(*) FROM files WHERE space_id=?", (space,)).fetchone()[0] == 0


def test_client_refresh_preserves_directory_selection_and_drops_stale_results(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    window = ClientWindow(store=ClientSettingsStore(tmp_path), smoke_test=True)
    tasks = []
    monkeypatch.setattr(window, "_start_task", lambda *args: tasks.append(args))
    monkeypatch.setattr(window, "_files_error", lambda *args: pytest.fail("stale error displayed"))
    window.api = type("FakeApi", (), {"list_entries": lambda *args: []})()
    spaces = [{"id": "personal", "name": "Personal", "can_upload": True}]
    try:
        window._set_spaces(spaces)
        stale = tasks[-1]
        window.current_directory = "Photos/Summer"
        window.refresh_entries()
        window._render_entries([{"name": "chosen.txt", "type": "file", "size_bytes": 5}])
        window.files_table.selectRow(0)
        for _ in range(5):
            window._set_spaces(spaces)
            assert window.current_directory == "Photos/Summer"
        assert len(tasks) == 2  # The timer must not starve slow directory requests.
        latest = tasks[-1]
        stale[1]([{"name": "WRONG.txt"}])
        stale[2]("network error")
        assert window.entries[0]["name"] == "chosen.txt"
        latest[1]([{"name": "other.txt"}, {"name": "chosen.txt"}])
        assert window.selected_entry()["name"] == "chosen.txt"
        letters = window.profile.drive_letters.copy()
        window._set_spaces([], authoritative=False)
        assert window.profile.drive_letters == letters and "personal" in letters
        window._set_spaces([])
        assert window.profile.drive_letters == {}
        assert window.store.load().drive_letters == {}
    finally:
        window.close()
        app.processEvents()


def test_refresh_inactive_profile_does_not_select_it(tmp_path):
    store = ClientSettingsStore(tmp_path)
    first = store.load()
    first.drive_letters = {"revoked": "Z"}
    store.save(first)
    second = store.add_profile()
    store.ensure_space_drive_letters(first, [], make_active=False)
    assert store.load().profile_id == second.profile_id
    assert next(p for p in store.list_profiles() if p.profile_id == first.profile_id).drive_letters == {}


def test_manager_target_and_archive_controls():
    app = QApplication.instance() or QApplication([])
    panel = ConnectionCodePanel()
    users = [{"id": "qa-user", "display_name": "QA Person", "enabled": True}]
    health = {"lan": {"enabled": True, "endpoints": ["https://192.168.1.2:8766"]}}
    panel.set_data(health, users, {})
    spy = QSignalSpy(panel.dynamic_target_requested)
    panel.dynamic_target.setCurrentIndex(panel.dynamic_target.findData("qa-user"))
    assert list(spy.at(0)) == ["qa-user"]
    assert panel.dynamic_code.text() == ""
    panel.set_data(health, users, {})
    assert panel.dynamic_target.currentData() == "qa-user" and spy.count() == 1
    assert "Только локальная сеть" in panel.web_status.text()
    panel.set_dynamic_code({"error": "Обновите ядро"})
    assert panel.dynamic_countdown.text() == "Обновите ядро"

    page = DisksPage()
    space = {"id": "qa-space", "name": "Personal", "kind": "personal", "owner_user_id": "qa-user",
             "quota_bytes": 3 * 1024**3, "members": [{"user_id": "qa-user", "display_name": "QA Person"}]}
    page.update_data([], AppSettings(), spaces=[space])
    page._set_view("roles")
    card = page.cards.itemAt(0).widget()
    assert any("QA Person" in label.text() for label in card.findChildren(QLabel))
    archived = QSignalSpy(page.archive_space_requested)
    edit = QSignalSpy(page.edit_personal_requested)
    next(b for b in card.findChildren(QPushButton) if b.text() == "Удалить виртуальный диск").click()
    next(b for b in card.findChildren(QPushButton) if b.text() == "Настроить владельца и квоту").click()
    assert list(archived.at(0)) == ["qa-space", True]
    assert list(edit.at(0)) == ["qa-user"]
    space["archived_at"] = "2026-09-04T12:00:00Z"
    page.update_data([], AppSettings(), spaces=[space])
    assert page.cards.count() == 0
    page.archive_toggle.setChecked(True)
    card = page.cards.itemAt(0).widget()
    next(b for b in card.findChildren(QPushButton) if b.text() == "Восстановить диск").click()
    assert list(archived.at(1)) == ["qa-space", False]
    dialog = UserEditDialog(users[0], [dict(space, kind="shared")])
    assert dialog.space_permissions == {}
    dialog.close()
    panel.close()
    page.close()
    app.processEvents()


def test_migration_preserves_existing_person_files_and_pairing(tmp_path):
    config = CoreConfig(data_directory=tmp_path, lan_enabled=True)
    app = create_app(config)
    with app.state.runtime.database.transaction() as db:
        db.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    with TestClient(app) as client:
        manager = {"Authorization": f"Bearer {app.state.runtime.secrets.manager_token}"}
        user, device, _ = join(client, manager)
        space = client.get("/v1/spaces", headers=device).json()[0]["id"]
        assert client.put(f"/v1/spaces/{space}/files/old.txt", content=b"preserve", headers=device).status_code == 201
    # Reproduce the previous schema in this temporary DB, never production.
    with app.state.runtime.database.transaction() as db:
        db.execute("ALTER TABLE spaces DROP COLUMN archived_at")
        db.execute("DELETE FROM schema_migrations WHERE version=21")
    upgraded = create_app(config)
    with TestClient(upgraded) as client:
        assert client.get(f"/v1/spaces/{space}/files/old.txt", headers=device).content == b"preserve"
        same_user, _, _ = join(client, manager, user)
        assert same_user == user
        assert client.get("/v1/admin/spaces", headers=manager).json()[0]["archived_at"] is None
        assert client.patch(f"/v1/admin/users/{user}", headers=manager, json={"display_name":"QA", "enabled":False}).status_code == 200
        assert client.get("/v1/admin/dynamic-pairing-code", headers=manager, params={"user_id":user}).status_code == 422
        assert client.get(f"/v1/spaces/{space}/entries", headers=device).status_code in {401,403}
