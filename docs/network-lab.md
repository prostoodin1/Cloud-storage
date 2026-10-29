# Isolated transfer lab

`scripts/network-lab.py` runs in a **dedicated disposable Linux/WSL distribution as root**. It creates three network namespaces and two veth subnets:

```text
client 10.203.11.2/24 <---> router 10.203.11.1 / 10.203.22.1 <---> server 10.203.22.2/24
```

Endpoints have no default route, no host-network interface and no common broadcast domain. Each endpoint has only a host route to the other through the router. The router's forwarding policy drops traffic except established replies, client-to-server HTTPS/iperf and diagnostic ICMP. Administrative HTTP remains on the server namespace's loopback. Removing forwarding must break a new client connection. Namespace IDs, routes, neighbors, ping, firewall and traffic-control counters are recorded.

The default profile applies 15 ms egress delay on each router interface (30 ms round trip), 1 Gbit/s per direction, MTU 1500, and no intentional packet loss. TSO/GSO/GRO are disabled on the lab's veth links. Actual TCP calibration is measured separately with single-stream iperf3 in both directions. These settings describe an experiment, not the measured properties of two real cities.

## Runtime

Required system tools: Python 3.11+, iproute2 including `tc`, iptables, ethtool, ping, iperf3. Install the project's headless Core dependencies in a virtual environment: fastapi, uvicorn, psutil, cryptography, argon2-cffi and qrcode. The GUI/PySide6 runtime is not required by this lab.

Use the same committed source for both endpoints. Set `PYTHONPATH` to that source. Example inside the dedicated Linux distribution:

```sh
export PYTHONPATH=/opt/cloudstorage-source
/opt/cloudstorage-qa/bin/python /opt/cloudstorage-source/scripts/network-lab.py run \
  --work /var/lib/cloudstorage-qa-run-unique \
  --output /mnt/c/path/to/results.json \
  --sizes 1000000000 10000000000 20000000000 30000000000 \
  --rate-mbit 1000 --rtt-ms 30 --faults \
  --source-commit YOUR_FULL_COMMIT_SHA
```

The work directory must not exist. Names `csqa-client`, `csqa-router`, and `csqa-server` must be unused. This script is specifically for a disposable WSL environment and checks physical host free space through `/mnt/c`; adapt this guard before running on another platform. It never changes Windows interfaces or the installed Cloud Storage service. It stops its processes and removes the namespaces it created when finished. It retains small logs, metadata and secret runtime files under the private work directory; do not publish that directory.

For a dynamic WSL disk on C:, `--host-vhd /mnt/c/path/to/ext4.vhdx` accounts for already allocated but reusable guest blocks in the preflight disk check. The separate low-free-host-space guard remains active during the transfer. Without this option the preflight is conservative and can refuse a later large file even though earlier deleted guest files left enough reusable space inside the VHD.

If the original client timed out at completion but the server retained the fully received upload, a separate run can recover that test account and download its existing file:

```sh
python scripts/network-lab.py run --work /var/lib/new-recovery-run \
  --recover-work /var/lib/original-private-qa-run --sizes 20000000000 \
  --output /mnt/c/path/to/recovery.json
```

Recovery deliberately issues a fresh invitation for the original test user through the server's private administrative API. It queries that QA database for the fully received upload and repeats the idempotent completion call. The recovery download starts a fresh full-size measurement; an old run's partial download is retained separately as failure evidence. Its speed excludes manual diagnosis and restart time, which must be reported separately. Temporary HTTP 408/429/5xx responses are recorded and retried by the lab driver; permanent errors such as 401/403/404/409 are not hidden. This changes the test driver, not the product client or server code.

## Measurements and data

- Sizes are decimal bytes: 1 GB is 1,000,000,000 bytes. Reports provide MB/s and MiB/s.
- Generate real random bytes on disk, hashing them during generation. Generation is timed separately and excluded from transfer speed.
- Upload through the real desktop ClientApi resumable protocol, capped at the desktop's 16 MiB chunks. The server validates the expected SHA-256 at commit. This is the API client and storage engine, not GUI automation or a frozen Windows executable test.
- If completion times out, the lab can repeat that specific idempotent endpoint to retrieve the committed result, recording completion retries. This is not an automatic replay of arbitrary mutations.
- Download through ClientApi with expected SHA-256, Range recovery, file fsync, final hash verification and final rename. All of these are inside the measured operation.
- With `--faults`, repeat the first size with a three-second router TCP-reset outage during upload and download. The lab explicitly retries the API after failure and queries upload offsets; it records retry counts and retained download offsets. This proves protocol recovery under the lab driver, not unattended behavior of a particular GUI or mobile OS.
- Upload and download are sequential, one file/client at a time. The default run has one repetition per size, not statistical medians or guaranteed minimum throughput.
- Files are removed after a successful round trip; only generated blobs already moved by the API into this lab server's trash are physically purged. Source is removed before downloading the final variant to keep large payload storage near two copies. Allow at least 68 GB of host free space for the 30 GB case.
- A resource sampler records RSS, process CPU time, process I/O and host free disk every two seconds. This can be used to distinguish disk/CPU pressure from network limits.

The two endpoints share a kernel, physical disk, CPU and RAM. Page caches are not cleared. TLS is enabled and its self-signed certificate fingerprint is validated. Root namespace control traffic is not a transfer bypass: file data crosses the routed veth path. No public internet, carrier NAT, Wi-Fi radio, real independent device, thermal endurance or mobile background scheduling is represented.

The measured endpoint uses the Core LAN TLS listener through a routed network. Public remote-session approval, external-listener policy, public DNS and relay/tunnel services are outside this lab's coverage.

The isolated server's per-file limit is raised from its default 20 GiB to 32 GiB for this run, and the test account quota is 64 GiB. Production settings remain unchanged. A successful 30 GB result therefore does not mean the default production configuration accepts 30 GB files.
