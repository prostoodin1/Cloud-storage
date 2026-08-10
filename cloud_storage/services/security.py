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
    if os.name == "nt":
        _deny_windows_execute(resolved_path)


def _deny_windows_execute(path: Path) -> None:
    """Protect a managed blob with a canonical NTFS deny-execute ACL."""

    try:
        import ntsecuritycon
        import win32api
        import win32security

        user_sid, _, _ = win32security.LookupAccountName(None, win32api.GetUserName())
        system_sid = win32security.CreateWellKnownSid(win32security.WinLocalSystemSid, None)
        everyone_sid = win32security.CreateWellKnownSid(win32security.WinWorldSid, None)
        dacl = win32security.ACL()
        dacl.AddAccessDeniedAce(
            win32security.ACL_REVISION,
            ntsecuritycon.FILE_EXECUTE,
            everyone_sid,
        )
        safe_access = ntsecuritycon.FILE_ALL_ACCESS & ~ntsecuritycon.FILE_EXECUTE
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, safe_access, user_sid)
        dacl.AddAccessAllowedAce(win32security.ACL_REVISION, safe_access, system_sid)
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            dacl,
            None,
        )
    except (ImportError, OSError):
        # Source-only Windows environments may not have pywin32. Objects still use
        # an opaque .blob name and Core never dispatches them to a process.
        return
