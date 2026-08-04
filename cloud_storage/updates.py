from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from cloud_storage import __version__

UPDATE_PUBLIC_KEY = "6MAV324wKv/5LwvAnKMyCk+djeST6b5ufaMBVfe3XWQ="
DEFAULT_FEEDS = {
    "client": (
        "https://github.com/prostoodin1/Cloud-storage/releases/latest/download/"
        "cloud-storage-client-stable.json"
    ),
    "server": (
        "https://github.com/prostoodin1/Cloud-storage/releases/latest/download/"
        "cloud-storage-server-stable.json"
    ),
}
MAX_MANIFEST_BYTES = 256 * 1024
MAX_INSTALLER_BYTES = 2 * 1024**3


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class UpdatePackage:
    url: str
    sha256: str
    size_bytes: int
    filename: str


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    product: str
    version: str
    channel: str
    published_at: str
    release_notes: str
    package: UpdatePackage

    @property
    def newer_than_current(self) -> bool:
        return version_key(self.version) > version_key(__version__)


def canonical_manifest_payload(value: dict[str, object]) -> bytes:
    unsigned = {key: item for key, item in value.items() if key != "signature"}
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_signed_manifest(
    raw: bytes,
    *,
    expected_product: str,
    public_key: str = UPDATE_PUBLIC_KEY,
) -> UpdateInfo:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("манифест обновления слишком большой")
    try:
        value = json.loads(raw.decode("utf-8"))
        signature = base64.b64decode(str(value["signature"]), validate=True)
        key_bytes = base64.b64decode(public_key, validate=True)
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature, canonical_manifest_payload(value)
        )
    except InvalidSignature as exc:
        raise UpdateError("цифровая подпись обновления недействительна") from exc
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("манифест обновления повреждён") from exc

    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise UpdateError("версия манифеста обновления не поддерживается")
    product = str(value.get("product", ""))
    version = str(value.get("version", ""))
    channel = str(value.get("channel", ""))
    package_value = value.get("package")
    if product != expected_product or product not in DEFAULT_FEEDS:
        raise UpdateError("обновление предназначено для другого приложения")
    version_key(version)
    if channel not in {"stable", "beta"} or not isinstance(package_value, dict):
        raise UpdateError("поля манифеста обновления недействительны")
    url = _https_url(str(package_value.get("url", "")))
    sha256 = str(package_value.get("sha256", "")).casefold()
    filename = str(package_value.get("filename", ""))
    try:
        size_bytes = int(package_value.get("size_bytes", 0))
    except (TypeError, ValueError) as exc:
        raise UpdateError("размер пакета обновления недействителен") from exc
    if (
        not re.fullmatch(r"[0-9a-f]{64}", sha256)
        or not 1 <= size_bytes <= MAX_INSTALLER_BYTES
        or not re.fullmatch(r"[A-Za-z0-9._-]{1,180}\.exe", filename)
    ):
        raise UpdateError("описание пакета обновления недействительно")
    return UpdateInfo(
        product=product,
        version=version,
        channel=channel,
        published_at=str(value.get("published_at", ""))[:64],
        release_notes=str(value.get("release_notes", ""))[:4000],
        package=UpdatePackage(url, sha256, size_bytes, filename),
    )


class UpdateService:
    def __init__(
        self,
        product: str,
        data_directory: Path,
        *,
        feed_url: str | None = None,
        public_key: str = UPDATE_PUBLIC_KEY,
    ) -> None:
        if product not in DEFAULT_FEEDS:
            raise ValueError("unknown update product")
        self.product = product
        self.data_directory = data_directory
        environment_name = f"CLOUD_STORAGE_{product.upper()}_UPDATE_URL"
        self.feed_url = _https_url(
            feed_url or os.environ.get(environment_name, DEFAULT_FEEDS[product])
        )
        self.public_key = public_key
        self.download_directory = data_directory / "updates"

    def check(self) -> UpdateInfo:
        request = urllib.request.Request(
            self.feed_url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"CloudStorage/{__version__} ({self.product}; Windows)",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                _https_url(response.geturl())
                raw = _read_limited(response, MAX_MANIFEST_BYTES)
        except (OSError, urllib.error.URLError) as exc:
            raise UpdateError("не удалось получить сведения об обновлении") from exc
        return parse_signed_manifest(
            raw,
            expected_product=self.product,
            public_key=self.public_key,
        )

    def download(self, info: UpdateInfo) -> Path:
        if info.product != self.product:
            raise UpdateError("пакет предназначен для другого приложения")
        self.download_directory.mkdir(parents=True, exist_ok=True)
        destination = (self.download_directory / info.package.filename).resolve()
        if self.download_directory.resolve() not in destination.parents:
            raise UpdateError("небезопасное имя пакета обновления")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="update-", suffix=".download", dir=self.download_directory
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        received = 0
        request = urllib.request.Request(
            info.package.url,
            headers={"User-Agent": f"CloudStorage/{__version__} ({self.product}; Windows)"},
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
                _https_url(response.geturl())
                while chunk := response.read(1024 * 1024):
                    received += len(chunk)
                    if received > info.package.size_bytes or received > MAX_INSTALLER_BYTES:
                        raise UpdateError("размер скачанного пакета превышает манифест")
                    digest.update(chunk)
                    out.write(chunk)
                out.flush()
                os.fsync(out.fileno())
            if received != info.package.size_bytes:
                raise UpdateError("пакет обновления скачан не полностью")
            if digest.hexdigest() != info.package.sha256:
                raise UpdateError("SHA-256 пакета обновления не совпадает")
            os.replace(temporary, destination)
            self._write_state(info, destination)
            return destination
        except UpdateError:
            raise
        except (OSError, urllib.error.URLError) as exc:
            raise UpdateError("не удалось скачать пакет обновления") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def launch_installer(self, installer: Path, info: UpdateInfo) -> None:
        installer = installer.resolve(strict=True)
        if (
            info.product != self.product
            or self.download_directory.resolve() not in installer.parents
            or installer.suffix.casefold() != ".exe"
            or installer.name != info.package.filename
        ):
            raise UpdateError("установщик находится вне защищённого каталога обновлений")
        size_bytes, sha256 = _file_digest(installer)
        if size_bytes != info.package.size_bytes or sha256 != info.package.sha256:
            raise UpdateError("установщик изменён после скачивания; запуск отменён")
        log_path = self.download_directory / f"install-{self.product}.log"
        arguments = [
            str(installer),
            "/VERYSILENT",
            "/SUPPRESSMSGBOXES",
            "/NORESTART",
            "/CLOSEAPPLICATIONS",
            f"/LOG={log_path}",
        ]
        try:
            subprocess.Popen(arguments, close_fds=True)
        except OSError as exc:
            raise UpdateError("не удалось запустить установщик обновления") from exc

    def _write_state(self, info: UpdateInfo, destination: Path) -> None:
        state = self.download_directory / "update-state.json"
        temporary = state.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "product": info.product,
                    "version": info.version,
                    "path": str(destination),
                    "sha256": info.package.sha256,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, state)


def version_key(value: str) -> tuple[int, int, int, int, str]:
    match = re.fullmatch(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})(?:(a|b|rc)(\d{1,6}))?", value)
    if not match:
        raise UpdateError("версия обновления имеет неверный формат")
    major, minor, patch = (int(match.group(index)) for index in range(1, 4))
    label = match.group(4) or ""
    number = int(match.group(5) or 0)
    rank = {"a": 0, "b": 1, "rc": 2, "": 3}[label]
    return major, minor, patch, rank, f"{number:06d}"


def _https_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise UpdateError("адрес обновления должен использовать HTTPS")
    return value.strip()


def _read_limited(stream: BinaryIO, maximum: int) -> bytes:
    result = stream.read(maximum + 1)
    if len(result) > maximum:
        raise UpdateError("ответ сервера обновлений слишком большой")
    return result


def _file_digest(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_INSTALLER_BYTES:
                raise UpdateError("установщик превышает допустимый размер")
            digest.update(chunk)
    return size, digest.hexdigest()


def is_frozen_windows() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))
