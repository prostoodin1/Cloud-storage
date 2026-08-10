import os
import stat

import pytest

from cloud_storage.core.security import CredentialService, InvalidCredential
from cloud_storage.services.security import UnsafeStoragePath, make_managed_file_inert


def test_managed_upload_becomes_read_only_and_non_executable(tmp_path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    upload = storage / "payload.sh"
    upload.write_text("#!/bin/sh\necho unsafe\n", encoding="utf-8")
    upload.chmod(0o777)

    make_managed_file_inert(upload, storage)

    mode = stat.S_IMODE(upload.stat().st_mode)
    assert not mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    assert not mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    if os.name == "nt":
        import ntsecuritycon
        import win32security

        descriptor = win32security.GetNamedSecurityInfo(
            str(upload),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION,
        )
        first_ace = descriptor.GetSecurityDescriptorDacl().GetAce(0)
        assert first_ace[0][0] == win32security.ACCESS_DENIED_ACE_TYPE
        assert first_ace[1] & ntsecuritycon.FILE_EXECUTE


def test_security_policy_rejects_files_outside_storage(tmp_path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"data")

    with pytest.raises(UnsafeStoragePath):
        make_managed_file_inert(outside, storage)


def test_remote_session_is_signed_bound_and_expires(monkeypatch) -> None:
    credentials = CredentialService.from_secret("11" * 48)
    token, expires_at = credentials.issue_remote_session(
        "user-1", "device-1", password_version=4, ttl_seconds=300
    )

    credentials.verify_remote_session(
        token, user_id="user-1", device_id="device-1", password_version=4
    )
    with pytest.raises(InvalidCredential):
        credentials.verify_remote_session(
            token, user_id="user-1", device_id="device-1", password_version=5
        )
    with pytest.raises(InvalidCredential):
        credentials.verify_remote_session(
            token, user_id="user-1", device_id="device-2", password_version=4
        )
    with pytest.raises(InvalidCredential):
        credentials.verify_remote_session(
            token[:-1] + ("0" if token[-1] != "0" else "1"),
            user_id="user-1",
            device_id="device-1",
            password_version=4,
        )

    monkeypatch.setattr("cloud_storage.core.security.time.time", lambda: expires_at + 1)
    with pytest.raises(InvalidCredential):
        credentials.verify_remote_session(
            token, user_id="user-1", device_id="device-1", password_version=4
        )
