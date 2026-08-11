from __future__ import annotations

import base64
import ctypes
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
        "cloud-storage-client-catalog.json"
    ),
    "server": (
        "https://github.com/prostoodin1/Cloud-storage/releases/latest/download/"
        "cloud-storage-server-catalog.json"
    ),
}
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_INSTALLER_BYTES = 2 * 1024**3
UPDATE_POLICIES = {"manual", "download", "install"}
UPDATE_CHANNELS = {"stable", "beta"}


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


@dataclass(frozen=True, slots=True)
class UpdatePreferences:
    policy: str = "manual"
    channel: str = "stable"

    def __post_init__(self) -> None:
        if self.policy not in UPDATE_POLICIES or self.channel not in UPDATE_CHANNELS:
            raise ValueError("invalid update preferences")


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
    return _parse_update_info(value, expected_product=expected_product)


def parse_signed_catalog(
    raw: bytes,
    *,
    expected_product: str,
    public_key: str = UPDATE_PUBLIC_KEY,
) -> list[UpdateInfo]:
    """Parse a signed multi-version catalog or a legacy single-version manifest."""
    if len(raw) > MAX_MANIFEST_BYTES:
        raise UpdateError("каталог обновлений слишком большой")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("каталог обновлений повреждён") from exc
    if isinstance(value, dict) and value.get("schema_version") == 1:
        return [
            parse_signed_manifest(
                raw,
                expected_product=expected_product,
                public_key=public_key,
            )
        ]
    try:
        signature = base64.b64decode(str(value["signature"]), validate=True)
        key_bytes = base64.b64decode(public_key, validate=True)
        Ed25519PublicKey.from_public_bytes(key_bytes).verify(
            signature, canonical_manifest_payload(value)
        )
    except InvalidSignature as exc:
        raise UpdateError("цифровая подпись каталога недействительна") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("каталог обновлений повреждён") from exc
    if not isinstance(value, dict) or value.get("schema_version") != 2:
        raise UpdateError("версия каталога обновлений не поддерживается")
    product = str(value.get("product", ""))
    versions = value.get("versions")
    if product != expected_product or product not in DEFAULT_FEEDS:
        raise UpdateError("каталог предназначен для другого приложения")
    if not isinstance(versions, list) or not 1 <= len(versions) <= 100:
        raise UpdateError("список версий в каталоге недействителен")
    result: list[UpdateInfo] = []
    seen: set[tuple[str, str]] = set()
    for item in versions:
        if not isinstance(item, dict):
            raise UpdateError("описание версии в каталоге недействительно")
        entry = dict(item)
        entry["product"] = product
        info = _parse_update_info(entry, expected_product=expected_product)
        identity = (info.version, info.channel)
        if identity in seen:
            raise UpdateError("каталог содержит повторяющуюся версию")
        seen.add(identity)
        result.append(info)
    return sorted(result, key=lambda item: version_key(item.version), reverse=True)


def _parse_update_info(
    value: dict[str, object], *, expected_product: str
) -> UpdateInfo:
    product = str(value.get("product", ""))
    version = str(value.get("version", ""))
    channel = str(value.get("channel", ""))
    package_value = value.get("package")
    if product != expected_product or product not in DEFAULT_FEEDS:
        raise UpdateError("обновление предназначено для другого приложения")
    version_key(version)
    if channel not in UPDATE_CHANNELS or not isinstance(package_value, dict):
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

    @property
    def preferences_path(self) -> Path:
        return self.download_directory / "preferences.json"

    def load_preferences(self) -> UpdatePreferences:
        try:
            value = json.loads(self.preferences_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or value.get("schema_version") != 1:
                raise ValueError
            return UpdatePreferences(
                policy=str(value.get("policy", "manual")),
                channel=str(value.get("channel", "stable")),
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return UpdatePreferences()

    def save_preferences(self, preferences: UpdatePreferences) -> None:
        self.download_directory.mkdir(parents=True, exist_ok=True)
        temporary = self.preferences_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "policy": preferences.policy,
                    "channel": preferences.channel,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(temporary, self.preferences_path)

    def list_versions(self, channel: str = "stable") -> list[UpdateInfo]:
        if channel not in UPDATE_CHANNELS:
            raise ValueError("unknown update channel")
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
        versions = parse_signed_catalog(
            raw,
            expected_product=self.product,
            public_key=self.public_key,
        )
        if channel == "stable":
            versions = [item for item in versions if item.channel == "stable"]
        if not versions:
            raise UpdateError("в выбранном канале пока нет доступных версий")
        return versions

    def check(self, channel: str = "stable") -> UpdateInfo:
        return self.list_versions(channel)[0]

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
            if os.name == "nt":
                _launch_elevated_windows(arguments)
            else:
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


def _launch_elevated_windows(arguments: list[str]) -> None:
    """Launch a verified installer through the Windows UAC broker.

    CreateProcess (and therefore ``subprocess.Popen``) returns Windows error 740
    for an installer whose manifest requires administrator privileges. ShellExecute
    with the ``runas`` verb is the supported way to display the UAC prompt.
    """
    if not arguments:
        raise OSError("installer command is empty")
    executable = str(Path(arguments[0]).resolve())
    parameters = subprocess.list2cmdline(arguments[1:])
    shell_execute = ctypes.windll.shell32.ShellExecuteW
    shell_execute.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell_execute.restype = ctypes.c_void_p
    result = shell_execute(
        None,
        "runas",
        executable,
        parameters,
        str(Path(executable).parent),
        1,
    )
    result_code = int(result or 0)
    if result_code <= 32:
        raise OSError(result_code, "Windows rejected the elevated installer launch")


def is_frozen_windows() -> bool:
    return os.name == "nt" and bool(getattr(sys, "frozen", False))
