from __future__ import annotations

from cloud_storage.services.password_vault import ManagerPasswordVault


def test_manager_password_vault_round_trip_and_clear(tmp_path) -> None:
    vault = ManagerPasswordVault(tmp_path)

    assert vault.load("user-1") == ""
    vault.store("user-1", "correct horse battery staple")
    assert vault.load("user-1") == "correct horse battery staple"

    vault.clear("user-1")
    assert vault.load("user-1") == ""
