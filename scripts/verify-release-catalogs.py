"""Verify a recovered feed before replacing any signed catalog on GitHub."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from cloud_storage.updates import UPDATE_PUBLIC_KEY, UpdateError, parse_signed_catalog


def verify(release: Path, existing: Path, repository: str, version: str, public_key: str = UPDATE_PUBLIC_KEY) -> None:
    for product in ("client", "server"):
        name = f"cloud-storage-{product}-catalog.json"
        candidates = parse_signed_catalog((release / name).read_bytes(), expected_product=product, public_key=public_key)
        previous = parse_signed_catalog((existing / name).read_bytes(), expected_product=product, public_key=public_key)
        indexed = {item.version: item for item in candidates}
        if version not in indexed:
            raise UpdateError(f"{product}: requested version missing from recovered catalog")
        selected = indexed[version]
        expected = f"https://github.com/{repository}/releases/download/v{version}/{selected.package.filename}"
        if selected.package.url != expected:
            raise UpdateError(f"{product}: package does not belong to the requested release")
        payload = (release / selected.package.filename).resolve(strict=True)
        if release.resolve() not in payload.parents or payload.stat().st_size != selected.package.size_bytes:
            raise UpdateError(f"{product}: recovered installer has invalid size or location")
        with payload.open("rb") as handle:
            if hashlib.file_digest(handle, "sha256").hexdigest() != selected.package.sha256:
                raise UpdateError(f"{product}: recovered installer SHA-256 mismatch")
        for old in previous:
            replacement = indexed.get(old.version)
            if replacement is None or replacement.package != old.package or replacement.channel != old.channel:
                raise UpdateError(f"{product}: recovery would remove or change an existing version: {old.version}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--existing", required=True, type=Path)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    verify(args.release, args.existing, args.repository, args.version)


if __name__ == "__main__":
    main()
