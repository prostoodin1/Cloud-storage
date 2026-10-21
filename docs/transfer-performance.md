# Transfer throughput and failure handling

This candidate targets WAN transfers using the existing v1 protocol. It does not
promise 100–200 MB/s over arbitrary internet connections or on every device.

## Changes

- Upload progress updates are coalesced at 16 MiB or 500 ms. Final file metadata,
  SHA-256, fsync and SQLite FULL durability remain authoritative and unchanged.
- Direct uploads buffer at most 4 MiB of network payload before handing the
  joined buffer to a disk worker. Joining, writing and hashing run off the network
  loop. ASGI frame boundaries are independent of the storage write size.
- The server admits four active PUT/PATCH/complete operations across all its
  listeners. Excess work receives HTTP 503 and Retry-After: 2 instead of waiting
  in an unbounded queue. A stalled request body receives HTTP 408 after 30 seconds
  without another body frame. These are admission limits, not bandwidth limits.
- Cancellation waits for the current disk operation to finish before closing its
  file or releasing its admission slot, including repeated cancellation.
- Resumable session responses advertise optional max_chunk_bytes=33554432. Old
  clients can continue sending their existing blocks. New desktop clients cap
  their allocation at 32 MiB; mobile clients cap native Blob slices at 16 MiB.
  Servers without this field retain the 4 MiB client fallback. Invalid hints do
  not trigger unbounded allocations or byte-at-a-time transfer loops.
- Desktop JSON requests reuse one connection per worker thread. Idle connections
  are discarded before reuse after two seconds. A stale connection may retry one
  GET; POST/PATCH are never blindly replayed. A lost acknowledgement is resolved
  by querying the durable resumable session offset on the next attempt.
- Desktop and mobile queues retry 408/429/502/503/504. Mobile foreground work is
  limited to two simultaneous transfers and retries after a delay with jitter.
- Desktop downloads validate declared length and resumed Content-Range before
  replacing the destination. Mobile downloads use a separate .cloud-part file
  and check a known expected size before moving the completed native file to its
  destination. SHA-256 verification remains enabled on the desktop download path;
  mobile native filesystem behavior still requires real Android/iOS testing.
- Persisted download progress is throttled; final progress is stored precisely.
- Graceful HTTP shutdown has a three-second deadline so idle TLS peers cannot
  indefinitely pin a restart. This is not a deadline for ordinary transfers.

Four simultaneous maximum resumable requests can retain about 256 MiB of request
payload while their buffers are joined, plus ASGI/TLS/runtime buffers. Direct PUT
uses smaller buffers. This is not a whole-process RAM ceiling; it should be
measured on the smallest intended server before increasing concurrency.

## Measured candidate, 2026-10-21

Windows 11 / Ryzen 7 4800H; same Python 3.13 environment and C: storage; loopback
TLS with certificate pinning. A random 128 MiB source file, three repetitions,
SHA-256 checked. Baseline: released v0.10.8b2 source. Candidate starts from main
2bb520f81eab9b2747615350355070202d8710e7. The candidate and baseline were run
sequentially; this is not a test between two physical devices or through WAN.

| Operation | Baseline median, MiB/s | Candidate median, MiB/s |
| --- | ---: | ---: |
| Direct upload | 13.72 | 72.47 |
| Resumable upload, including final verification | 17.98 | 59.93 |
| Download with SHA-256 and fsync | 84.48 | 140.84 |

One four-file parallel upload sample reached 83.99 MiB/s **in aggregate**, not per
file. Profiling overhead and measurements concurrent with dependency installation
are excluded from the table. OS cache was not flushed. These numbers do not
establish a stable sustained maximum. The 100–200 MB/s upload goal remains unmet.

The frozen Windows candidate also passed 23 packaged checks. Three 128 MiB
loopback TLS samples measured medians of 74.85 MiB/s direct upload, 59.80 MiB/s
resumable upload and 142.63 MiB/s verified download. A separate single 16 MiB
sample through a public Quick Tunnel measured 13.16, 10.78 and 13.25 MiB/s,
respectively. Both WAN endpoints were on this PC; this is not an independent
network/device test. The Windows client and compatibility engine were rebuilt;
unchanged Go supervisor/service, managers and WinFsp executable are from v0.10.8b2.

Run the same comparison with scripts/benchmark-transfers.py. It accepts --source,
--output, --size-mib, --repeats and --parallel; --profile-server adds worker timing
and TLS-thread profiling. --request-delay-ms introduces an artificial delay before
each resumable chunk; it is not an internet measurement. The script creates only
isolated servers and leaves synthetic test data in its output runtime directory.

## Validation and promotion gates

Automated regressions cover overload with responsive health checks, disk write
failure, stalled bodies, repeated cancellation, negotiated blocks, malformed body
lengths, idle keep-alive recovery, thread isolation, ambiguous mutation responses,
and incomplete or invalid-range downloads. Existing restart, quota, access,
pairing and filesystem tests remain applicable. Mobile protocol tests stub native
file operations; they do not validate physical devices or native crash behavior.

Before promoting the candidate:

1. Test two physical devices on independent internet connections. Record the
   server's measured outgoing bandwidth, recipient's incoming bandwidth, route,
   RTT, disk throughput, CPU and memory; separate direct TLS from a relay/tunnel.
2. For 200 MB/s, provide at least 1.6 Gbit/s useful end-to-end bandwidth plus
   overhead. A 1 Gbit/s link has a raw ceiling of 125 MB/s. A tunnel cannot create
   bandwidth absent from either endpoint, and its own limits must be measured.
3. Test supported Windows, Android and iOS clients against both old and new
   servers. Include a low-memory phone, sleep/wake and switching Wi-Fi/mobile data.
4. Interrupt the network and restart the server between and during chunks. Verify
   full SHA-256 after recovery and verify that no existing local destination was
   replaced by an incomplete download.
5. Exercise disk-full/quota boundaries, disk write errors, multiple users and
   prolonged traffic. Confirm bounded admission, visible errors, no busy retry
   loops and no unbounded growth in memory, staging or queued jobs.

Next performance work should follow profiling of the actual server and public
route. Native TLS/data transport is a candidate for investigation; moving the
security boundary or weakening durable writes is not justified by these results.
