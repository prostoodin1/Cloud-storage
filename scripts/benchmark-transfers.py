"""Compare isolated source revisions over real loopback TLS, not WAN claims.

Example: python scripts/benchmark-transfers.py --source . --output outputs/after.json
Use the same Python runtime, size and repeat count for both revisions.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--source", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--size-mib", type=int, default=128)
parser.add_argument("--repeats", type=int, default=3)
parser.add_argument("--request-delay-ms", type=float, default=0)
parser.add_argument("--profile-server", action="store_true")
parser.add_argument("--parallel", type=int, default=1)
parser.add_argument("--core-exe", type=Path)
parser.add_argument("--public-tunnel", type=Path,
                    help="Temporary cloudflared Quick Tunnel; synthetic data only")
args = parser.parse_args()
source = args.source.resolve()
args.output = args.output.resolve()
args.output.parent.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(source))
from cloud_storage.client.api_client import ClientApi, ClientApiError  # noqa: E402
from cloud_storage.pairing import parse_dynamic_pairing_code  # noqa: E402


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


run = args.output.parent / (args.output.stem + "-runtime")
run.mkdir(exist_ok=False)
p, lp = port(), port()
base = f"http://127.0.0.1:{p}"
env = dict(os.environ, PYTHONPATH=str(source), CLOUD_STORAGE_CORE_DATA_DIR=str(run / "core"),
           CLOUD_STORAGE_CORE_PORT=str(p), CLOUD_STORAGE_LAN_ENABLED="1",
           CLOUD_STORAGE_LAN_PORT=str(lp), CLOUD_STORAGE_DISCOVERY_PORT=str(port()))
if args.public_tunnel:
    cp = port()
    env.update(CLOUD_STORAGE_CLOUDFLARE_ENABLED="1", CLOUD_STORAGE_CLOUDFLARE_PORT=str(cp),
               CLOUD_STORAGE_CLOUDFLARE_EXECUTABLE=str(args.public_tunnel.resolve()),
               CLOUD_STORAGE_REMOTE_PAIRING_ENABLED="1")
results = {"source": str(source), "size_mib": args.size_mib, "repeats": args.repeats,
           "request_delay_ms": args.request_delay_ms, "route": "loopback TLS, not internet",
           "samples": [], "errors": []}
log = (run / "core.log").open("w", encoding="utf-8")
command = [sys.executable, "-m", "cloud_storage.core"]
if args.core_exe:
    command = [str(args.core_exe.resolve())]
    env["CLOUD_STORAGE_LEGACY_CORE"] = str(args.core_exe.resolve().parent.parent / "CloudStorageLegacyCore/CloudStorageLegacyCore.exe")
    env["CLOUD_STORAGE_LEGACY_PORT"] = str(port())
    results["core_executable"] = str(args.core_exe.resolve())
    if args.profile_server:
        parser.error("--profile-server applies only to a source runtime")
if args.profile_server:
    profiling = '''
import atexit,cProfile,functools,json,runpy,threading,time
from pathlib import Path
from cloud_storage.core.storage import StorageService,UploadSession
from cloud_storage.core.main import CoreServerGroup
stats={};lock=threading.Lock()
def wrap(cls,name):
    original=getattr(cls,name)
    @functools.wraps(original)
    def measured(*args,**kwargs):
        start=time.perf_counter()
        try:return original(*args,**kwargs)
        finally:
            elapsed=time.perf_counter()-start
            with lock:
                current=stats.setdefault(cls.__name__+'.'+name,{'calls':0,'seconds':0})
                current['calls']+=1;current['seconds']+=elapsed
    setattr(cls,name,measured)
for cls,names in ((UploadSession,('write','commit')), (StorageService,('_update_transfer_network','_finalize_inbound_transfer','append_resumable_upload','complete_resumable_upload'))):
    for name in names:wrap(cls,name)
original_lan=CoreServerGroup._run_lan
def profile_lan(self):
    profiler=cProfile.Profile()
    profiler.enable()
    try:return original_lan(self)
    finally:
        profiler.disable()
        profiler.dump_stats(PROFILE_PSTATS)
CoreServerGroup._run_lan=profile_lan
atexit.register(lambda:Path(PROFILE_PATH).write_text(json.dumps(stats,indent=2)))
runpy.run_module('cloud_storage.core',run_name='__main__')
'''.replace("PROFILE_PATH", repr(str(run / "server-profile.json"))).replace("PROFILE_PSTATS", repr(str(run / "tls-profile.pstats")))
    command = [sys.executable, "-c", profiling]
process = subprocess.Popen(command, cwd=source, env=env,
                           stdout=log, stderr=log,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def request(method, route, body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    h = dict(headers or {})
    if data is not None:
        h["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(base + route, data=data, method=method, headers=h), timeout=30) as r:
        return json.load(r)


manager = None
api = None
tunnel = None
tunnel_log = None
try:
    for _ in range(200):
        try:
            if request("GET", "/v1/health")["status"] == "ok":
                break
        except (OSError, ValueError):
            if process.poll() is not None:
                raise RuntimeError("Core exited during startup") from None
            time.sleep(.1)
    secrets = json.loads((run / "core/core-secrets.json").read_text())
    manager = {"Authorization": "Bearer " + secrets["manager_token"]}
    code = request("GET", "/v1/admin/dynamic-pairing-code", headers=manager)["code"]
    invite = parse_dynamic_pairing_code(code)
    client_url = f"https://127.0.0.1:{lp}"
    fingerprint = invite.certificate_fingerprint
    if args.public_tunnel:
        tunnel_log = (run / "tunnel.log").open("w", encoding="utf-8")
        tunnel = subprocess.Popen([str(args.public_tunnel.resolve()), "tunnel", "--no-autoupdate",
                                   "--url", f"http://127.0.0.1:{cp}", "--protocol", "http2"],
                                  stdout=tunnel_log, stderr=tunnel_log,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(150):
            found = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com",
                              (run / "tunnel.log").read_text(errors="replace"))
            if found:
                client_url = found.group()
                break
            time.sleep(.2)
        else:
            raise RuntimeError("Quick Tunnel supplied no public URL")
        fingerprint = ""  # Public CA validation, not the local self-signed pin.
        for _ in range(30):
            try:
                ClientApi(client_url).health()
                break
            except (ConnectionError, ClientApiError):
                time.sleep(1)
        else:
            raise RuntimeError("Public tunnel did not become healthy")
        results["route"] = "public HTTPS Quick Tunnel, same physical PC at both ends"
    api = ClientApi(client_url, certificate_fingerprint=fingerprint)
    paired = api.redeem_invitation(code, None, "Throughput QA", "Windows")
    api.token = paired["device_token"]
    space = api.list_spaces()[0]["id"]
    file = run / "random.bin"
    digest = hashlib.sha256()
    remaining = args.size_mib*1024**2
    with file.open("wb") as out:
        while remaining:
            data = os.urandom(min(4*1024**2, remaining))
            digest.update(data)
            out.write(data)
            remaining -= len(data)
    if args.parallel > 1:
        def parallel_upload(number):
            client = ClientApi(client_url, token=api.token, certificate_fingerprint=fingerprint)
            try:
                started = time.perf_counter()
                result = client.upload_file(space, f"parallel-{number}.bin", file)
                assert result["sha256"] == digest.hexdigest()
                return time.perf_counter() - started
            finally:
                if hasattr(client, "close"):
                    client.close()
        started = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            durations = list(pool.map(parallel_upload, range(args.parallel)))
        seconds = time.perf_counter() - started
        results["samples"].append({"kind": f"parallel_upload_{args.parallel}", "seconds": seconds,
                                  "MiB_s": args.parallel * args.size_mib / seconds,
                                  "per_file_seconds": durations, "sha256_ok": True})
        print("parallel aggregate MiB/s", round(args.parallel * args.size_mib / seconds, 2), flush=True)
    for repeat in range(args.repeats):
        name = f"direct-{repeat}.bin"
        start = time.perf_counter()
        api.upload_file(space, name, file)
        seconds = time.perf_counter()-start
        results["samples"].append({"kind": "direct_upload", "seconds": seconds,
                                  "MiB_s": args.size_mib/seconds})
        start = time.perf_counter()
        api.download_file(space, name, run / f"download-{repeat}.bin", expected_sha256=digest.hexdigest())
        seconds = time.perf_counter()-start
        results["samples"].append({"kind": "download_sha256", "seconds": seconds,
                                  "MiB_s": args.size_mib/seconds, "sha256_ok": True})
        start = time.perf_counter()
        upload = api.create_resumable_upload(space, f"resume-{repeat}.bin", file.stat().st_size,
                                              sha256=digest.hexdigest())
        chunk_size = min(32*1024**2, int(upload.get("max_chunk_bytes", 4*1024**2)))
        offset = 0
        with file.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                if args.request_delay_ms:
                    time.sleep(args.request_delay_ms/1000)
                upload = api.append_resumable_upload(upload["id"], offset, chunk)
                offset = upload["received_bytes"]
        record = api.complete_resumable_upload(upload["id"])
        seconds = time.perf_counter()-start
        assert record["sha256"] == digest.hexdigest()
        results["samples"].append({"kind": "resumable_upload", "seconds": seconds,
                                  "MiB_s": args.size_mib/seconds, "chunk_bytes": chunk_size,
                                  "sha256_ok": True})
        args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(repeat+1, [(s["kind"], round(s["MiB_s"], 2)) for s in results["samples"][-3:]], flush=True)
except Exception as exc:
    results["errors"].append({"type": type(exc).__name__, "message": str(exc)})
    raise
finally:
    if api and hasattr(api, "close"):
        api.close()
    if tunnel and tunnel.poll() is None:
        tunnel.terminate()
        tunnel.wait(timeout=15)
    if tunnel_log:
        tunnel_log.close()
    if process.poll() is None:
        try:
            request("POST", "/v1/admin/shutdown", headers=manager)
            process.wait(timeout=15)
        except Exception:
            process.terminate()
            process.wait(timeout=10)
    results["server_stopped"] = process.poll() is not None
    if args.public_tunnel:
        results["tunnel_stopped"] = tunnel is None or tunnel.poll() is not None
    args.output.write_text(json.dumps(results, indent=2), encoding="utf-8")
    log.close()
