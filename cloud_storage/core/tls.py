from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psutil
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from cloud_storage.core.config import CoreConfig, _restrict_secret_file


@dataclass(frozen=True, slots=True)
class TlsIdentity:
    certificate_path: str
    private_key_path: str
    fingerprint: str

    @property
    def display_fingerprint(self) -> str:
        return ":".join(
            self.fingerprint[index : index + 2].upper()
            for index in range(0, len(self.fingerprint), 2)
        )


def load_or_create_tls_identity(config: CoreConfig) -> TlsIdentity:
    config.ensure_directories()
    certificate_path = config.tls_certificate_path
    private_key_path = config.tls_private_key_path
    if certificate_path.exists() != private_key_path.exists():
        raise RuntimeError(
            "TLS identity is incomplete; restore both certificate files or remove both explicitly"
        )
    if not certificate_path.exists():
        _generate_identity(config)
    try:
        certificate = x509.load_pem_x509_certificate(certificate_path.read_bytes())
        private_key = serialization.load_pem_private_key(
            private_key_path.read_bytes(), password=None
        )
        public_key = certificate.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        private_public_key = private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        if public_key != private_public_key:
            raise ValueError("certificate and private key do not match")
    except (OSError, ValueError, TypeError) as exc:
        raise RuntimeError("TLS identity is damaged; refusing to replace it silently") from exc
    _restrict_secret_file(private_key_path)
    return TlsIdentity(
        certificate_path=str(certificate_path),
        private_key_path=str(private_key_path),
        fingerprint=certificate.fingerprint(hashes.SHA256()).hex(),
    )


def local_ipv4_addresses() -> list[str]:
    addresses: set[str] = set()
    for values in psutil.net_if_addrs().values():
        for value in values:
            if value.family != socket.AF_INET:
                continue
            try:
                address = ipaddress.ip_address(value.address)
            except ValueError:
                continue
            if address.is_loopback or address.is_link_local or address.is_unspecified:
                continue
            addresses.add(str(address))
    ordered = sorted(addresses, key=lambda value: tuple(int(part) for part in value.split(".")))
    preferred = _preferred_route_address()
    if preferred in ordered:
        ordered.remove(preferred)
        ordered.insert(0, preferred)
    return ordered


def lan_endpoints(config: CoreConfig) -> list[str]:
    return [f"https://{address}:{config.lan_port}" for address in local_ipv4_addresses()]


def _preferred_route_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("1.1.1.1", 9))
        return str(probe.getsockname()[0])
    except OSError:
        return ""
    finally:
        probe.close()


def _generate_identity(config: CoreConfig) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    common_name = config.server_name[:64] or "Cloud Storage Server"
    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Cloud Storage"),
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
        ]
    )
    names: list[x509.GeneralName] = [
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
        x509.IPAddress(ipaddress.ip_address("::1")),
    ]
    hostname = socket.gethostname().strip()
    if hostname and hostname.casefold() != "localhost":
        names.append(x509.DNSName(hostname))
    names.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in local_ipv4_addresses())
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_payload = certificate.public_bytes(serialization.Encoding.PEM)
    key_payload = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    _atomic_write(config.tls_private_key_path, key_payload, secret=True)
    try:
        _atomic_write(config.tls_certificate_path, certificate_payload, secret=False)
    except OSError:
        config.tls_private_key_path.unlink(missing_ok=True)
        raise


def _atomic_write(path, payload: bytes, *, secret: bool) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if secret and os.name != "nt":
            temporary.chmod(0o600)
        os.replace(temporary, path)
        if secret:
            _restrict_secret_file(path)
    finally:
        temporary.unlink(missing_ok=True)
