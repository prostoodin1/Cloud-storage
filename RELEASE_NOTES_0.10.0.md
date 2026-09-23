# Cloud Storage 0.10.0

## Windows desktop release candidate

- Versioned the Server Manager, Desktop Client, Core, Windows drive and installers as 0.10.0.
- Made the Core single-instance lock specific to its canonical data directory. This avoids
  collisions with other Cloud Storage installations and their older global Windows mutex.
- Added Cloudflare Tunnel as the only advertised public-tunnel workflow. The Core exposes a
  separate loopback client gateway, keeps the Tunnel token in a restricted file, and can install
  the official `cloudflared` binary after checking its SHA-256 digest.
- Kept the dynamic five-minute pairing code shared by Desktop Client and browser. Packaged QA
  confirms that both clients see the same account and spaces, can create folders and round-trip
  uploads/downloads, and keep their session and files across a Core restart.
- Combined inbound and outbound transfer history into one Manager page; hid the paused offline
  sync entry from the Client navigation.
- The 0.10.0 migration creates and verifies a SQLite backup before schema changes. Background
  automations remain disabled in this release line; settings not shown in the reduced System page
  are preserved by the Core settings merge.
- Mobile applications were not rebuilt or modified; they remain on their previous release.

## Verification

- Python: 197 tests passed.
- Go Core: `go test ./...` passed.
- Windows drive: `go test ./...` passed.
- Packaged Windows QA passed for the Manager and Container Manager payloads, both stable installer
  bootstrappers, Go Core startup, web assets, pairing, authorization, quotas, file and folder
  round-trips, browser sessions, public shares, diagnostics, archive/restore, user isolation, clean
  shutdown, and restart persistence.
- The stable installers and versioned setup EXEs are published with this GitHub release.

Cloudflare reachability over a real public hostname still needs a configured Tunnel token and DNS
hostname; the packaged test uses the loopback gateway and does not claim an Internet e2e test.
