from __future__ import annotations

import asyncio
import json
import re
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path as FileSystemPath
from pathlib import PurePosixPath
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path,
    Query,
    Request,
    status,
)
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cloud_storage import __version__
from cloud_storage.core.automation import AutomationService
from cloud_storage.core.backup_automation import BackupAutomationService
from cloud_storage.core.config import CoreConfig, CoreSecrets
from cloud_storage.core.control_center import ControlCenterService
from cloud_storage.core.database import Database
from cloud_storage.core.diagnostics import DiagnosticsService
from cloud_storage.core.integrations import IntegrationRegistry
from cloud_storage.core.notifications import NotificationService
from cloud_storage.core.recovery import RecoveryService
from cloud_storage.core.repository import (
    ConflictError,
    CoreRepository,
    DeviceRecord,
    NotFoundError,
    PermissionDeniedError,
)
from cloud_storage.core.security import CredentialService, InvalidCredential
from cloud_storage.core.storage import (
    InvalidLogicalPath,
    InvalidStorageRoot,
    ManagedRootRequest,
    StorageCapacityError,
    StorageService,
)
from cloud_storage.core.support import SupportBundleService
from cloud_storage.core.tls import TlsIdentity, lan_endpoints, load_or_create_tls_identity
from cloud_storage.core.tunnels import TunnelProviderRegistry
from cloud_storage.core.web import BrowserAccess
from cloud_storage.pairing import build_dynamic_pairing_code, verify_dynamic_pairing_code


class SpaceCapabilitiesRequest(BaseModel):
    read: bool = True
    upload: bool = True
    modify: bool = False
    delete: bool = False
    share: bool = False


class ZrokEnableRequest(BaseModel):
    token: SecretStr


class SpaceGrantRequest(BaseModel):
    space_id: str = Field(min_length=1, max_length=100)
    capabilities: SpaceCapabilitiesRequest = Field(default_factory=SpaceCapabilitiesRequest)


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    quota_gib: int = Field(default=100, ge=1, le=1_000_000)
    role: str = Field(default="member", pattern="^(admin|member)$")
    password: SecretStr | None = None
    email: str = Field(default="", max_length=254)
    create_personal_space: bool = True
    primary_storage_root_id: str | None = Field(default=None, max_length=100)
    fallback_storage_root_id: str | None = Field(default=None, max_length=100)
    space_grants: list[SpaceGrantRequest] = Field(default_factory=list, max_length=128)
    prepare_access: bool = True


class UpdateUserRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    email: str = Field(default="", max_length=254)
    quota_gib: int = Field(default=100, ge=1, le=1_000_000)
    role: str = Field(default="member", pattern="^(admin|member)$")
    enabled: bool = True


class CreateSpaceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    quota_gib: int = Field(default=100, ge=1, le=1_000_000)
    primary_storage_root_id: str | None = Field(default=None, max_length=100)
    fallback_storage_root_id: str | None = Field(default=None, max_length=100)


class UpdateSpaceRequest(CreateSpaceRequest):
    enabled: bool = True


class SetUserPasswordRequest(BaseModel):
    password: SecretStr


class CreateInvitationRequest(BaseModel):
    user_id: str
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class RedeemInvitationRequest(BaseModel):
    code: str = Field(min_length=8, max_length=4096)
    password: SecretStr | None = None
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)


class DeviceLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: SecretStr
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)


class GoogleDeviceLoginRequest(BaseModel):
    id_token: SecretStr
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)


class RemoteSessionRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: SecretStr


class StorageRootRequest(BaseModel):
    disk_id: str = Field(min_length=8, max_length=100)
    path: str = Field(min_length=3, max_length=2048)
    priority: int = Field(default=50, ge=0, le=100)
    max_fill_percent: int = Field(default=90, ge=50, le=99)
    min_free_gib: int = Field(default=10, ge=0, le=1_000_000)
    write_enabled: bool = True
    purpose: str = Field(default="primary", pattern="^(primary|backup|mirror)$")


class CreateMigrationRequest(BaseModel):
    source_root_id: str = Field(min_length=1, max_length=100)
    target_root_id: str = Field(min_length=1, max_length=100)
    space_id: str | None = Field(default=None, max_length=100)


class CreateBackupRequest(BaseModel):
    target_root_id: str = Field(min_length=1, max_length=100)


class SetBackupPolicyRequest(BaseModel):
    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=8760)
    keep_last: int = Field(default=7, ge=1, le=365)
    verification_root_id: str | None = Field(default=None, max_length=100)


class CreateBackupVerificationRequest(BaseModel):
    target_root_id: str = Field(min_length=1, max_length=100)


class CreateDiagnosticScanRequest(BaseModel):
    kind: str = Field(default="quick", pattern="^(quick|full)$")


class DiagnosticRemediationRequest(BaseModel):
    action: str = Field(pattern="^(recheck|cleanup_expired_uploads|enter_read_only)$")
    confirmed: bool = False


class AutomationRuleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    trigger_type: str = Field(
        pattern=(
            "^(diagnostic_warning|diagnostic_critical|storage_low|backup_failed|"
            "restore_failed|mirror_degraded|maintenance_failed|pending_device|"
            "tunnel_offline|scheduled)$"
        )
    )
    action_type: str = Field(
        pattern=(
            "^(notify|quick_scan|full_scan|read_only|run_backup|reconcile_mirrors|"
            "restart_tunnel|sleep_after_hour)$"
        )
    )
    cooldown_minutes: int = Field(default=60, ge=1, le=10080)
    confirmed: bool = False


class AutomationSettingsRequest(BaseModel):
    enabled: bool = True
    interval_seconds: int = Field(default=60, ge=10, le=3600)


class CreateRestoreRequest(BaseModel):
    backup_job_id: str = Field(min_length=1, max_length=100)
    target_root_id: str = Field(min_length=1, max_length=100)


class SetServerModeRequest(BaseModel):
    mode: str = Field(pattern="^(normal|read_only)$")
    reason: str = Field(default="", max_length=500)
    confirmed: bool = False


class CreateResumableUploadRequest(BaseModel):
    logical_path: str = Field(min_length=1, max_length=1024)
    size_bytes: int = Field(ge=0)
    content_type: str = Field(default="application/octet-stream", max_length=127)
    sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")


class CreateDirectoryRequest(BaseModel):
    logical_path: str = Field(min_length=1, max_length=1024)


class MoveEntryRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=1024)
    destination_path: str = Field(min_length=1, max_length=1024)
    kind: str = Field(pattern="^(file|directory)$")


class CreateShareRequest(BaseModel):
    space_id: str = Field(min_length=1, max_length=100)
    logical_path: str = Field(min_length=1, max_length=1024)
    kind: str = Field(pattern="^(file|directory)$")
    ttl_hours: int = Field(default=24, ge=1, le=720)


class MobileAdminConfirmationRequest(BaseModel):
    password: SecretStr
    action: str = Field(min_length=3, max_length=100)


class MobileStorageWriteRequest(BaseModel):
    enabled: bool


class MobileAutomationRequest(BaseModel):
    enabled: bool


class MobileUserEnabledRequest(BaseModel):
    enabled: bool


class SyncStorageRootsRequest(BaseModel):
    roots: list[StorageRootRequest] = Field(max_length=128)


class TransferSettingsRequest(BaseModel):
    staging_enabled: bool = False
    staging_path: str = Field(default="", max_length=2048)


class ReportScheduleRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    interval_hours: int = Field(default=24, ge=1, le=8760)
    sections: list[str] = Field(min_length=1, max_length=10)
    delivery_channels: list[str] = Field(default_factory=list, max_length=8)


class GenerateReportRequest(BaseModel):
    sections: list[str] = Field(min_length=1, max_length=10)
    delivery_channels: list[str] = Field(default_factory=list, max_length=8)


class SandboxCellRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    image: str = Field(min_length=1, max_length=200)
    command: list[str] = Field(min_length=1, max_length=64)
    cpu_limit: float = Field(gt=0)
    memory_mib: int = Field(ge=64)
    storage_mib: int = Field(default=512, ge=64, le=10 * 1024)
    timeout_seconds: int = Field(default=300, ge=1, le=86400)
    network_enabled: bool = False


class DockerInstallRequest(BaseModel):
    confirmed: bool = False


class SshEnableRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    public_key: str = Field(min_length=32, max_length=16_384)
    port: int = Field(default=22, ge=1, le=65535)
    confirmed: bool = False


class SshDisableRequest(BaseModel):
    confirmed: bool = False


class WakeOnLanRequest(BaseModel):
    mac_address: str = Field(min_length=12, max_length=32)
    broadcast: str = Field(default="255.255.255.255", max_length=255)


class StorageCleanupRequest(BaseModel):
    confirmed: bool = False


class PrepareAccessRequest(BaseModel):
    email: str = Field(default="", max_length=254)
    ttl_seconds: int = Field(default=604800, ge=60, le=604800)
    send_email: bool = False


@dataclass(slots=True)
class CoreRuntime:
    config: CoreConfig
    secrets: CoreSecrets
    database: Database
    repository: CoreRepository
    storage: StorageService
    backup_automation: BackupAutomationService
    diagnostics: DiagnosticsService
    recovery: RecoveryService
    tls_identity: TlsIdentity | None
    tunnels: TunnelProviderRegistry
    integrations: IntegrationRegistry
    notifications: NotificationService
    automation: AutomationService
    support: SupportBundleService
    control: ControlCenterService
    started_monotonic: float


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_seconds: int) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and events[0] <= now - self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True


MOBILE_ADMIN_ACTIONS = {
    "device.approve",
    "device.revoke",
    "server.read_only",
    "server.normal",
    "diagnostics.quick",
    "diagnostics.full",
    "backup.run",
    "storage.write.pause",
    "storage.write.resume",
    "automation.pause",
    "automation.resume",
    "tunnel.restart",
    "core.restart",
    "user.create",
    "user.password",
    "user.enable",
    "user.disable",
}


def build_runtime(config: CoreConfig | None = None) -> CoreRuntime:
    config = config or CoreConfig.from_environment()
    config.ensure_directories()
    secrets_store = CoreSecrets.load_or_create(config)
    database = Database(config.database_path)
    database.initialize()
    credentials = CredentialService.from_secret(secrets_store.hmac_secret)
    repository = CoreRepository(database, credentials)
    repository.purge_audit(30)
    repository.record_audit(
        actor_type="system",
        actor_id=None,
        action="core.started",
        target_type="core",
        target_id=None,
        detail=f"Cloud Storage Core {__version__} запущен",
    )
    repository.initialize_default_storage(config.default_storage_root)
    storage = StorageService(config, database, repository)
    storage.cleanup_expired_uploads()
    storage.recover_interrupted_transfers()
    storage.recover_interrupted_maintenance()
    backup_automation = BackupAutomationService(database, repository, storage)
    backup_automation.recover_interrupted_verifications()
    diagnostics = DiagnosticsService(config, database, repository)
    diagnostics.recover_interrupted_scans()
    recovery = RecoveryService(database, repository, storage)
    recovery.recover_interrupted_jobs()
    tls_identity = (
        load_or_create_tls_identity(config) if config.lan_enabled or config.remote_enabled else None
    )
    tunnels = TunnelProviderRegistry.built_in(config)
    integrations = IntegrationRegistry.built_in(tunnels, config)
    notifications = NotificationService(database, repository, integrations)
    automation = AutomationService(
        database,
        repository,
        diagnostics,
        recovery,
        tunnels,
        notifications,
        storage,
        backup_automation,
    )
    support = SupportBundleService(
        config,
        repository,
        diagnostics,
        tunnels,
        automation,
        notifications,
        integrations,
    )
    control = ControlCenterService(
        config,
        database,
        repository,
        storage,
        diagnostics,
        tunnels,
        integrations,
        notifications,
        automation,
        tls_identity.fingerprint if tls_identity is not None else "",
    )
    return CoreRuntime(
        config=config,
        secrets=secrets_store,
        database=database,
        repository=repository,
        storage=storage,
        backup_automation=backup_automation,
        diagnostics=diagnostics,
        recovery=recovery,
        tls_identity=tls_identity,
        tunnels=tunnels,
        integrations=integrations,
        notifications=notifications,
        automation=automation,
        support=support,
        control=control,
        started_monotonic=time.monotonic(),
    )


def create_app(config: CoreConfig | None = None) -> FastAPI:
    runtime = build_runtime(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.diagnostics.start_monitor()
        runtime.backup_automation.start_scheduler()
        runtime.automation.start_scheduler()
        runtime.control.start()
        try:
            yield
        finally:
            runtime.control.stop()
            runtime.automation.stop_scheduler()
            runtime.backup_automation.stop_scheduler()
            runtime.diagnostics.stop_monitor()

    app = FastAPI(
        title="Cloud Storage Server Core",
        version=__version__,
        description="Local manager API and protected client API for trusted devices.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.shutdown_callback = None
    app.state.restart_callback = None

    dynamic_server_id = runtime.repository.credentials.fingerprint(
        runtime.config.server_name, "dynamic-server-id"
    )[:32]

    def current_dynamic_pairing(user_id: str = "") -> dict[str, Any]:
        if user_id and not runtime.repository.get_user(user_id).enabled:
            raise InvalidCredential("Пользователь отключён")
        now = int(time.time())
        expires_at = ((now // 300) + 1) * 300
        nonce = runtime.repository.credentials.fingerprint(
            f"{dynamic_server_id}:{expires_at}", "dynamic-pairing-nonce"
        )[:24]
        addresses: list[str] = []
        if runtime.config.lan_enabled:
            addresses.extend(lan_endpoints(runtime.config))
        zrok_status = runtime.tunnels.status("zrok")
        zrok_url = str(zrok_status.get("public_url") or "").rstrip("/")
        if (
            runtime.config.zrok_enabled
            and runtime.config.remote_pairing_enabled
            and zrok_status.get("state") == "online"
            and zrok_url.startswith("https://")
        ):
            addresses.append(zrok_url)
        remote_url = runtime.config.remote_public_url.rstrip("/")
        if (
            runtime.config.remote_enabled
            and runtime.config.remote_pairing_enabled
            and remote_url.startswith("https://")
        ):
            addresses.append(remote_url)
        addresses = list(dict.fromkeys(item for item in addresses if item))
        if not addresses:
            raise HTTPException(
                status_code=503,
                detail="Включите локальный HTTPS или защищённый интернет-вход для pairing",
            )
        fingerprint = (
            runtime.tls_identity.fingerprint
            if runtime.tls_identity and addresses[0] != zrok_url
            else ""
        )
        code = build_dynamic_pairing_code(
            server_id=dynamic_server_id,
            server_url=addresses[0],
            certificate_fingerprint=fingerprint,
            expires_at=expires_at,
            nonce=nonce,
            signing_key=runtime.repository.credentials.hmac_secret,
            alternate_addresses=addresses[1:],
            user_id=user_id,
        )
        return {
            "code": code,
            "server_id": dynamic_server_id,
            "expires_at": expires_at,
            "seconds_remaining": max(0, expires_at - now),
            "user_id": user_id,
        }
    browser = BrowserAccess(runtime)
    bearer = HTTPBearer(auto_error=False)
    pairing_limiter = SlidingWindowLimiter(limit=10, window_seconds=300)
    remote_pairing_limiter = SlidingWindowLimiter(limit=5, window_seconds=900)
    remote_login_limiter = SlidingWindowLimiter(limit=5, window_seconds=300)
    remote_request_limiter = SlidingWindowLimiter(limit=600, window_seconds=60)
    remote_audit_limiter = SlidingWindowLimiter(limit=30, window_seconds=3600)
    mobile_confirmation_limiter = SlidingWindowLimiter(limit=10, window_seconds=300)
    consumed_mobile_confirmations: dict[str, int] = {}
    consumed_mobile_confirmations_lock = threading.Lock()
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=(
            ["*"]
            if runtime.config.lan_enabled
            or runtime.config.remote_enabled
            or runtime.config.zrok_enabled
            else ["127.0.0.1", "localhost", "[::1]", "testserver"]
        ),
    )

    def is_listener_request(request: Request, port: int, enabled: bool) -> bool:
        server = request.scope.get("server")
        return bool(enabled and server and len(server) > 1 and server[1] == port)

    def remote_client_path_allowed(path: str) -> bool:
        if path in {
            "/",
            "/v1/health",
            "/v1/web/pair",
            "/v1/web/session",
            "/v1/web/logout",
            "/web/assets/app.css",
            "/web/assets/app.js",
            "/v1/auth/device-login",
            "/v1/auth/google-device-login",
            "/v1/pairing/redeem",
            "/v1/pairing/status",
            "/v1/remote/session",
            "/v1/spaces",
            "/v1/operations",
            "/v1/shares",
            "/v1/mobile/admin/overview",
            "/v1/mobile/admin/confirm",
            "/v1/mobile/admin/server-mode",
            "/v1/mobile/admin/diagnostics/scans",
            "/v1/mobile/admin/automation/settings",
            "/v1/mobile/admin/core/restart",
            "/v1/mobile/admin/users",
        }:
            return True
        patterns = (
            r"/v1/spaces/[^/]+/entries",
            r"/v1/spaces/[^/]+/search",
            r"/v1/spaces/[^/]+/directories",
            r"/v1/spaces/[^/]+/directories/.+",
            r"/v1/spaces/[^/]+/moves",
            r"/v1/spaces/[^/]+/uploads",
            r"/v1/spaces/[^/]+/files/.+",
            r"/v1/uploads/[^/]+",
            r"/v1/uploads/[^/]+/complete",
            r"/v1/shares/[^/]+",
            r"/v1/public/shares/[^/]+",
            r"/v1/public/shares/[^/]+/download",
            r"/v1/public/shares/[^/]+/files/.+",
            r"/v1/mobile/admin/devices/[^/]+/(approve|revoke)",
            r"/v1/mobile/admin/backups/[^/]+/run",
            r"/v1/mobile/admin/storage/[^/]+/write",
            r"/v1/mobile/admin/tunnels/[^/]+/restart",
            r"/v1/mobile/admin/users/[^/]+/(password|enabled)",
        )
        return any(re.fullmatch(pattern, path) for pattern in patterns)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        lan_request = is_listener_request(
            request, runtime.config.lan_port, runtime.config.lan_enabled
        )
        remote_request = is_listener_request(
            request, runtime.config.remote_port, runtime.config.remote_enabled
        )
        zrok_request = is_listener_request(
            request, runtime.config.zrok_port, runtime.config.zrok_enabled
        )
        external_request = remote_request or zrok_request
        request.state.remote_request = remote_request
        request.state.lan_request = lan_request
        request.state.zrok_request = zrok_request
        request.state.external_request = external_request
        remote_address = request.client.host if request.client else "unknown"
        local_host = (request.url.hostname or "").casefold()
        invalid_local_host = (
            not lan_request
            and not external_request
            and local_host
            not in {
                "127.0.0.1",
                "::1",
                "localhost",
                "testserver",
            }
        )
        restricted_path = request.url.path.startswith("/v1/admin") or request.url.path in {
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        browser_policy = runtime.control.settings()["browser_access"] if zrok_request else "all"
        zrok_policy_denied = zrok_request and (
            (browser_policy == "nobody" and (
                request.url.path == "/" or request.url.path.startswith(("/web/", "/v1/web/", "/v1/public/shares/"))
            ))
            or (browser_policy == "approved" and request.url.path.startswith("/v1/public/shares/"))
        )
        if invalid_local_host:
            response = JSONResponse(status_code=400, content={"detail": "invalid host header"})
        elif zrok_policy_denied:
            response = JSONResponse(
                status_code=403,
                content={"detail": "browser access is blocked by administrator policy"},
            )
        elif (lan_request and restricted_path) or (
            external_request and not remote_client_path_allowed(request.url.path)
        ):
            response = JSONResponse(status_code=404, content={"detail": "not found"})
        elif external_request and not remote_request_limiter.allow(remote_address):
            response = JSONResponse(
                status_code=429,
                content={"detail": "too many remote requests"},
            )
        elif (
            external_request
            and request.url.path in {"/v1/pairing/redeem", "/v1/web/pair"}
            and not runtime.config.remote_pairing_enabled
        ):
            response = JSONResponse(
                status_code=403,
                content={"detail": "remote pairing is disabled by the administrator"},
            )
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            runtime.recovery.server_mode()["mode"] == "read_only"
            and not request.url.path.startswith(
                (
                    "/v1/admin/server-mode",
                    "/v1/admin/diagnostics",
                    "/v1/admin/restores",
                    "/v1/admin/backup-verifications",
                    "/v1/admin/automation",
                    "/v1/admin/notifications",
                    "/v1/admin/integrations",
                    "/v1/admin/control-center",
                    "/v1/mobile/admin/server-mode",
                    "/v1/mobile/admin/diagnostics/scans",
                    "/v1/mobile/admin/automation/settings",
                    "/v1/mobile/admin/core/restart",
                )
            )
            and not re.fullmatch(r"/v1/mobile/admin/storage/[^/]+/write", request.url.path)
            and request.url.path not in {"/v1/admin/shutdown", "/v1/web/logout"}
            and not request.url.path.endswith("/revoke")
        ):
            response = JSONResponse(
                status_code=503,
                content={"detail": "server is in emergency read-only mode"},
            )
        else:
            if request.url.path.startswith(("/v1/spaces", "/v1/uploads", "/v1/shares")):
                runtime.control.note_access()
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
        if request.url.path == "/" or request.url.path.startswith("/web/assets/"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' blob:; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
        response.headers["Referrer-Policy"] = "no-referrer"
        if external_request:
            important = request.method != "GET" or response.status_code >= 400
            if important or remote_audit_limiter.allow(remote_address):
                try:
                    runtime.repository.record_audit(
                        actor_type="zrok_client" if zrok_request else "remote_client",
                        actor_id=None,
                        action=(
                            "remote.access.allowed"
                            if response.status_code < 400
                            else "remote.access.denied"
                        ),
                        target_type="api_route",
                        target_id=request.url.path[:1024],
                        detail=(
                            f"{'zrok' if zrok_request else 'direct'} "
                            f"{request.method} {request.url.path} -> {response.status_code}"
                        ),
                        remote_address=remote_address,
                    )
                except sqlite3.Error:
                    pass
        return response

    def require_manager(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> None:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="manager authorization required")
        if not secrets.compare_digest(
            credentials.credentials.encode(), runtime.secrets.manager_token.encode()
        ):
            raise HTTPException(status_code=403, detail="invalid manager credential")

    def require_device(
        request: Request,
        remote_session: Annotated[str | None, Header(alias="X-Cloud-Remote-Session")] = None,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> DeviceRecord:
        if credentials is None:
            return browser.authenticate(request)
        if credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="device authorization required")
        try:
            device = runtime.repository.authenticate_device(credentials.credentials)
            if request.state.external_request and device.pairing_method != "dynamic":
                runtime.repository.credentials.verify_remote_session(
                    remote_session or "",
                    user_id=device.user_id,
                    device_id=device.id,
                    password_version=runtime.repository.user_password_version(device.user_id),
                )
            return device
        except (InvalidCredential, PermissionDeniedError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def require_pairing_device(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> DeviceRecord:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="device authorization required")
        try:
            return runtime.repository.authenticate_device(
                credentials.credentials, allow_pending=True
            )
        except (InvalidCredential, PermissionDeniedError) as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    def require_mobile_admin(
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ) -> DeviceRecord:
        user = runtime.repository.get_user(device.user_id)
        if user.role != "admin" or not user.enabled:
            raise HTTPException(status_code=403, detail="administrator role is required")
        return device

    def consume_mobile_confirmation(
        token: str,
        *,
        device: DeviceRecord,
        action: str,
    ) -> None:
        if action not in MOBILE_ADMIN_ACTIONS:
            raise HTTPException(status_code=400, detail="unsupported administrator action")
        try:
            runtime.repository.credentials.verify_mobile_confirmation(
                token,
                user_id=device.user_id,
                device_id=device.id,
                action=action,
                password_version=runtime.repository.user_password_version(device.user_id),
            )
        except InvalidCredential as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        fingerprint = runtime.repository.credentials.fingerprint(
            token, "consumed-mobile-confirmation"
        )
        now = int(time.time())
        with consumed_mobile_confirmations_lock:
            expired = [
                key
                for key, consumed_at in consumed_mobile_confirmations.items()
                if consumed_at <= now - 300
            ]
            for key in expired:
                consumed_mobile_confirmations.pop(key, None)
            if fingerprint in consumed_mobile_confirmations:
                raise HTTPException(
                    status_code=409,
                    detail="administrator confirmation has already been used",
                )
            consumed_mobile_confirmations[fingerprint] = now

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        if str(exc) == "username already exists":
            return JSONResponse(
                status_code=409,
                content={"code": "username_exists", "detail": "Этот логин уже используется"},
            )
        return JSONResponse(status_code=409, content={"code": "conflict", "detail": str(exc)})

    @app.exception_handler(InvalidCredential)
    @app.exception_handler(InvalidLogicalPath)
    @app.exception_handler(InvalidStorageRoot)
    @app.exception_handler(ValueError)
    async def validation_handler(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(PermissionDeniedError)
    async def permission_handler(request: Request, exc: PermissionDeniedError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(StorageCapacityError)
    async def capacity_handler(request: Request, exc: StorageCapacityError) -> JSONResponse:
        return JSONResponse(status_code=507, content={"detail": str(exc)})

    @app.get("/v1/health", tags=["system"])
    def health() -> dict[str, Any]:
        try:
            with runtime.database.connection() as connection:
                connection.execute("SELECT 1").fetchone()
            database_status = "ok"
        except sqlite3.Error:
            database_status = "error"
        lan = None
        if runtime.tls_identity is not None:
            lan = {
                "enabled": True,
                "port": runtime.config.lan_port,
                "discovery_port": runtime.config.discovery_port,
                "fingerprint": runtime.tls_identity.fingerprint,
                "display_fingerprint": runtime.tls_identity.display_fingerprint,
                "endpoints": lan_endpoints(runtime.config),
            }
        remote = None
        if runtime.config.remote_enabled and runtime.tls_identity is not None:
            remote = {
                "enabled": True,
                "port": runtime.config.remote_port,
                "public_url": runtime.config.remote_public_url.rstrip("/"),
                "pairing_enabled": runtime.config.remote_pairing_enabled,
                "fingerprint": runtime.tls_identity.fingerprint,
                "display_fingerprint": runtime.tls_identity.display_fingerprint,
                "manager_api_exposed": False,
                "automatic_router_changes": False,
                "login_required": True,
            }
        zrok_status = runtime.tunnels.status("zrok")
        zrok = {
            "enabled": zrok_status["enabled"],
            "provider": "zrok",
            "probe_id": runtime.tunnels.providers["zrok"].health_probe_id,
            "state": zrok_status["state"],
            "installed": zrok_status["installed"],
            "process_running": zrok_status["process_running"],
            "listener_port": runtime.config.zrok_port,
            "public_url": zrok_status["public_url"],
            "pairing_enabled": runtime.config.remote_pairing_enabled,
            "share_type": zrok_status["share_type"],
            "login_required": True,
            "manager_api_exposed": False,
            "automatic_router_changes": False,
        }
        google_client_id = str(
            runtime.control.settings().get("integrations", {}).get("email", {}).get(
                "gmail_client_id", ""
            )
        )
        return {
            "status": "ok" if database_status == "ok" else "degraded",
            "version": __version__,
            "api_version": "v1",
            "server_name": runtime.config.server_name,
            "server_mode": runtime.recovery.server_mode()["mode"],
            "database": database_status,
            "uptime_seconds": round(time.monotonic() - runtime.started_monotonic),
            "bind": f"{runtime.config.host}:{runtime.config.port}",
            "lan": lan,
            "remote": remote,
            "zrok": zrok,
            "google_oauth_client_id": google_client_id,
        }

    @app.get("/v1/admin/summary", tags=["manager"], dependencies=[Depends(require_manager)])
    def admin_summary() -> dict[str, Any]:
        return runtime.repository.summary()

    @app.get("/v1/admin/tunnels", tags=["manager"], dependencies=[Depends(require_manager)])
    def tunnel_status() -> dict[str, Any]:
        return runtime.tunnels.overview()

    @app.post(
        "/v1/admin/tunnels/{provider_id}/restart",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def restart_tunnel(provider_id: str) -> dict[str, Any]:
        try:
            result = runtime.tunnels.restart(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="tunnel provider not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="tunnel.provider.restarted",
            target_type="tunnel_provider",
            target_id=provider_id,
            detail=f"Перезапущен встроенный tunnel-provider {provider_id}",
        )
        return result

    @app.post(
        "/v1/admin/tunnels/{provider_id}/install",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_tunnel(provider_id: str) -> dict[str, Any]:
        try:
            result = runtime.tunnels.install(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="tunnel provider not found") from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="tunnel.provider.install.started",
            target_type="tunnel_provider",
            target_id=provider_id,
            detail=f"Запущена проверяемая установка tunnel-provider {provider_id}",
        )
        return result

    @app.post(
        "/v1/admin/tunnels/{provider_id}/enable",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def enable_tunnel(provider_id: str, body: ZrokEnableRequest) -> dict[str, Any]:
        try:
            result = runtime.tunnels.enable(provider_id, body.token.get_secret_value())
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="tunnel provider not found") from exc
        except (RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        runtime.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="tunnel.provider.account.enabled",
            target_type="tunnel_provider",
            target_id=provider_id,
            detail=f"Аккаунт tunnel-provider {provider_id} подключён; профиль хранится в каталоге Core",
        )
        return result

    @app.get(
        "/v1/admin/integrations",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def integrations_overview() -> dict[str, Any]:
        return runtime.integrations.overview()

    @app.post(
        "/v1/admin/integrations/{provider_id}/test",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def test_integration(provider_id: str) -> dict[str, Any]:
        return runtime.notifications.test_provider(provider_id)

    @app.get(
        "/v1/admin/control-center",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def control_center_overview() -> dict[str, Any]:
        return runtime.control.overview()

    @app.put(
        "/v1/admin/control-center/settings",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_control_center_settings(body: dict[str, Any]) -> dict[str, Any]:
        update = dict(body)
        confirmed = update.pop("confirmed", False) is True
        power = update.get("power")
        current_power = runtime.control.settings().get("power", {})
        enabling_system_sleep = (
            isinstance(power, dict)
            and power.get("allow_os_sleep") is True
            and current_power.get("allow_os_sleep") is not True
        )
        enabling_system_shutdown = (
            isinstance(power, dict)
            and power.get("allow_os_shutdown") is True
            and current_power.get("allow_os_shutdown") is not True
        )
        if (enabling_system_sleep or enabling_system_shutdown) and not confirmed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="explicit confirmation is required to enable operating-system power actions",
            )
        return runtime.control.update_settings(update)

    @app.post(
        "/v1/admin/control-center/presets/{preset_id}/apply",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def apply_system_preset(preset_id: str) -> dict[str, Any]:
        return runtime.control.apply_preset(preset_id)

    @app.post(
        "/v1/admin/control-center/automations/{template_id}/install",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def install_automation_template(template_id: str) -> dict[str, Any]:
        return runtime.control.install_template(template_id)

    @app.get(
        "/v1/admin/control-center/reports",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_report_schedules() -> list[dict[str, Any]]:
        return runtime.control.list_reports()

    @app.post(
        "/v1/admin/control-center/reports",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_report_schedule(body: ReportScheduleRequest) -> dict[str, Any]:
        return runtime.control.save_report(**body.model_dump())

    @app.put(
        "/v1/admin/control-center/reports/{report_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_report_schedule(report_id: str, body: ReportScheduleRequest) -> dict[str, Any]:
        return runtime.control.save_report(report_id=report_id, **body.model_dump())

    @app.post(
        "/v1/admin/control-center/reports/run",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def run_report(body: GenerateReportRequest) -> dict[str, Any]:
        return runtime.control.generate_report(
            body.sections, delivery_channels=body.delivery_channels
        )

    @app.get(
        "/v1/admin/control-center/cells",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_sandbox_cells() -> list[dict[str, Any]]:
        return runtime.control.list_cells()

    @app.post(
        "/v1/admin/control-center/cells",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_sandbox_cell(body: SandboxCellRequest) -> dict[str, Any]:
        return runtime.control.create_cell(**body.model_dump())

    @app.put(
        "/v1/admin/control-center/cells/{cell_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_sandbox_cell(cell_id: str, body: SandboxCellRequest) -> dict[str, Any]:
        return runtime.control.update_cell(cell_id, **body.model_dump())

    @app.delete(
        "/v1/admin/control-center/cells/{cell_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_sandbox_cell(cell_id: str) -> Response:
        try:
            runtime.control.delete_cell(cell_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/admin/control-center/cells/{cell_id}/run",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def run_sandbox_cell(cell_id: str) -> dict[str, Any]:
        try:
            return runtime.control.run_cell(cell_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post(
        "/v1/admin/control-center/host-tools/docker/install",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def install_docker_desktop(body: DockerInstallRequest) -> dict[str, Any]:
        return runtime.control.install_docker(confirmed=body.confirmed)

    @app.post(
        "/v1/admin/control-center/host-tools/ssh/enable",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def enable_windows_ssh(body: SshEnableRequest) -> dict[str, Any]:
        return runtime.control.enable_ssh(**body.model_dump())

    @app.post(
        "/v1/admin/control-center/host-tools/ssh/disable",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def disable_windows_ssh(body: SshDisableRequest) -> dict[str, Any]:
        return runtime.control.disable_ssh(confirmed=body.confirmed)

    @app.post(
        "/v1/admin/control-center/wake-on-lan",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def send_wake_on_lan(body: WakeOnLanRequest) -> dict[str, Any]:
        return runtime.control.wake_on_lan(body.mac_address, broadcast=body.broadcast)

    @app.post(
        "/v1/admin/storage-roots/{root_id}/cleanup",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cleanup_storage_root(root_id: str, body: StorageCleanupRequest) -> dict[str, int]:
        if not body.confirmed:
            raise HTTPException(status_code=409, detail="cleanup requires confirmation")
        return runtime.storage.cleanup_root_temporary(root_id)

    @app.get(
        "/v1/admin/notifications",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_notifications(
        include_acknowledged: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[dict[str, Any]]:
        return runtime.notifications.list(
            include_acknowledged=include_acknowledged,
            limit=limit,
        )

    @app.post(
        "/v1/admin/notifications/{notification_id}/acknowledge",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def acknowledge_notification(notification_id: str) -> dict[str, Any]:
        return runtime.notifications.acknowledge(notification_id)

    @app.get(
        "/v1/admin/automation",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def automation_overview() -> dict[str, Any]:
        return runtime.automation.overview()

    @app.put(
        "/v1/admin/automation/settings",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_automation_settings(body: AutomationSettingsRequest) -> dict[str, Any]:
        return runtime.automation.set_settings(
            enabled=body.enabled,
            interval_seconds=body.interval_seconds,
        )

    @app.get(
        "/v1/admin/automation/preview",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def preview_automation() -> dict[str, Any]:
        return runtime.automation.preview()

    @app.get(
        "/v1/admin/automation/rules/{rule_id}/preview",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def preview_automation_rule(rule_id: str) -> dict[str, Any]:
        return runtime.automation.preview(rule_id)

    @app.post(
        "/v1/admin/automation/rules",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_automation_rule(body: AutomationRuleRequest) -> dict[str, Any]:
        if body.action_type == "read_only" and not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="explicit confirmation is required for automatic read-only mode",
            )
        try:
            rule = runtime.automation.create_rule(
                name=body.name,
                enabled=body.enabled,
                trigger_type=body.trigger_type,
                action_type=body.action_type,
                cooldown_seconds=body.cooldown_minutes * 60,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return runtime.automation.rule_to_dict(rule)

    @app.put(
        "/v1/admin/automation/rules/{rule_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_automation_rule(
        rule_id: str,
        body: AutomationRuleRequest,
    ) -> dict[str, Any]:
        if body.action_type == "read_only" and body.enabled and not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="explicit confirmation is required for automatic read-only mode",
            )
        try:
            rule = runtime.automation.update_rule(
                rule_id,
                name=body.name,
                enabled=body.enabled,
                trigger_type=body.trigger_type,
                action_type=body.action_type,
                cooldown_seconds=body.cooldown_minutes * 60,
            )
        except ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return runtime.automation.rule_to_dict(rule)

    @app.delete(
        "/v1/admin/automation/rules/{rule_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def delete_automation_rule(rule_id: str) -> Response:
        runtime.automation.delete_rule(rule_id)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(
        "/v1/admin/automation/evaluate",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def evaluate_automation() -> dict[str, Any]:
        runs = runtime.automation.evaluate(trigger_source="manager")
        return {"matched_rules": len(runs), "runs": runs}

    @app.get(
        "/v1/admin/support-bundle",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def support_bundle() -> Response:
        bundle = runtime.support.build()
        runtime.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action="support.bundle.created",
            target_type="support_bundle",
            target_id=None,
            detail="Сформирован обезличенный диагностический пакет без базы и секретов",
        )
        return Response(
            content=bundle.content,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{bundle.filename}"',
                "X-Support-Bundle-SHA256": bundle.sha256,
            },
        )

    @app.get(
        "/v1/admin/server-mode",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def server_mode() -> dict[str, Any]:
        return runtime.recovery.server_mode()

    @app.put(
        "/v1/admin/server-mode",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def set_server_mode(body: SetServerModeRequest) -> dict[str, Any]:
        if body.mode == "read_only" and not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="explicit confirmation is required for emergency read-only mode",
            )
        return runtime.recovery.set_server_mode(body.mode, body.reason)

    @app.get(
        "/v1/admin/diagnostics",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def diagnostics_overview() -> dict[str, Any]:
        return runtime.diagnostics.overview()

    @app.get(
        "/v1/admin/diagnostics/scans",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_diagnostic_scans() -> list[dict[str, Any]]:
        return [runtime.diagnostics.scan_to_dict(item) for item in runtime.diagnostics.list_scans()]

    @app.post(
        "/v1/admin/diagnostics/scans",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_diagnostic_scan(
        body: CreateDiagnosticScanRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        scan = runtime.diagnostics.create_scan(body.kind, source="manager")
        background_tasks.add_task(runtime.diagnostics.run_scan_safely, scan.id)
        return runtime.diagnostics.scan_to_dict(scan)

    @app.get(
        "/v1/admin/diagnostics/scans/{scan_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def diagnostic_scan_status(scan_id: str) -> dict[str, Any]:
        return runtime.diagnostics.scan_to_dict(runtime.diagnostics.get_scan(scan_id))

    @app.get(
        "/v1/admin/diagnostics/incidents",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_diagnostic_incidents(
        include_resolved: bool = Query(default=False),
    ) -> list[dict[str, Any]]:
        return [
            runtime.diagnostics.incident_to_dict(item)
            for item in runtime.diagnostics.list_incidents(include_resolved=include_resolved)
        ]

    @app.post(
        "/v1/admin/diagnostics/incidents/{incident_id}/remediate",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def remediate_diagnostic_incident(
        incident_id: str,
        body: DiagnosticRemediationRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        incident = runtime.diagnostics.get_incident(incident_id)
        if body.action == "recheck":
            scan = runtime.diagnostics.create_scan("quick", source="manager")
            background_tasks.add_task(runtime.diagnostics.run_scan_safely, scan.id)
            result: dict[str, Any] = {"action": body.action, "scan_id": scan.id}
        elif body.action == "cleanup_expired_uploads":
            if incident.check_key != "uploads.expired":
                raise HTTPException(
                    status_code=409,
                    detail="cleanup action does not apply to this incident",
                )
            removed = runtime.storage.cleanup_expired_uploads()
            result = {"action": body.action, "removed_uploads": removed}
        else:
            if incident.severity != "critical":
                raise HTTPException(
                    status_code=409,
                    detail="read-only remediation requires a critical incident",
                )
            if not body.confirmed:
                raise HTTPException(
                    status_code=400,
                    detail="explicit confirmation is required for read-only remediation",
                )
            state = runtime.recovery.set_server_mode(
                "read_only",
                f"Диагностический инцидент: {incident.summary}",
            )
            result = {"action": body.action, "server_mode": state}
        runtime.repository.record_audit(
            actor_type="manager",
            actor_id=None,
            action=f"diagnostics.incident.{body.action}",
            target_type="diagnostic_incident",
            target_id=incident_id,
            detail=f"Выполнено действие {body.action} для «{incident.summary}»",
        )
        return result

    @app.put(
        "/v1/admin/storage-roots",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def sync_storage_roots(body: SyncStorageRootsRequest) -> list[dict[str, Any]]:
        roots = runtime.storage.sync_managed_roots(
            [
                ManagedRootRequest(
                    disk_id=item.disk_id,
                    path=FileSystemPath(item.path),
                    priority=item.priority,
                    max_fill_percent=item.max_fill_percent,
                    min_free_bytes=item.min_free_gib * 1024**3,
                    write_enabled=item.write_enabled,
                    purpose=item.purpose,
                )
                for item in body.roots
            ]
        )
        return [
            {
                "id": item.id,
                "disk_id": item.disk_id,
                "path": str(item.path),
                "write_enabled": item.write_enabled,
                "purpose": item.purpose,
                "priority": item.priority,
                "max_fill_percent": item.max_fill_percent,
                "min_free_bytes": item.min_free_bytes,
            }
            for item in roots
        ]

    @app.get(
        "/v1/admin/storage-roots",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_storage_roots() -> list[dict[str, Any]]:
        return [
            {
                "id": item.id,
                "disk_id": item.disk_id,
                "path": str(item.path),
                "write_enabled": item.write_enabled,
                "purpose": item.purpose,
                "priority": item.priority,
                "max_fill_percent": item.max_fill_percent,
                "min_free_bytes": item.min_free_bytes,
            }
            for item in runtime.storage.list_roots()
        ]

    @app.get(
        "/v1/admin/transfers",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def transfer_overview(
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        return runtime.storage.transfer_overview(limit=limit)

    @app.put(
        "/v1/admin/transfers/settings",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_transfer_settings(body: TransferSettingsRequest) -> dict[str, Any]:
        return runtime.storage.set_transfer_settings(
            staging_enabled=body.staging_enabled,
            staging_path=body.staging_path,
        )

    @app.post(
        "/v1/admin/transfers/{transfer_id}/retry",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def retry_transfer(
        transfer_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        queued = runtime.storage.queue_transfer_retry(transfer_id)
        background_tasks.add_task(runtime.storage.retry_transfer, transfer_id)
        return queued

    @app.get(
        "/v1/admin/maintenance/jobs",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_maintenance_jobs() -> list[dict[str, Any]]:
        return [
            runtime.storage.migration_to_dict(item)
            for item in runtime.storage.list_migration_jobs()
        ]

    @app.post(
        "/v1/admin/maintenance/migrations",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_migration(
        body: CreateMigrationRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.create_migration_job(
            body.source_root_id,
            body.target_root_id,
            body.space_id,
        )
        background_tasks.add_task(runtime.storage.run_migration_job, job.id)
        return runtime.storage.migration_to_dict(job)

    @app.get(
        "/v1/admin/maintenance/jobs/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def maintenance_job(job_id: str) -> dict[str, Any]:
        return runtime.storage.migration_to_dict(runtime.storage.get_migration_job(job_id))

    @app.post(
        "/v1/admin/maintenance/jobs/{job_id}/resume",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def resume_maintenance_job(
        job_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.resume_migration_job(job_id)
        background_tasks.add_task(runtime.storage.run_migration_job, job.id)
        return runtime.storage.migration_to_dict(job)

    @app.delete(
        "/v1/admin/maintenance/jobs/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_maintenance_job(job_id: str) -> dict[str, Any]:
        return runtime.storage.migration_to_dict(runtime.storage.cancel_migration_job(job_id))

    @app.get(
        "/v1/admin/backups",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_backups() -> list[dict[str, Any]]:
        return [runtime.storage.backup_to_dict(item) for item in runtime.storage.list_backup_jobs()]

    @app.get(
        "/v1/admin/backup-automation",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def backup_automation() -> dict[str, Any]:
        return {
            "policies": [
                runtime.backup_automation.policy_to_dict(item)
                for item in runtime.backup_automation.list_policies()
            ],
            "verifications": [
                runtime.backup_automation.verification_to_dict(item)
                for item in runtime.backup_automation.list_verifications()
            ],
        }

    @app.put(
        "/v1/admin/backup-policies/{target_root_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def set_backup_policy(
        target_root_id: str,
        body: SetBackupPolicyRequest,
    ) -> dict[str, Any]:
        policy = runtime.backup_automation.set_policy(
            target_root_id,
            enabled=body.enabled,
            interval_hours=body.interval_hours,
            keep_last=body.keep_last,
            verification_root_id=body.verification_root_id,
        )
        return runtime.backup_automation.policy_to_dict(policy)

    @app.post(
        "/v1/admin/backup-policies/{target_root_id}/run",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def run_backup_policy(
        target_root_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.backup_automation.queue_policy_run(target_root_id)
        background_tasks.add_task(runtime.backup_automation.run_backup_pipeline, job.id)
        return runtime.storage.backup_to_dict(job)

    @app.post(
        "/v1/admin/backups",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_backup(
        body: CreateBackupRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.create_backup_job(body.target_root_id)
        background_tasks.add_task(runtime.backup_automation.run_backup_pipeline, job.id)
        return runtime.storage.backup_to_dict(job)

    @app.get(
        "/v1/admin/backups/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def backup_status(job_id: str) -> dict[str, Any]:
        return runtime.storage.backup_to_dict(runtime.storage.get_backup_job(job_id))

    @app.post(
        "/v1/admin/backups/{job_id}/resume",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def resume_backup(
        job_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.resume_backup_job(job_id)
        background_tasks.add_task(runtime.backup_automation.run_backup_pipeline, job.id)
        return runtime.storage.backup_to_dict(job)

    @app.delete(
        "/v1/admin/backups/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_backup(job_id: str) -> dict[str, Any]:
        return runtime.storage.backup_to_dict(runtime.storage.cancel_backup_job(job_id))

    @app.post(
        "/v1/admin/backups/{job_id}/verify",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def verify_backup(
        job_id: str,
        body: CreateBackupVerificationRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        verification = runtime.backup_automation.create_verification(job_id, body.target_root_id)
        background_tasks.add_task(runtime.backup_automation.run_verification, verification.id)
        return runtime.backup_automation.verification_to_dict(verification)

    @app.get(
        "/v1/admin/backup-verifications/{verification_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def backup_verification(verification_id: str) -> dict[str, Any]:
        return runtime.backup_automation.verification_to_dict(
            runtime.backup_automation.get_verification(verification_id)
        )

    @app.delete(
        "/v1/admin/backup-verifications/{verification_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_backup_verification(verification_id: str) -> dict[str, Any]:
        return runtime.backup_automation.verification_to_dict(
            runtime.backup_automation.cancel_verification(verification_id)
        )

    @app.get(
        "/v1/admin/restores",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_restores() -> list[dict[str, Any]]:
        return [
            runtime.recovery.restore_job_to_dict(item)
            for item in runtime.recovery.list_restore_jobs()
        ]

    @app.post(
        "/v1/admin/restores",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def create_restore(
        body: CreateRestoreRequest,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.recovery.create_restore_job(
            body.backup_job_id,
            body.target_root_id,
        )
        background_tasks.add_task(runtime.recovery.run_restore_job, job.id)
        return runtime.recovery.restore_job_to_dict(job)

    @app.get(
        "/v1/admin/restores/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def restore_status(job_id: str) -> dict[str, Any]:
        return runtime.recovery.restore_job_to_dict(runtime.recovery.get_restore_job(job_id))

    @app.post(
        "/v1/admin/restores/{job_id}/resume",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def resume_restore(
        job_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.recovery.resume_restore_job(job_id)
        background_tasks.add_task(runtime.recovery.run_restore_job, job.id)
        return runtime.recovery.restore_job_to_dict(job)

    @app.delete(
        "/v1/admin/restores/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_restore(job_id: str) -> dict[str, Any]:
        return runtime.recovery.restore_job_to_dict(runtime.recovery.cancel_restore_job(job_id))

    @app.get(
        "/v1/admin/mirrors",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def mirror_status() -> dict[str, Any]:
        return {
            "roots": runtime.storage.mirror_overview(),
            "jobs": [
                runtime.storage.mirror_job_to_dict(item)
                for item in runtime.storage.list_mirror_jobs()
            ],
        }

    @app.post(
        "/v1/admin/mirrors/{root_id}/reconcile",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def reconcile_mirror(
        root_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.create_mirror_job(root_id)
        background_tasks.add_task(runtime.storage.run_mirror_job, job.id)
        return runtime.storage.mirror_job_to_dict(job)

    @app.get(
        "/v1/admin/mirrors/jobs/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def mirror_job_status(job_id: str) -> dict[str, Any]:
        return runtime.storage.mirror_job_to_dict(runtime.storage.get_mirror_job(job_id))

    @app.post(
        "/v1/admin/mirrors/jobs/{job_id}/resume",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def resume_mirror_job(
        job_id: str,
        background_tasks: BackgroundTasks,
    ) -> dict[str, Any]:
        job = runtime.storage.resume_mirror_job(job_id)
        background_tasks.add_task(runtime.storage.run_mirror_job, job.id)
        return runtime.storage.mirror_job_to_dict(job)

    @app.delete(
        "/v1/admin/mirrors/jobs/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_mirror_job(job_id: str) -> dict[str, Any]:
        return runtime.storage.mirror_job_to_dict(runtime.storage.cancel_mirror_job(job_id))

    @app.get("/v1/admin/users", tags=["manager"], dependencies=[Depends(require_manager)])
    def list_users() -> list[dict[str, Any]]:
        runtime_overview = runtime.repository.user_runtime_overview()
        spaces = runtime.repository.list_spaces_admin()
        grants: dict[str, list[dict[str, Any]]] = {}
        for space in spaces:
            for member in space["members"]:
                grants.setdefault(str(member["user_id"]), []).append(
                    {
                        "space_id": space["id"],
                        "space_name": space["name"],
                        "kind": space["kind"],
                        "capabilities": member["capabilities"],
                    }
                )
        return [
            {
                **asdict(item),
                **runtime_overview.get(item.id, {}),
                "space_grants": grants.get(item.id, []),
            }
            for item in runtime.repository.list_users()
        ]

    @app.delete(
        "/v1/admin/users/{user_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
    )
    def delete_user_disabled(user_id: str) -> None:
        del user_id
        raise HTTPException(
            status_code=status.HTTP_405_METHOD_NOT_ALLOWED,
            detail=(
                "Удаление пользователей отключено. Отключите учётную запись — "
                "файлы, аудит и устройства будут сохранены."
            ),
        )

    @app.post(
        "/v1/admin/users",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_user(body: CreateUserRequest) -> dict[str, Any]:
        user, space = runtime.repository.create_user(
            username=body.username,
            display_name=body.display_name,
            quota_bytes=body.quota_gib * 1024**3,
            role=body.role,
            password=(body.password.get_secret_value() if body.password is not None else None),
            email=body.email,
            create_personal_space=body.create_personal_space,
            primary_storage_root_id=body.primary_storage_root_id,
            fallback_storage_root_id=body.fallback_storage_root_id,
        )
        for grant in body.space_grants:
            runtime.repository.set_space_member(
                grant.space_id,
                user.id,
                grant.capabilities.model_dump(),
            )
        result: dict[str, Any] = {
            "user": asdict(user),
            "personal_space": asdict(space) if space is not None else None,
        }
        if body.prepare_access:
            access = runtime.control.prepare_user_access(user.id, email=body.email)
            access["download_url"] = f"/v1/admin/access-packages/{access['invitation_id']}"
            result["access_package"] = access
        return result

    @app.patch(
        "/v1/admin/users/{user_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_user(user_id: str, body: UpdateUserRequest) -> dict[str, Any]:
        return asdict(
            runtime.repository.update_user(
                user_id,
                display_name=body.display_name,
                email=body.email,
                role=body.role,
                quota_bytes=body.quota_gib * 1024**3,
                enabled=body.enabled,
            )
        )

    @app.get(
        "/v1/admin/spaces",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def list_admin_spaces(include_archived: bool = False) -> list[dict[str, Any]]:
        return runtime.repository.list_spaces_admin(include_archived=include_archived)

    @app.delete("/v1/admin/spaces/{space_id}", tags=["manager"], dependencies=[Depends(require_manager)])
    def archive_admin_space(space_id: str) -> dict[str, bool]:
        runtime.repository.set_space_archived(space_id, True)
        return {"archived": True, "files_preserved": True}

    @app.post("/v1/admin/spaces/{space_id}/restore", tags=["manager"], dependencies=[Depends(require_manager)])
    def restore_admin_space(space_id: str) -> dict[str, bool]:
        runtime.repository.set_space_archived(space_id, False)
        return {"restored": True}

    @app.post(
        "/v1/admin/spaces",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_admin_space(body: CreateSpaceRequest) -> dict[str, Any]:
        return asdict(
            runtime.repository.create_shared_space(
                name=body.name,
                quota_bytes=body.quota_gib * 1024**3,
                primary_storage_root_id=body.primary_storage_root_id,
                fallback_storage_root_id=body.fallback_storage_root_id,
            )
        )

    @app.patch(
        "/v1/admin/spaces/{space_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def update_admin_space(space_id: str, body: UpdateSpaceRequest) -> dict[str, Any]:
        return asdict(
            runtime.repository.update_space(
                space_id,
                name=body.name,
                quota_bytes=body.quota_gib * 1024**3,
                primary_storage_root_id=body.primary_storage_root_id,
                fallback_storage_root_id=body.fallback_storage_root_id,
                enabled=body.enabled,
            )
        )

    @app.put(
        "/v1/admin/spaces/{space_id}/members/{user_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def set_admin_space_member(
        space_id: str,
        user_id: str,
        body: SpaceCapabilitiesRequest,
    ) -> dict[str, bool]:
        runtime.repository.set_space_member(space_id, user_id, body.model_dump())
        return body.model_dump()

    @app.post(
        "/v1/admin/users/{user_id}/access-package",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def prepare_user_access(user_id: str, body: PrepareAccessRequest) -> dict[str, Any]:
        result = runtime.control.prepare_user_access(
            user_id,
            email=body.email,
            ttl_seconds=body.ttl_seconds,
            send_email=body.send_email,
        )
        result["download_url"] = f"/v1/admin/access-packages/{result['invitation_id']}"
        return result

    @app.get(
        "/v1/admin/access-packages/{invitation_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def download_access_package(invitation_id: str) -> Response:
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", invitation_id):
            raise HTTPException(status_code=404, detail="access package not found")
        path = (
            runtime.config.data_directory
            / "access-packages"
            / f"access-{invitation_id}.cloud-access.json"
        ).resolve()
        expected_parent = (runtime.config.data_directory / "access-packages").resolve()
        if path.parent != expected_parent or not path.is_file():
            raise HTTPException(status_code=404, detail="access package not found")
        return Response(
            content=path.read_bytes(),
            media_type="application/json",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="access-{invitation_id}.cloud-access.json"'
                )
            },
        )

    @app.put(
        "/v1/admin/users/{user_id}/password",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def set_user_password(user_id: str, body: SetUserPasswordRequest) -> dict[str, Any]:
        return asdict(
            runtime.repository.set_user_password(
                user_id,
                body.password.get_secret_value(),
            )
        )

    @app.post(
        "/v1/admin/invitations",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
        status_code=status.HTTP_201_CREATED,
    )
    def create_invitation(body: CreateInvitationRequest) -> dict[str, Any]:
        invitation_id, code, expires_at = runtime.repository.create_invitation(
            body.user_id, body.ttl_seconds
        )
        return {
            "id": invitation_id,
            "code": code,
            "expires_at": expires_at,
            "one_time": True,
        }

    @app.delete(
        "/v1/admin/invitations/{invitation_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_invitation(invitation_id: str) -> dict[str, bool]:
        return {"cancelled": runtime.repository.cancel_invitation(invitation_id)}

    @app.get("/v1/admin/devices", tags=["manager"], dependencies=[Depends(require_manager)])
    def list_devices() -> list[dict[str, Any]]:
        return [asdict(item) for item in runtime.repository.list_devices()]

    @app.post(
        "/v1/admin/devices/{device_id}/approve",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def approve_device(device_id: str) -> dict[str, Any]:
        return asdict(runtime.repository.set_device_status(device_id, "trusted"))

    @app.post(
        "/v1/admin/devices/{device_id}/revoke",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def revoke_device(device_id: str) -> dict[str, Any]:
        return asdict(runtime.repository.set_device_status(device_id, "revoked"))

    @app.get("/v1/admin/audit", tags=["manager"], dependencies=[Depends(require_manager)])
    def list_audit(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
        return runtime.repository.recent_audit(limit)

    @app.get(
        "/v1/mobile/admin/overview",
        tags=["mobile-control"],
        dependencies=[Depends(require_mobile_admin)],
    )
    def mobile_admin_overview() -> dict[str, Any]:
        diagnostics = runtime.diagnostics.overview()
        zrok = runtime.tunnels.status("zrok")
        control = runtime.control.overview()
        return {
            "summary": runtime.repository.summary(),
            "server_mode": runtime.recovery.server_mode(),
            "diagnostics": {
                "status": diagnostics["status"],
                "active_warning_count": diagnostics["active_warning_count"],
                "active_critical_count": diagnostics["active_critical_count"],
                "latest_scan": diagnostics["latest_scan"],
            },
            "transfers": runtime.storage.transfer_overview(limit=20)["counts"],
            "zrok": {
                "enabled": bool(zrok.get("enabled")),
                "state": str(zrok.get("state") or "disabled"),
                "public_url": str(zrok.get("public_url") or ""),
            },
            "users": [asdict(item) for item in runtime.repository.list_users()],
            "devices": [asdict(item) for item in runtime.repository.list_devices()],
            "storage": runtime.storage.mobile_storage_overview(),
            "backup_policies": [
                runtime.backup_automation.policy_to_dict(item)
                for item in runtime.backup_automation.list_policies()
            ],
            "backups": [
                runtime.storage.backup_to_dict(item)
                for item in runtime.storage.list_backup_jobs(limit=10)
            ],
            "automation": runtime.automation.overview()["scheduler"],
            "control": {
                "profile": control["settings"]["profile"],
                "interface_mode": control["settings"]["interface_mode"],
                "security_mode": control["settings"]["security"]["mode"],
                "browser_access": control["settings"]["browser_access"],
                "power": control["power"],
                "report_schedules": len(control["reports"]["schedules"]),
                "sandbox_available": control["sandbox"]["available"],
                "sandbox_runtime": control["sandbox"]["runtime"],
            },
        }

    @app.post(
        "/v1/mobile/admin/confirm",
        tags=["mobile-control"],
    )
    def mobile_confirm_admin_action(
        body: MobileAdminConfirmationRequest,
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        if body.action not in MOBILE_ADMIN_ACTIONS:
            raise HTTPException(status_code=400, detail="unsupported administrator action")
        if not mobile_confirmation_limiter.allow(device.id):
            raise HTTPException(status_code=429, detail="too many administrator confirmations")
        user = runtime.repository.get_user(device.user_id)
        try:
            authenticated = runtime.repository.authenticate_user_password(
                user.username,
                body.password.get_secret_value(),
            )
        except (InvalidCredential, PermissionDeniedError) as exc:
            raise HTTPException(status_code=403, detail="invalid administrator password") from exc
        if authenticated.id != device.user_id or authenticated.role != "admin":
            raise HTTPException(status_code=403, detail="administrator role is required")
        token, expires_at = runtime.repository.credentials.issue_mobile_confirmation(
            device.user_id,
            device.id,
            body.action,
            password_version=runtime.repository.user_password_version(device.user_id),
        )
        return {"confirmation_token": token, "action": body.action, "expires_at": expires_at}

    @app.post(
        "/v1/mobile/admin/users",
        tags=["mobile-control"],
        status_code=status.HTTP_201_CREATED,
    )
    def mobile_create_user(
        body: CreateUserRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="user.create")
        user, space = runtime.repository.create_user(
            username=body.username,
            display_name=body.display_name,
            quota_bytes=body.quota_gib * 1024**3,
            role=body.role,
            password=body.password.get_secret_value() if body.password else None,
            email=body.email,
            create_personal_space=body.create_personal_space,
            primary_storage_root_id=body.primary_storage_root_id,
            fallback_storage_root_id=body.fallback_storage_root_id,
        )
        for grant in body.space_grants:
            runtime.repository.set_space_member(
                grant.space_id, user.id, grant.capabilities.model_dump()
            )
        result: dict[str, Any] = {
            "user": asdict(user),
            "space": asdict(space) if space is not None else None,
        }
        if body.prepare_access:
            result["access_package"] = runtime.control.prepare_user_access(
                user.id, email=body.email
            )
        return result

    @app.put(
        "/v1/mobile/admin/users/{user_id}/password",
        tags=["mobile-control"],
    )
    def mobile_set_user_password(
        user_id: str,
        body: SetUserPasswordRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="user.password")
        return asdict(
            runtime.repository.set_user_password(user_id, body.password.get_secret_value())
        )

    @app.put(
        "/v1/mobile/admin/users/{user_id}/enabled",
        tags=["mobile-control"],
    )
    def mobile_set_user_enabled(
        user_id: str,
        body: MobileUserEnabledRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        action = "user.enable" if body.enabled else "user.disable"
        consume_mobile_confirmation(confirmation, device=device, action=action)
        if user_id == device.user_id and not body.enabled:
            raise HTTPException(status_code=400, detail="you cannot disable your own account")
        return asdict(runtime.repository.set_user_enabled(user_id, body.enabled))

    @app.post(
        "/v1/mobile/admin/devices/{device_id}/approve",
        tags=["mobile-control"],
    )
    def mobile_approve_device(
        device_id: str,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="device.approve")
        return asdict(runtime.repository.set_device_status(device_id, "trusted"))

    @app.post(
        "/v1/mobile/admin/devices/{device_id}/revoke",
        tags=["mobile-control"],
    )
    def mobile_revoke_device(
        device_id: str,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="device.revoke")
        return asdict(runtime.repository.set_device_status(device_id, "revoked"))

    @app.put(
        "/v1/mobile/admin/server-mode",
        tags=["mobile-control"],
    )
    def mobile_set_server_mode(
        body: SetServerModeRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(
            confirmation,
            device=device,
            action="server.read_only" if body.mode == "read_only" else "server.normal",
        )
        if body.mode == "read_only" and not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="explicit confirmation is required for emergency read-only mode",
            )
        return runtime.recovery.set_server_mode(body.mode, body.reason)

    @app.post(
        "/v1/mobile/admin/diagnostics/scans",
        tags=["mobile-control"],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def mobile_run_diagnostics(
        body: CreateDiagnosticScanRequest,
        background: BackgroundTasks,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(
            confirmation,
            device=device,
            action="diagnostics.full" if body.kind == "full" else "diagnostics.quick",
        )
        scan = runtime.diagnostics.create_scan(body.kind, source="mobile")
        background.add_task(runtime.diagnostics.run_scan_safely, scan.id)
        return runtime.diagnostics.scan_to_dict(scan)

    @app.post(
        "/v1/mobile/admin/backups/{target_root_id}/run",
        tags=["mobile-control"],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def mobile_run_backup(
        target_root_id: str,
        background: BackgroundTasks,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="backup.run")
        job = runtime.backup_automation.queue_policy_run(target_root_id)
        background.add_task(runtime.backup_automation.run_backup_pipeline, job.id)
        return runtime.storage.backup_to_dict(job)

    @app.put(
        "/v1/mobile/admin/storage/{root_id}/write",
        tags=["mobile-control"],
    )
    def mobile_set_storage_write(
        root_id: str,
        body: MobileStorageWriteRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(
            confirmation,
            device=device,
            action="storage.write.resume" if body.enabled else "storage.write.pause",
        )
        return runtime.storage.set_root_write_enabled(
            root_id,
            body.enabled,
            actor_user_id=device.user_id,
        )

    @app.put(
        "/v1/mobile/admin/automation/settings",
        tags=["mobile-control"],
    )
    def mobile_set_automation(
        body: MobileAutomationRequest,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(
            confirmation,
            device=device,
            action="automation.resume" if body.enabled else "automation.pause",
        )
        settings = runtime.automation.settings()
        return runtime.automation.set_settings(
            enabled=body.enabled,
            interval_seconds=int(settings["interval_seconds"]),
        )

    @app.post(
        "/v1/mobile/admin/tunnels/{provider_id}/restart",
        tags=["mobile-control"],
    )
    def mobile_restart_tunnel(
        provider_id: str,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, Any]:
        consume_mobile_confirmation(confirmation, device=device, action="tunnel.restart")
        try:
            return runtime.tunnels.restart(provider_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="tunnel provider not found") from exc

    @app.post(
        "/v1/mobile/admin/core/restart",
        tags=["mobile-control"],
        status_code=status.HTTP_202_ACCEPTED,
    )
    def mobile_restart_core(
        background: BackgroundTasks,
        confirmation: str = Header(alias="X-Cloud-Admin-Confirmation"),
        device: DeviceRecord = Depends(require_mobile_admin),  # noqa: B008
    ) -> dict[str, bool]:
        consume_mobile_confirmation(confirmation, device=device, action="core.restart")
        callback = app.state.restart_callback
        if callback is None:
            raise HTTPException(status_code=409, detail="restart is unavailable in embedded mode")
        background.add_task(callback)
        return {"accepted": True}

    @app.post("/v1/admin/shutdown", tags=["manager"], dependencies=[Depends(require_manager)])
    def shutdown(background: BackgroundTasks) -> dict[str, bool]:
        callback = app.state.shutdown_callback
        if callback is None:
            raise HTTPException(status_code=409, detail="shutdown is unavailable in embedded mode")
        background.add_task(callback)
        return {"accepted": True}

    @app.get(
        "/v1/admin/dynamic-pairing-code",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def dynamic_pairing_code(user_id: str = "") -> dict[str, Any]:
        return current_dynamic_pairing(user_id)

    @app.post("/v1/pairing/redeem", tags=["pairing"], status_code=status.HTTP_201_CREATED)
    def redeem_invitation(body: RedeemInvitationRequest, request: Request) -> dict[str, Any]:
        remote = request.client.host if request.client else "unknown"
        limiter = remote_pairing_limiter if request.state.external_request else pairing_limiter
        if not limiter.allow(remote):
            raise HTTPException(status_code=429, detail="too many pairing attempts")
        if body.code.strip().upper().startswith("CS3."):
            verified = verify_dynamic_pairing_code(
                body.code,
                signing_key=runtime.repository.credentials.hmac_secret,
                expected_server_id=dynamic_server_id,
            )
            current = current_dynamic_pairing(verified.user_id)
            if not secrets.compare_digest(verified.raw_code, current["code"]):
                raise InvalidCredential("dynamic pairing code is invalid or expired")
            result = runtime.repository.redeem_dynamic_pairing(
                device_name=body.device_name,
                platform=body.platform,
                remote_address=remote,
                user_id=verified.user_id,
            )
            message = "Подключение выполнено"
        else:
            result = runtime.repository.redeem_invitation(
                code=body.code,
                password=body.password.get_secret_value() if body.password is not None else None,
                device_name=body.device_name,
                platform=body.platform,
                remote_address=remote,
            )
            message = "Ожидается подтверждение администратора"
        return {
            "device": asdict(result.device),
            "device_token": result.device_token,
            "message": message,
        }

    @app.post(
        "/v1/auth/device-login",
        tags=["pairing"],
        status_code=status.HTTP_201_CREATED,
    )
    def login_new_device(body: DeviceLoginRequest, request: Request) -> dict[str, Any]:
        remote_address = request.client.host if request.client else "unknown"
        limiter_key = f"{remote_address}:{body.username.strip().casefold()}"
        limiter = remote_login_limiter if request.state.external_request else pairing_limiter
        if not limiter.allow(limiter_key):
            raise HTTPException(status_code=429, detail="too many account login attempts")
        result = runtime.repository.request_device_login(
            username=body.username,
            password=body.password.get_secret_value(),
            device_name=body.device_name,
            platform=body.platform,
            remote_address=remote_address,
        )
        return {
            "device": asdict(result.device),
            "device_token": result.device_token,
            "message": "Ожидается подтверждение администратора",
        }

    @app.post(
        "/v1/auth/google-device-login",
        tags=["pairing"],
        status_code=status.HTTP_201_CREATED,
    )
    def google_device_login(body: GoogleDeviceLoginRequest, request: Request) -> dict[str, Any]:
        client_id = str(
            runtime.control.settings().get("integrations", {}).get("email", {}).get(
                "gmail_client_id", ""
            )
        )
        if not client_id:
            raise HTTPException(status_code=409, detail="Google-вход не настроен")
        remote_address = request.client.host if request.client else "unknown"
        limiter = remote_login_limiter if request.state.external_request else pairing_limiter
        if not limiter.allow(f"google:{remote_address}"):
            raise HTTPException(status_code=429, detail="too many Google login attempts")
        token = urllib.parse.quote(body.id_token.get_secret_value(), safe="")
        google_request = urllib.request.Request(
            f"https://oauth2.googleapis.com/tokeninfo?id_token={token}",
            headers={"User-Agent": "CloudStorageCore/1"},
        )
        try:
            with urllib.request.urlopen(google_request, timeout=15) as response:
                identity = json.loads(response.read().decode("utf-8"))
        except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=401, detail="Google не подтвердил вход") from exc
        if (
            identity.get("aud") != client_id
            or str(identity.get("email_verified", "")).casefold() not in {"true", "1"}
            or not identity.get("email")
        ):
            raise HTTPException(status_code=401, detail="Google-вход не прошёл проверку")
        result = runtime.repository.request_google_device_login(
            email=str(identity["email"]),
            device_name=body.device_name,
            platform=body.platform,
            remote_address=remote_address,
        )
        return {
            "device": asdict(result.device),
            "device_token": result.device_token,
            "message": "Ожидается подтверждение администратора",
        }

    @app.get("/v1/pairing/status", tags=["pairing"])
    def pairing_status(
        device: DeviceRecord = Depends(require_pairing_device),  # noqa: B008
    ):
        return {"device_id": device.id, "status": device.status}

    @app.post("/v1/remote/session", tags=["pairing"])
    def create_remote_session(
        body: RemoteSessionRequest,
        request: Request,
        device: DeviceRecord = Depends(require_pairing_device),  # noqa: B008
    ) -> dict[str, Any]:
        remote_address = request.client.host if request.client else "unknown"
        limiter_key = f"{remote_address}:{body.username.strip().casefold()}"
        if not remote_login_limiter.allow(limiter_key):
            raise HTTPException(status_code=429, detail="too many internet login attempts")
        user = runtime.repository.authenticate_user_password(
            body.username,
            body.password.get_secret_value(),
        )
        if user.id != device.user_id:
            raise PermissionDeniedError("invalid username or password")
        token, expires_at = runtime.repository.credentials.issue_remote_session(
            user.id,
            device.id,
            password_version=runtime.repository.user_password_version(user.id),
        )
        runtime.repository.record_audit(
            actor_type="device",
            actor_id=device.id,
            action="remote.session.created",
            target_type="user",
            target_id=user.id,
            detail="Создана интернет-сессия после проверки логина, пароля и устройства",
            remote_address=remote_address,
        )
        return {
            "session_token": token,
            "expires_at": expires_at,
            "username": user.username,
        }

    @app.get("/v1/spaces", tags=["files"])
    def list_spaces(device: DeviceRecord = Depends(require_device)):  # noqa: B008
        result = []
        for item in runtime.repository.list_spaces_for_user(device.user_id):
            used = runtime.storage.space_usage(item.id)
            result.append({
                **asdict(item),
                "used_bytes": used,
                "free_bytes": max(0, item.quota_bytes - used),
            })
        return result

    @app.get("/v1/spaces/{space_id}/entries", tags=["files"])
    def list_entries(
        space_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
        directory: str = Query(default="", max_length=1024),
    ):
        return runtime.storage.list_entries(space_id, device.user_id, directory)

    @app.get("/v1/spaces/{space_id}/search", tags=["files"])
    def search_entries(
        space_id: str,
        query: str = Query(min_length=2, max_length=200),
        directory: str = Query(default="", max_length=1024),
        limit: int = Query(default=100, ge=1, le=200),
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        return runtime.storage.search_entries(
            space_id,
            device.user_id,
            query,
            directory=directory,
            limit=limit,
        )

    @app.get("/v1/operations", tags=["files"])
    def recent_operations(
        limit: int = Query(default=100, ge=1, le=200),
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ) -> list[dict[str, Any]]:
        return runtime.repository.recent_user_operations(device.user_id, limit)

    @app.get("/v1/shares", tags=["files"])
    def list_shares(
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ) -> list[dict[str, Any]]:
        return runtime.storage.list_public_shares(device.user_id)

    @app.post("/v1/shares", tags=["files"], status_code=status.HTTP_201_CREATED)
    def create_share(
        body: CreateShareRequest,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ) -> dict[str, Any]:
        share = runtime.storage.create_public_share(
            body.space_id,
            device.user_id,
            body.logical_path,
            body.kind,
            ttl_hours=body.ttl_hours,
        )
        return {
            **asdict(share),
            "url_path": f"/v1/public/shares/{quote(share.token)}",
        }

    @app.delete("/v1/shares/{share_id}", tags=["files"])
    def revoke_share(
        share_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ) -> dict[str, bool]:
        return {"revoked": runtime.storage.revoke_public_share(share_id, device.user_id)}

    @app.get("/v1/public/shares/{token}", tags=["public-share"])
    def public_share_overview(token: str) -> dict[str, Any]:
        return runtime.storage.public_share_overview(token)

    def public_share_stream(token: str, relative_path: str = "") -> StreamingResponse:
        share, record, physical_path = runtime.storage.resolve_public_share_download(
            token,
            relative_path,
        )
        filename = PurePosixPath(record.logical_path).name
        transfer_id, stream = runtime.storage.stream_download(
            record,
            physical_path,
            share.owner_user_id,
        )
        return StreamingResponse(
            stream,
            media_type=record.content_type,
            headers={
                "ETag": f'"sha256:{record.sha256}"',
                "Content-Length": str(record.size_bytes),
                "Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}",
                "X-Transfer-ID": transfer_id,
            },
        )

    @app.get("/v1/public/shares/{token}/download", tags=["public-share"])
    def download_public_shared_file(token: str) -> StreamingResponse:
        return public_share_stream(token)

    @app.get("/v1/public/shares/{token}/files/{relative_path:path}", tags=["public-share"])
    def download_public_shared_directory_file(
        token: str,
        relative_path: Annotated[str, Path(min_length=1, max_length=1024)],
    ) -> StreamingResponse:
        return public_share_stream(token, relative_path)

    @app.post(
        "/v1/spaces/{space_id}/directories",
        tags=["files"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_directory(
        space_id: str,
        body: CreateDirectoryRequest,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        return runtime.storage.create_directory(space_id, device.user_id, body.logical_path)

    @app.delete("/v1/spaces/{space_id}/directories/{logical_path:path}", tags=["files"])
    def delete_directory(
        space_id: str,
        logical_path: Annotated[str, Path(min_length=1, max_length=1024)],
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        return {"deleted": runtime.storage.delete_directory(space_id, device.user_id, logical_path)}

    @app.post("/v1/spaces/{space_id}/moves", tags=["files"])
    def move_entry(
        space_id: str,
        body: MoveEntryRequest,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        return runtime.storage.move_entry(
            space_id,
            device.user_id,
            body.source_path,
            body.destination_path,
            body.kind,
        )

    @app.post(
        "/v1/spaces/{space_id}/uploads",
        tags=["files"],
        status_code=status.HTTP_201_CREATED,
    )
    def create_resumable_upload(
        space_id: str,
        body: CreateResumableUploadRequest,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        upload = runtime.storage.create_resumable_upload(
            space_id=space_id,
            user_id=device.user_id,
            logical_path=body.logical_path,
            expected_size=body.size_bytes,
            expected_sha256=body.sha256,
            content_type=body.content_type,
        )
        return JSONResponse(
            status_code=201,
            content=runtime.storage.resumable_to_dict(upload),
            headers={"Upload-Offset": str(upload.received_bytes)},
        )

    @app.get("/v1/uploads/{upload_id}", tags=["files"])
    def resumable_upload_status(
        upload_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        upload = runtime.storage.get_resumable_upload(upload_id, device.user_id)
        return JSONResponse(
            content=runtime.storage.resumable_to_dict(upload),
            headers={"Upload-Offset": str(upload.received_bytes)},
        )

    @app.patch("/v1/uploads/{upload_id}", tags=["files"])
    async def append_resumable_upload(
        request: Request,
        upload_id: str,
        upload_offset: Annotated[int, Header(alias="Upload-Offset", ge=0)],
        content_length: Annotated[int, Header(alias="Content-Length", ge=1)],
        device: DeviceRecord = Depends(require_device),  # noqa: B008
        content_type: Annotated[str | None, Header()] = None,
    ):
        if content_type and content_type.casefold() != "application/offset+octet-stream":
            raise HTTPException(status_code=415, detail="invalid resumable chunk content type")
        if content_length > 8 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="upload chunk exceeds 8 MiB")
        payload = await request.body()
        if len(payload) != content_length:
            raise HTTPException(status_code=400, detail="upload chunk length mismatch")
        upload = await asyncio.to_thread(
            runtime.storage.append_resumable_upload,
            upload_id,
            device.user_id,
            upload_offset,
            payload,
        )
        return JSONResponse(
            content=runtime.storage.resumable_to_dict(upload),
            headers={"Upload-Offset": str(upload.received_bytes)},
        )

    @app.post("/v1/uploads/{upload_id}/complete", tags=["files"])
    async def complete_resumable_upload(
        upload_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        record = await asyncio.to_thread(
            runtime.storage.complete_resumable_upload,
            upload_id,
            device.user_id,
        )
        return JSONResponse(
            content=runtime.storage.to_dict(record),
            headers={"ETag": f'"sha256:{record.sha256}"'},
        )

    @app.delete("/v1/uploads/{upload_id}", tags=["files"])
    def cancel_resumable_upload(
        upload_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        return {"cancelled": runtime.storage.cancel_resumable_upload(upload_id, device.user_id)}

    @app.put("/v1/spaces/{space_id}/files/{logical_path:path}", tags=["files"])
    async def upload_file(
        request: Request,
        space_id: str,
        logical_path: Annotated[str, Path(min_length=1, max_length=1024)],
        device: DeviceRecord = Depends(require_device),  # noqa: B008
        content_length: Annotated[int | None, Header(ge=0)] = None,
        content_type: Annotated[str | None, Header()] = None,
        content_encoding: Annotated[str | None, Header()] = None,
    ):
        if content_encoding and content_encoding.casefold() not in {"identity"}:
            raise HTTPException(status_code=415, detail="encoded request bodies are not accepted")
        session = runtime.storage.prepare_upload(
            space_id=space_id,
            user_id=device.user_id,
            logical_path=logical_path,
            content_type=content_type,
            expected_size=content_length,
        )
        try:
            async for chunk in request.stream():
                session.write(chunk)
            record = await asyncio.to_thread(session.commit)
        except Exception:
            session.abort()
            raise
        return JSONResponse(
            status_code=201,
            content=runtime.storage.to_dict(record),
            headers={"ETag": f'"sha256:{record.sha256}"'},
        )

    @app.get("/v1/spaces/{space_id}/files/{logical_path:path}", tags=["files"])
    def download_file(
        space_id: str,
        logical_path: Annotated[str, Path(min_length=1, max_length=1024)],
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        record, physical_path = runtime.storage.resolve_download(
            space_id, device.user_id, logical_path
        )
        filename = PurePosixPath(record.logical_path).name
        transfer_id, stream = runtime.storage.stream_download(
            record,
            physical_path,
            device.user_id,
        )
        return StreamingResponse(
            stream,
            media_type=record.content_type,
            headers={
                "ETag": f'"sha256:{record.sha256}"',
                "Content-Length": str(record.size_bytes),
                "Content-Disposition": f"attachment; filename*=utf-8''{quote(filename)}",
                "X-Transfer-ID": transfer_id,
            },
        )

    @app.delete("/v1/spaces/{space_id}/files/{logical_path:path}", tags=["files"])
    def delete_file(
        space_id: str,
        logical_path: Annotated[str, Path(min_length=1, max_length=1024)],
        device: DeviceRecord = Depends(require_device),  # noqa: B008
    ):
        record = runtime.storage.soft_delete(space_id, device.user_id, logical_path)
        return {"deleted": True, "file": runtime.storage.to_dict(record)}

    browser.register(app, redeem_invitation, RedeemInvitationRequest)
    return app
