import stat

import pytest

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


def test_security_policy_rejects_files_outside_storage(tmp_path) -> None:
    storage = tmp_path / "storage"
    storage.mkdir()
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"data")

    with pytest.raises(UnsafeStoragePath):
        make_managed_file_inert(outside, storage)
