"""Keep the desktop client in the same Windows logon context as Explorer."""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes

_TOKEN_QUERY = 0x0008
_TOKEN_LINKED_TOKEN = 19
_LOGON_WITH_PROFILE = 0x00000001
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_RELAUNCH_MARKER = "--desktop-user-relaunch"


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


def _command_for_relaunch() -> list[str]:
    arguments = [item for item in sys.argv[1:] if item != _RELAUNCH_MARKER]
    if getattr(sys, "frozen", False):
        return [str(sys.executable), _RELAUNCH_MARKER, *arguments]
    return [str(sys.executable), str(sys.argv[0]), _RELAUNCH_MARKER, *arguments]


def relaunch_as_desktop_user() -> bool:
    """Relaunch an elevated frozen client through its linked desktop token.

    Installers and updaters run elevated. A WinFsp drive created by the client
    they start belongs to the elevated DOS-device namespace and is invisible to
    ordinary Explorer. The linked token puts the client back in Explorer's
    medium-integrity desktop session.
    """
    if (
        os.name != "nt"
        or not getattr(sys, "frozen", False)
        or "--smoke-test" in sys.argv
        or _RELAUNCH_MARKER in sys.argv
        or not ctypes.windll.shell32.IsUserAnAdmin()
    ):
        return False

    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    token = wintypes.HANDLE()
    linked_token = wintypes.HANDLE()
    process_info = _ProcessInformation()
    startup_info = _StartupInfo()
    startup_info.cb = ctypes.sizeof(startup_info)
    startup_info.lpDesktop = "winsta0\\default"
    try:
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise ctypes.WinError()
        returned = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_LINKED_TOKEN,
            ctypes.byref(linked_token),
            ctypes.sizeof(linked_token),
            ctypes.byref(returned),
        ):
            raise ctypes.WinError()
        command = _command_for_relaunch()
        command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(command))
        if not advapi32.CreateProcessWithTokenW(
            linked_token,
            _LOGON_WITH_PROFILE,
            None,
            command_line,
            _CREATE_UNICODE_ENVIRONMENT,
            None,
            str(os.path.dirname(sys.executable)),
            ctypes.byref(startup_info),
            ctypes.byref(process_info),
        ):
            raise ctypes.WinError()
        return True
    finally:
        for handle in (
            process_info.hThread,
            process_info.hProcess,
            linked_token,
            token,
        ):
            if handle:
                kernel32.CloseHandle(handle)
