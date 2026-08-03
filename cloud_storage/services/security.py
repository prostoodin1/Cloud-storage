from __future__ import annotations

import os
import stat
from pathlib import Path


class UnsafeStoragePath(ValueError):
    pass


def make_managed_file_inert(path: Path, storage_root: Path) -> None:
    """Make an uploaded regular file read-only and non-executable.

    The containment check is mandatory: this helper must never alter arbitrary user files.
    Server code must additionally serve uploads as data and must never execute them.
    """

    resolved_root = storage_root.resolve(strict=True)
    resolved_path = path.resolve(strict=True)
    if resolved_path == resolved_root or resolved_root not in resolved_path.parents:
        raise UnsafeStoragePath("file is outside the managed storage root")
    if not resolved_path.is_file() or resolved_path.is_symlink():
        raise UnsafeStoragePath("only regular managed files can be hardened")

    current = resolved_path.stat().st_mode
    safe_mode = current & ~(stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    safe_mode &= ~(stat.S_IWGRP | stat.S_IWOTH)
    if os.name == "nt":
        safe_mode = stat.S_IREAD
    else:
        safe_mode &= ~stat.S_IWUSR
        safe_mode |= stat.S_IRUSR
    os.chmod(resolved_path, safe_mode)
