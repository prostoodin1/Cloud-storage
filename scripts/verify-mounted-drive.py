"""Opt-in Windows regression against an isolated Core and a real WinFSP mount.

Usage: python scripts/verify-mounted-drive.py --drive-exe ABSOLUTE_EXE --letter T
Never touches the installed service, client profile, or an occupied drive letter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_storage.client.api_client import ClientApi
from cloud_storage.client.settings import ClientProfile, ClientSettingsStore, DeviceTokenVault
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.main import CoreServerGroup


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--drive-exe", type=Path, required=True)
    parser.add_argument("--letter", default="T", choices=list("DEFGHIJKLMNOPQRSTUVWXYZ"))
    args = parser.parse_args()
    if os.name != "nt":
        raise RuntimeError("Windows is required")
    import ctypes
    bit = 1 << (ord(args.letter) - ord("A"))
    if ctypes.windll.kernel32.GetLogicalDrives() & bit:
        raise RuntimeError("Drive letter is occupied; refusing to touch it")
    executable = args.drive_exe.resolve(strict=True)
    run = Path(tempfile.mkdtemp(prefix="cloud-storage-drive-qa-"))
    config = CoreConfig(data_directory=run / "core", port=free_port(), lan_enabled=True,
                        lan_port=free_port(), discovery_port=free_port())
    servers = CoreServerGroup(config)
    runtime = servers.application.state.runtime
    with runtime.database.transaction() as connection:
        connection.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    thread = threading.Thread(target=servers.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{config.port}"
    manager = ClientApi(base, token=runtime.secrets.manager_token)
    process = None
    results = []
    def check(name, condition):
        results.append({"name": name, "passed": bool(condition)})
        print(json.dumps(results[-1]), flush=True)
        if not condition:
            raise AssertionError(name)
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                manager.health()
                break
            except Exception:
                time.sleep(.1)
        code = manager._json_request("/v1/admin/dynamic-pairing-code")["code"]
        pair = manager._json_request("/v1/pairing/redeem", method="POST", payload={"code": code, "device_name": "Drive QA", "platform": "Windows"})
        client = ClientApi(base, token=pair["device_token"])
        space = client.list_spaces()[0]["id"]
        user = pair["device"]["user_id"]
        manager._json_request(f"/v1/admin/users/{user}", method="PATCH", payload={"display_name": "Quota QA", "quota_gib": 3})
        profile = ClientProfile(server_url=base, device_id=pair["device"]["id"], device_status="trusted", last_space_id=space)
        data = run / "client"
        ClientSettingsStore(data).save(profile)
        DeviceTokenVault(data).store(pair["device_token"])
        process = subprocess.Popen([str(executable), "--data-dir", str(data), "--profile-id", "default", "--space-id", space, "--mount", args.letter + ":", "--parent-pid", str(os.getpid())], creationflags=subprocess.CREATE_NO_WINDOW)
        root = Path(args.letter + ":/")
        deadline = time.monotonic() + 20
        while not root.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(.1)
        check("mounted", root.exists())

        def capacity():
            available, total, free = ctypes.c_ulonglong(), ctypes.c_ulonglong(), ctypes.c_ulonglong()
            if not ctypes.windll.kernel32.GetDiskFreeSpaceExW(str(root), ctypes.byref(available), ctypes.byref(total), ctypes.byref(free)):
                raise ctypes.WinError()
            return total.value, available.value

        def wait_capacity(total, free):
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if capacity() == (total, free):
                    return True
                time.sleep(.2)
            return False

        check("configured_quota_not_8tb", capacity() == (3 * 1024**3, 3 * 1024**3))
        directory = root / "Mixed Folder"
        directory.mkdir()
        payload = b"Cloud Storage mounted drive regression\n" * 8192
        test = directory / "MiXeD-file.txt"
        test.write_bytes(payload)
        check("immediately_visible_after_close", test.exists())
        check("immediate_read_hash", hashlib.sha256(test.read_bytes()).digest() == hashlib.sha256(payload).digest())
        check("nested_case_insensitive_read", (root / "MIXED FOLDER" / "mixed-FILE.TXT").read_bytes() == payload)
        check("free_space_after_upload", wait_capacity(3 * 1024**3, 3 * 1024**3 - len(payload)))
        manager._json_request(f"/v1/admin/users/{user}", method="PATCH", payload={"display_name": "Quota QA", "quota_gib": 5})
        check("live_quota_change", wait_capacity(5 * 1024**3, 5 * 1024**3 - len(payload)))
        renamed = directory / "Renamed.txt"
        test.rename(renamed)
        check("rename", renamed.exists() and not test.exists())
        renamed.unlink()
        check("delete_dotnet_equivalent", not renamed.exists())
        psfile = directory / "PowerShell.txt"
        psfile.write_bytes(b"PowerShell delete test")
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", f"$ErrorActionPreference='Stop'; Remove-Item -LiteralPath '{psfile}'"], capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
        check("delete_powershell", result.returncode == 0 and not psfile.exists())
        directory.rmdir()
        check("delete_directory", not directory.exists())
        check("server_empty", client.list_entries(space) == [])
        check("free_space_after_delete", wait_capacity(5 * 1024**3, 5 * 1024**3))
        (root / "Preserved.txt").write_bytes(b"Preserved after archive")
        manager._json_request(f"/v1/admin/spaces/{space}", method="DELETE")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and (ctypes.windll.kernel32.GetLogicalDrives() & bit or process.poll() is None):
            time.sleep(.2)
        check("revoked_drive_unmounted", not ctypes.windll.kernel32.GetLogicalDrives() & bit and process.poll() is not None)
        manager._json_request(f"/v1/admin/spaces/{space}/restore", method="POST")
        check("archive_restore_keeps_file", client.list_entries(space)[0]["name"] == "Preserved.txt")
    except Exception as exc:
        results.append({"error": str(exc)})
        raise
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=15)
        servers.request_shutdown(delay=False)
        thread.join(timeout=15)
        (run / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        print("REPORT " + str(run / "results.json"), flush=True)


if __name__ == "__main__":
    main()
