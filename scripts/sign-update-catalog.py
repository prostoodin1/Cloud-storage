from __future__ import annotations

import argparse
import base64
import json
from datetime import UTC, datetime
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def canonical_payload(value: dict[str, object]) -> bytes:
    return json.dumps(
        {key: item for key, item in value.items() if key != "signature"},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build and sign a multi-version Cloud Storage update catalog"
    )
    parser.add_argument("--key", required=True, type=Path)
    parser.add_argument("--product", required=True, choices=("client", "server"))
    parser.add_argument("--manifest", required=True, action="append", type=Path)
    parser.add_argument("--existing-catalog", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    private_key = serialization.load_pem_private_key(args.key.read_bytes(), password=None)
    if not isinstance(private_key, Ed25519PrivateKey):
        raise TypeError("the update key must be an Ed25519 private key")
    versions: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    if args.existing_catalog is not None:
        existing = json.loads(args.existing_catalog.read_text(encoding="utf-8"))
        if (
            not isinstance(existing, dict)
            or existing.get("schema_version") != 2
            or existing.get("product") != args.product
            or not isinstance(existing.get("versions"), list)
        ):
            raise ValueError(f"invalid existing {args.product} catalog")
        try:
            private_key.public_key().verify(
                base64.b64decode(str(existing.get("signature", "")), validate=True),
                canonical_payload(existing),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ValueError("existing catalog signature is invalid") from exc
        for item in existing["versions"]:
            if not isinstance(item, dict):
                raise ValueError("existing catalog contains an invalid version")
            identity = (str(item.get("version", "")), str(item.get("channel", "")))
            if not all(identity):
                raise ValueError("existing catalog version identity is incomplete")
            identities.add(identity)
            versions.append(dict(item))
    for manifest_path in args.manifest:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            not isinstance(manifest, dict)
            or manifest.get("schema_version") != 1
            or manifest.get("product") != args.product
        ):
            raise ValueError(f"invalid {args.product} manifest: {manifest_path}")
        identity = (str(manifest.get("version", "")), str(manifest.get("channel", "")))
        if identity in identities:
            versions = [
                item
                for item in versions
                if (str(item.get("version", "")), str(item.get("channel", ""))) != identity
            ]
        identities.add(identity)
        versions.append(
            {
                "version": manifest.get("version"),
                "channel": manifest.get("channel"),
                "published_at": manifest.get("published_at"),
                "release_notes": manifest.get("release_notes"),
                "package": manifest.get("package"),
            }
        )
    versions.sort(
        key=lambda item: tuple(int(part) for part in str(item.get("version", "0")).split(".")),
        reverse=True,
    )
    value: dict[str, object] = {
        "schema_version": 2,
        "product": args.product,
        "published_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "versions": versions,
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
    print(f"Signed {args.product} update catalog with {len(versions)} versions: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
