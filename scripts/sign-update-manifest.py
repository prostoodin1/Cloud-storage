from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_payload(value: dict[str, object]) -> bytes:
    return json.dumps(
        {key: item for key, item in value.items() if key != "signature"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Sign a Cloud Storage update manifest")
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--product", required=True, choices=("client", "server"))
    parser.add_argument("--version", required=True)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument("--notes", default="")
    args = parser.parse_args()

    private_key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("the update key must be an Ed25519 private key")
    size_bytes, sha256 = file_digest(args.installer)
    value: dict[str, object] = {
        "schema_version": 1,
        "product": args.product,
        "version": args.version,
        "channel": args.channel,
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "release_notes": args.notes,
        "package": {
            "url": args.url,
            "filename": args.installer.name,
            "size_bytes": size_bytes,
            "sha256": sha256,
        },
    }
    value["signature"] = base64.b64encode(
        private_key.sign(canonical_payload(value))
    ).decode("ascii")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(args.output)
    print(f"Signed {args.product} update manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
