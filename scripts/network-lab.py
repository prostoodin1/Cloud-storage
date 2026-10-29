"""Linux root-only, disposable network-namespace transfer lab. Never real WAN.

Run in a dedicated WSL distribution, not on a production server. Payloads live
only in --work; successful test payloads are removed to bound disk consumption.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

NAMES = ("csqa-client", "csqa-router", "csqa-server")
CLIENT = "10.203.11.2"
SERVER = "10.203.22.2"
PYTHON = sys.executable


def command(*args, check=True, **kwargs):
    return subprocess.run(args, check=check, capture_output=True, text=True, **kwargs)


def ns(name, *args, **kwargs):
    return command("ip", "netns", "exec", name, *args, **kwargs)


def emit(event, **values):
    print(json.dumps({"event": event, **values}), flush=True)


def request(method, route, payload=None, token=""):
    headers = {"Authorization": "Bearer " + token} if token else {}
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:8765" + route, data=data, method=method, headers=headers), timeout=10) as response:
        return json.load(response)


def admin(args):
    from cloud_storage.pairing import parse_dynamic_pairing_code

    secrets = json.loads((args.work / "server/core-secrets.json").read_text())
    token = secrets["manager_token"]
    if args.action in {"invite", "recover-invite"}:
        recovery = None
        route = "/v1/admin/dynamic-pairing-code"
        if args.action == "recover-invite":
            with sqlite3.connect(args.work / "server/core.db") as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute("SELECT * FROM upload_sessions WHERE expected_size=? ORDER BY updated_at DESC LIMIT 1", (args.sizes[0],)).fetchone()
            if row is None or row["received_bytes"] != row["expected_size"]:
                raise RuntimeError("No fully received QA upload to recover")
            recovery = {k: row[k] for k in ("id", "space_id", "logical_path", "expected_size", "expected_sha256", "user_id")}
            route += "?user_id=" + row["user_id"]
        code = request("GET", route, token=token)["code"]
        invitation = parse_dynamic_pairing_code(code)
        print(json.dumps({"code": code, "fingerprint": invitation.certificate_fingerprint, "recovery": recovery}))
    elif args.action == "quota":
        data = json.load(sys.stdin)
        request("PATCH", "/v1/admin/users/" + data["user_id"], {"quota_gib": 64, "display_name": "Isolated network QA"}, token)
    elif args.action == "shutdown":
        request("POST", "/v1/admin/shutdown", token=token)


def make_payload(path, size):
    digest = hashlib.sha256()
    started = time.perf_counter()
    next_report = started + 20
    with path.open("wb") as handle:
        remaining = size
        while remaining:
            block = os.urandom(min(4 * 1024**2, remaining))
            handle.write(block)
            digest.update(block)
            remaining -= len(block)
            if time.perf_counter() >= next_report:
                emit("generate", bytes=size - remaining, total=size)
                next_report += 20
        handle.flush()
        os.fsync(handle.fileno())
    return digest.hexdigest(), time.perf_counter() - started


def outage(record):
    def disrupt():
        rule = ("-p", "tcp", "-j", "REJECT", "--reject-with", "tcp-reset")
        ns(NAMES[1], "iptables", "-I", "FORWARD", "1", *rule)
        record["injected"] = True
        emit("network_outage", seconds=3, method="router TCP reset")
        try:
            time.sleep(3)
        finally:
            ns(NAMES[1], "iptables", "-D", "FORWARD", *rule)
            record["restored"] = True
    thread = threading.Thread(target=disrupt, daemon=True)
    thread.start()
    return thread


def client(args):
    from cloud_storage.client.api_client import ClientApi, ClientApiError, ClientConnectionError

    data = json.load(sys.stdin)
    api = ClientApi(f"https://{SERVER}:8766", certificate_fingerprint=data["fingerprint"])
    paired = api.redeem_invitation(data["code"], None, "Network namespace client", "Linux/WSL emulation")
    api.token = paired["device_token"]
    server_work = args.recover_work or args.work
    ns(NAMES[2], PYTHON, __file__, "admin", "quota", "--work", str(server_work), input=json.dumps({"user_id": paired["device"]["user_id"]}))
    space = api.list_spaces()[0]["id"]
    folder = args.work / "client"
    folder.mkdir(exist_ok=True)
    report = {"samples": [], "source_generation": [], "errors": [], "recoveries": []}
    output = args.work / "client-results.json"

    def save():
        temporary = output.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, indent=2))
        temporary.replace(output)

    try:
        if args.recover_work:
            recovery = data["recovery"]
            size = int(recovery["expected_size"])
            digest = recovery["expected_sha256"]
            assert recovery["space_id"] == space
            record = api.complete_resumable_upload(recovery["id"])
            assert record["size_bytes"] == size and record["sha256"] == digest
            emit("recovered_committed_upload", size_bytes=size, sha256_ok=True)
            report["recovered_committed_upload"] = {"size_bytes": size, "sha256_ok": True, "original_client_completion_timed_out": True}
            old_source = args.recover_work / "client/source.bin"
            assert old_source.resolve().is_relative_to(args.recover_work.resolve())
            if old_source.exists() and old_source.stat().st_size == size:
                old_source.unlink()
            started = time.perf_counter()
            retries = 0
            resume_offsets = []
            destination = folder / "download.bin"
            last_report = started

            def recovered_progress(received, total):
                nonlocal last_report
                if time.perf_counter() - last_report > 20:
                    emit("download_progress", size=size, bytes=received, MB_s=round(received / (time.perf_counter() - started) / 1e6, 2), recovery_run=True)
                    last_report = time.perf_counter()

            while True:
                try:
                    api.download_file(space, recovery["logical_path"], destination, progress=recovered_progress, expected_sha256=digest)
                    break
                except (ClientConnectionError, ClientApiError) as error:
                    if isinstance(error, ClientApiError) and error.status_code not in {408, 429, 500, 502, 503, 504}:
                        raise
                    retries += 1
                    if retries > 15:
                        raise
                    part = destination.with_name(destination.name + ".part")
                    offset = part.stat().st_size if part.exists() else 0
                    resume_offsets.append(offset)
                    report["recoveries"].append({"direction": "download", "size_bytes": size, "offset": offset, "error_type": type(error).__name__, "cause_type": type(error.__cause__).__name__ if error.__cause__ else None, "http_status": getattr(error, "status_code", None)})
                    emit("download_retry", offset=offset, http_status=getattr(error, "status_code", None), cause=type(error.__cause__).__name__ if error.__cause__ else type(error).__name__)
                    save()
                    time.sleep(max(1, min(60, getattr(error, "retry_after", None) or 0)))
            elapsed = time.perf_counter() - started
            sample = {"direction": "download", "protocol": "desktop Range + SHA256 + fsync", "size_bytes": size, "seconds": elapsed, "MB_s": size / elapsed / 1e6, "MiB_s": size / elapsed / 1024**2, "sha256_ok": True, "outage": False, "retries": retries, "resume_offsets": resume_offsets, "recovery_run": True}
            report["samples"].append(sample)
            save()
            emit("sample", **sample)
            destination.unlink()
            api.delete_file(space, recovery["logical_path"])
            trash = server_work / "server/storage/default/.trash"
            assert trash.resolve().is_relative_to(server_work.resolve())
            for blob in trash.glob("*.blob"):
                blob.unlink()
            return
        for size in args.sizes:
            free = shutil.disk_usage("/mnt/c").free
            reusable = 0
            if args.host_vhd:
                # Deleted guest payloads leave reusable blocks inside a dynamic
                # VHD. Do not count those blocks as requiring fresh host space.
                guest_used = shutil.disk_usage(args.work).used
                reusable = max(0, args.host_vhd.stat().st_size - guest_used)
            if free < max(0, 2 * size - reusable) + 8_000_000_000:
                raise RuntimeError(f"Insufficient host disk: {free} bytes free")
            source = folder / "source.bin"
            destination = folder / "download.bin"
            digest, seconds = make_payload(source, size)
            report["source_generation"].append({"size_bytes": size, "seconds": seconds, "sha256": digest})
            emit("generated", size_bytes=size, seconds=round(seconds, 2))
            for broken in ([False, True] if size == args.sizes[0] and args.faults else [False]):
                logical = f"qa-{size}-{'outage' if broken else 'clean'}.bin"
                started = time.perf_counter()
                state = api.create_resumable_upload(space, logical, size, sha256=digest)
                chunk_size = min(16 * 1024**2, int(state.get("max_chunk_bytes", 4 * 1024**2)))
                if broken:
                    chunk_size = min(chunk_size, max(1, size // 4))
                offset = 0
                retries = 0
                fault = {"injected": False, "restored": False}
                fault_thread = None
                last_report = started
                with source.open("rb") as handle:
                    while offset < size:
                        if broken and offset >= min(size // 4, 256 * 1024**2) and not fault_thread:
                            fault_thread = outage(fault)
                            time.sleep(0.1)
                        handle.seek(offset)
                        block = handle.read(min(chunk_size, size - offset))
                        try:
                            state = api.append_resumable_upload(state["id"], offset, block)
                            offset = int(state["received_bytes"])
                        except (ClientConnectionError, ClientApiError) as error:
                            if isinstance(error, ClientApiError) and error.status_code not in {408, 409, 429, 500, 502, 503, 504}:
                                raise
                            retries += 1
                            report["recoveries"].append({"direction": "upload", "size_bytes": size, "offset": offset, "outage": broken, "error_type": type(error).__name__, "cause_type": type(error.__cause__).__name__ if error.__cause__ else None})
                            if retries > 15:
                                raise
                            time.sleep(1)
                            try:
                                state = api.resumable_upload_status(state["id"])
                                offset = int(state["received_bytes"])
                            except (ClientConnectionError, ClientApiError):
                                pass
                        if time.perf_counter() - last_report > 20:
                            emit("upload_progress", size=size, bytes=offset, MB_s=round(offset / (time.perf_counter() - started) / 1e6, 2), outage=broken)
                            last_report = time.perf_counter()
                complete_retries = 0
                while True:
                    try:
                        record = api.complete_resumable_upload(state["id"])
                        break
                    except (ClientConnectionError, ClientApiError) as error:
                        if isinstance(error, ClientApiError) and error.status_code not in {408, 429, 500, 502, 503, 504}:
                            raise
                        # This endpoint explicitly returns the committed result
                        # for an already completed session. Unlike arbitrary
                        # mutations, repeating completion is idempotent.
                        complete_retries += 1
                        report["recoveries"].append({"direction": "upload_complete", "size_bytes": size, "offset": offset, "outage": broken, "error_type": type(error).__name__, "cause_type": type(error.__cause__).__name__ if error.__cause__ else None})
                        emit("completion_retry", size=size, attempt=complete_retries)
                        if complete_retries > 5:
                            raise
                        time.sleep(1)
                elapsed = time.perf_counter() - started
                if fault_thread:
                    fault_thread.join(10)
                assert record["sha256"] == digest and record["size_bytes"] == size
                sample = {"direction": "upload", "protocol": "resumable, at most desktop 16MiB chunks", "chunk_bytes": chunk_size, "size_bytes": size, "seconds": elapsed, "MB_s": size / elapsed / 1e6, "MiB_s": size / elapsed / 1024**2, "sha256_ok": True, "outage": broken, "retries": retries, "completion_retries": complete_retries, "fault": fault}
                report["samples"].append(sample)
                save()
                emit("sample", **sample)
                # Remove source before a clean download of the last variant, so
                # no more than two full-size payload copies coexist.
                final_variant = broken or not args.faults or size != args.sizes[0]
                if final_variant:
                    source.unlink()
                fault = {"injected": False, "restored": False}
                fault_thread = None
                retries = 0
                last_report = time.perf_counter()
                started = time.perf_counter()

                def progress(received, total, broken=broken, size=size, fault=fault, started=started):
                    nonlocal fault_thread, last_report
                    if broken and received >= min(size // 4, 256 * 1024**2) and not fault_thread:
                        fault_thread = outage(fault)
                    if time.perf_counter() - last_report > 20:
                        emit("download_progress", size=size, bytes=received, MB_s=round(received / (time.perf_counter() - started) / 1e6, 2), outage=broken)
                        last_report = time.perf_counter()

                resume_offsets = []
                while True:
                    try:
                        api.download_file(space, logical, destination, progress=progress, expected_sha256=digest)
                        break
                    except (ClientConnectionError, ClientApiError) as error:
                        if isinstance(error, ClientApiError) and error.status_code not in {408, 429, 500, 502, 503, 504}:
                            raise
                        retries += 1
                        if retries > 15:
                            raise
                        part = destination.with_name(destination.name + ".part")
                        resume_offsets.append(part.stat().st_size if part.exists() else 0)
                        report["recoveries"].append({"direction": "download", "size_bytes": size, "offset": resume_offsets[-1], "outage": broken, "error_type": type(error).__name__, "cause_type": type(error.__cause__).__name__ if error.__cause__ else None, "http_status": getattr(error, "status_code", None)})
                        emit("download_retry", offset=resume_offsets[-1], http_status=getattr(error, "status_code", None), cause=type(error.__cause__).__name__ if error.__cause__ else type(error).__name__)
                        save()
                        time.sleep(max(1, min(60, getattr(error, "retry_after", None) or 0)))
                elapsed = time.perf_counter() - started
                if fault_thread:
                    fault_thread.join(10)
                assert destination.stat().st_size == size
                sample = {"direction": "download", "protocol": "desktop Range + SHA256 + fsync", "size_bytes": size, "seconds": elapsed, "MB_s": size / elapsed / 1e6, "MiB_s": size / elapsed / 1024**2, "sha256_ok": True, "outage": broken, "retries": retries, "resume_offsets": resume_offsets, "fault": fault}
                report["samples"].append(sample)
                save()
                emit("sample", **sample)
                destination.unlink()
                api.delete_file(space, logical)
                # Dedicated disposable server root; purge only synthetic blobs
                # that the API has already moved into its trash directory.
                trash = args.work / "server/storage/default/.trash"
                assert trash.resolve().is_relative_to(args.work.resolve())
                for blob in trash.glob("*.blob"):
                    blob.unlink()
        save()
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
        save()
        raise
    finally:
        api.close()


def setup_network(args, owned_names):
    existing = command("ip", "netns", "list").stdout
    if any(name in existing for name in NAMES):
        raise RuntimeError("QA namespaces already exist; refusing to modify them")
    for name in NAMES:
        command("ip", "netns", "add", name)
        owned_names.append(name)
        command("ip", "-n", name, "link", "set", "lo", "up")
    for endpoint, address, link in ((NAMES[0], CLIENT, "c"), (NAMES[2], SERVER, "s")):
        command("ip", "link", "add", f"csqa-{link}e", "type", "veth", "peer", "name", f"csqa-{link}r")
        command("ip", "link", "set", f"csqa-{link}e", "netns", endpoint)
        command("ip", "link", "set", f"csqa-{link}r", "netns", NAMES[1])
        command("ip", "-n", endpoint, "addr", "add", address + "/24", "dev", f"csqa-{link}e")
        gateway = address.rsplit(".", 1)[0] + ".1"
        command("ip", "-n", NAMES[1], "addr", "add", gateway + "/24", "dev", f"csqa-{link}r")
        command("ip", "-n", endpoint, "link", "set", f"csqa-{link}e", "up")
        command("ip", "-n", NAMES[1], "link", "set", f"csqa-{link}r", "up")
        other = SERVER if endpoint == NAMES[0] else CLIENT
        command("ip", "-n", endpoint, "route", "add", other + "/32", "via", gateway)
        # Do not let large GSO packets bypass the intended packet-rate model.
        ns(endpoint, "ethtool", "-K", f"csqa-{link}e", "tso", "off", "gso", "off", "gro", "off")
        ns(NAMES[1], "ethtool", "-K", f"csqa-{link}r", "tso", "off", "gso", "off", "gro", "off")
        ns(NAMES[1], "tc", "qdisc", "add", "dev", f"csqa-{link}r", "root", "netem", "delay", f"{args.rtt_ms / 2}ms", "rate", f"{args.rate_mbit}mbit", "limit", "10000")
    ns(NAMES[1], "sysctl", "-w", "net.ipv4.ip_forward=1")
    ns(NAMES[1], "iptables", "-P", "FORWARD", "DROP")
    ns(NAMES[1], "iptables", "-A", "FORWARD", "-m", "conntrack", "--ctstate", "ESTABLISHED,RELATED", "-j", "ACCEPT")
    ns(NAMES[1], "iptables", "-A", "FORWARD", "-s", CLIENT, "-d", SERVER, "-p", "tcp", "-m", "multiport", "--dports", "8766,5201", "-j", "ACCEPT")
    ns(NAMES[1], "iptables", "-A", "FORWARD", "-s", CLIENT, "-d", SERVER, "-p", "icmp", "-j", "ACCEPT")


def run_lab(args):
    if sys.platform != "linux" or os.geteuid() != 0:
        raise RuntimeError("Run as root inside the dedicated WSL QA distribution")
    args.work = args.work.resolve()
    if args.work.exists():
        raise RuntimeError("Use a new dedicated --work directory for every run")
    args.work.mkdir(parents=True, mode=0o700)
    source = Path(__file__).resolve().parents[1]
    report = {"kind": "single-PC WSL network namespace emulation; NOT real WAN", "source_commit": args.source_commit, "rtt_ms_configured": args.rtt_ms, "rate_mbit_each_direction": args.rate_mbit, "loss_percent_configured": 0, "size_unit": "decimal GB", "sizes_bytes": args.sizes, "repeats": 1, "platform": platform.platform(), "python": sys.version, "shared_physical_disk": True, "shared_host_cpu_ram": True, "page_cache_not_cleared": True, "samples": [], "errors": []}
    server = None
    client_process = None
    owned_names = []
    monitor_stop = threading.Event()
    server_log = (args.work / "server.log").open("w")
    try:
        setup_network(args, owned_names)
        report["namespaces"] = {name: ns(name, "readlink", "/proc/self/ns/net").stdout.strip() for name in NAMES}
        assert len(set(report["namespaces"].values())) == 3
        report["routes"] = {name: json.loads(command("ip", "-n", name, "-j", "route").stdout) for name in NAMES}
        report["qdisc_before"] = ns(NAMES[1], "tc", "-s", "qdisc", "show").stdout
        server_work = args.recover_work or args.work
        environment = dict(os.environ, PYTHONPATH=str(source), CLOUD_STORAGE_CORE_DATA_DIR=str(server_work / "server"), CLOUD_STORAGE_LAN_ENABLED="1", CLOUD_STORAGE_LAN_PORT="8766", CLOUD_STORAGE_MAX_UPLOAD_GIB="32", CLOUD_STORAGE_REMOTE_ENABLED="0", CLOUD_STORAGE_CLOUDFLARE_ENABLED="0", CLOUD_STORAGE_ZROK_ENABLED="0")
        server = subprocess.Popen(["ip", "netns", "exec", NAMES[2], PYTHON, "-m", "cloud_storage.core"], env=environment, stdout=server_log, stderr=server_log)
        for _ in range(200):
            if server.poll() is not None:
                raise RuntimeError("Server exited during startup; inspect server.log")
            probe = ns(NAMES[2], PYTHON, "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/v1/health',timeout=1).read()", check=False)
            if probe.returncode == 0:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Server startup timed out")
        probe_code = "import socket; s=socket.create_connection((%r,%d),timeout=2); s.close()"
        assert ns(NAMES[0], PYTHON, "-c", probe_code % (SERVER, 8766), check=False).returncode == 0
        report["admin_port_blocked"] = ns(NAMES[0], PYTHON, "-c", probe_code % (SERVER, 8765), check=False).returncode != 0
        ns(NAMES[1], "sysctl", "-w", "net.ipv4.ip_forward=0")
        try:
            report["router_disabled_blocks_connection"] = ns(NAMES[0], PYTHON, "-c", probe_code % (SERVER, 8766), check=False).returncode != 0
        finally:
            ns(NAMES[1], "sysctl", "-w", "net.ipv4.ip_forward=1")
        report["ping"] = ns(NAMES[0], "ping", "-n", "-c", "8", "-i", "0.2", SERVER).stdout
        report["neighbors"] = {name: json.loads(command("ip", "-n", name, "-j", "neigh").stdout) for name in (NAMES[0], NAMES[2])}
        assert report["admin_port_blocked"] and report["router_disabled_blocks_connection"]
        emit("network_verified", **{k: report[k] for k in ("namespaces", "admin_port_blocked", "router_disabled_blocks_connection", "ping")})
        report["iperf"] = []
        for reverse in (False, True):
            iperf = subprocess.Popen(["ip", "netns", "exec", NAMES[2], "iperf3", "-s", "-1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                time.sleep(0.3)
                calibration = ns(NAMES[0], "iperf3", "-c", SERVER, "-t", "10", "-J", *(["-R"] if reverse else []))
                result = json.loads(calibration.stdout)
                report["iperf"].append({"reverse": reverse, "result": result})
                emit("iperf", reverse=reverse, MB_s=result["end"]["sum_received"]["bits_per_second"] / 8e6)
            finally:
                if iperf.poll() is None:
                    iperf.terminate()
                iperf.wait(timeout=5)
        invitation = ns(NAMES[2], PYTHON, __file__, "admin", "recover-invite" if args.recover_work else "invite", "--work", str(server_work), "--sizes", str(args.sizes[0])).stdout
        # Invitation is passed only through the child stdin, never logged.
        client_process = subprocess.Popen(["ip", "netns", "exec", NAMES[0], PYTHON, __file__, "client", "--work", str(args.work), "--sizes", *map(str, args.sizes), *(["--faults"] if args.faults else []), *(["--host-vhd", str(args.host_vhd)] if args.host_vhd else []), *(["--recover-work", str(args.recover_work)] if args.recover_work else [])], stdin=subprocess.PIPE, env=dict(os.environ, PYTHONPATH=str(source)))
        client_process.stdin.write(invitation.encode())
        client_process.stdin.close()

        def monitor():
            import psutil
            with (args.work / "resources.jsonl").open("w") as output:
                while not monitor_stop.wait(2):
                    values = {"time": time.time(), "host_c_free": shutil.disk_usage("/mnt/c").free}
                    values["linux_memory"] = psutil.virtual_memory()._asdict()
                    values["io_pressure"] = Path("/proc/pressure/io").read_text()
                    for label, process in (("server", server), ("client", client_process)):
                        try:
                            item = psutil.Process(process.pid)
                            values[label] = {"rss": item.memory_info().rss, "cpu_seconds": sum(item.cpu_times()[:2]), "io": item.io_counters()._asdict()}
                        except psutil.Error:
                            pass
                    output.write(json.dumps(values) + "\n")
                    output.flush()
                    if values["host_c_free"] < 5_000_000_000:
                        client_process.terminate()
                        emit("disk_guard_stop", free=values["host_c_free"])
                        return
        monitor_thread = threading.Thread(target=monitor, daemon=True)
        monitor_thread.start()
        code = client_process.wait()
        monitor_stop.set()
        monitor_thread.join(5)
        if (args.work / "client-results.json").exists():
            report.update(json.loads((args.work / "client-results.json").read_text()))
        if code:
            raise RuntimeError("Client benchmark failed; preserved partial evidence")
        report["qdisc_after"] = ns(NAMES[1], "tc", "-s", "qdisc", "show").stdout
        report["firewall_after"] = ns(NAMES[1], "iptables", "-L", "FORWARD", "-n", "-v", "-x").stdout
        report["health_after"] = json.loads(ns(NAMES[2], PYTHON, "-c", "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8765/v1/health').read().decode())").stdout)
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        monitor_stop.set()
        if client_process and client_process.poll() is None:
            client_process.terminate()
            try:
                client_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                client_process.kill()
                client_process.wait(timeout=5)
        if server and server.poll() is None:
            ns(NAMES[2], PYTHON, __file__, "admin", "shutdown", "--work", str(args.recover_work or args.work), check=False)
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.terminate()
                server.wait(timeout=10)
        report["server_stopped"] = server is None or server.poll() is not None
        # Names are reserved for this dedicated distro/run. Never modify host
        # Windows interfaces, production services, or the root WSL routing table.
        for name in reversed(owned_names):
            command("ip", "netns", "del", name, check=False)
        report["namespaces_removed"] = not any(name in command("ip", "netns", "list").stdout for name in NAMES)
        server_log.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        emit("report", file=str(args.output), samples=len(report["samples"]), errors=len(report["errors"]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "client", "admin"))
    parser.add_argument("action", nargs="?", choices=("invite", "recover-invite", "quota", "shutdown"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("network-lab-results.json"))
    parser.add_argument("--sizes", type=int, nargs="+", default=[10**9, 10 * 10**9, 20 * 10**9, 30 * 10**9])
    parser.add_argument("--rtt-ms", type=float, default=30)
    parser.add_argument("--rate-mbit", type=int, default=1000)
    parser.add_argument("--source-commit", default="unknown")
    parser.add_argument("--host-vhd", type=Path, help="Dynamic WSL disk on C:, to account for reusable allocated guest blocks")
    parser.add_argument("--recover-work", type=Path, help="Existing private QA work directory with a fully received upload; recover and download it without re-upload")
    parser.add_argument("--faults", action="store_true")
    args = parser.parse_args()
    if any(size <= 0 for size in args.sizes) or args.rtt_ms < 0 or args.rate_mbit <= 0:
        parser.error("Invalid size or link profile")
    {"run": run_lab, "client": client, "admin": admin}[args.mode](args)


if __name__ == "__main__":
    main()
