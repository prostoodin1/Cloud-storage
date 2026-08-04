from __future__ import annotations

import asyncio
import re
import secrets
import sqlite3
import threading
import time
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


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    quota_gib: int = Field(default=100, ge=1, le=1_000_000)
    role: str = Field(default="member", pattern="^(admin|member)$")
    password: SecretStr | None = None


class SetUserPasswordRequest(BaseModel):
    password: SecretStr


class CreateInvitationRequest(BaseModel):
    user_id: str
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class RedeemInvitationRequest(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    password: SecretStr
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)


class DeviceLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: SecretStr
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
            "^(notify|quick_scan|full_scan|read_only|run_backup|"
            "reconcile_mirrors|restart_tunnel)$"
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


class SyncStorageRootsRequest(BaseModel):
    roots: list[StorageRootRequest] = Field(max_length=128)


class TransferSettingsRequest(BaseModel):
    staging_enabled: bool = False
    staging_path: str = Field(default="", max_length=2048)


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


def build_runtime(config: CoreConfig | None = None) -> CoreRuntime:
    config = config or CoreConfig.from_environment()
    config.ensure_directories()
    secrets_store = CoreSecrets.load_or_create(config)
    database = Database(config.database_path)
    database.initialize()
    credentials = CredentialService.from_secret(secrets_store.hmac_secret)
    repository = CoreRepository(database, credentials)
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
        load_or_create_tls_identity(config)
        if config.lan_enabled or config.remote_enabled
        else None
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
        started_monotonic=time.monotonic(),
    )


def create_app(config: CoreConfig | None = None) -> FastAPI:
    runtime = build_runtime(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.diagnostics.start_monitor()
        runtime.backup_automation.start_scheduler()
        runtime.automation.start_scheduler()
        try:
            yield
        finally:
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
    bearer = HTTPBearer(auto_error=False)
    pairing_limiter = SlidingWindowLimiter(limit=10, window_seconds=300)
    remote_pairing_limiter = SlidingWindowLimiter(limit=5, window_seconds=900)
    remote_login_limiter = SlidingWindowLimiter(limit=5, window_seconds=300)
    remote_request_limiter = SlidingWindowLimiter(limit=600, window_seconds=60)
    remote_audit_limiter = SlidingWindowLimiter(limit=30, window_seconds=3600)
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
            "/v1/health",
            "/v1/auth/device-login",
            "/v1/pairing/redeem",
            "/v1/pairing/status",
            "/v1/remote/session",
            "/v1/spaces",
            "/v1/mobile/admin/overview",
            "/v1/mobile/admin/server-mode",
        }:
            return True
        patterns = (
            r"/v1/spaces/[^/]+/entries",
            r"/v1/spaces/[^/]+/directories",
            r"/v1/spaces/[^/]+/directories/.+",
            r"/v1/spaces/[^/]+/moves",
            r"/v1/spaces/[^/]+/uploads",
            r"/v1/spaces/[^/]+/files/.+",
            r"/v1/uploads/[^/]+",
            r"/v1/uploads/[^/]+/complete",
            r"/v1/mobile/admin/devices/[^/]+/(approve|revoke)",
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
        request.state.zrok_request = zrok_request
        request.state.external_request = external_request
        remote_address = request.client.host if request.client else "unknown"
        local_host = (request.url.hostname or "").casefold()
        invalid_local_host = not lan_request and not external_request and local_host not in {
            "127.0.0.1",
            "::1",
            "localhost",
            "testserver",
        }
        restricted_path = request.url.path.startswith("/v1/admin") or request.url.path in {
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        if invalid_local_host:
            response = JSONResponse(status_code=400, content={"detail": "invalid host header"})
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
            and request.url.path == "/v1/pairing/redeem"
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
                    "/v1/mobile/admin/server-mode",
                )
            )
            and request.url.path != "/v1/admin/shutdown"
            and not request.url.path.endswith("/revoke")
        ):
            response = JSONResponse(
                status_code=503,
                content={"detail": "server is in emergency read-only mode"},
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
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
        remote_session: Annotated[
            str | None, Header(alias="X-Cloud-Remote-Session")
        ] = None,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> DeviceRecord:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="device authorization required")
        try:
            device = runtime.repository.authenticate_device(credentials.credentials)
            if request.state.external_request:
                runtime.repository.credentials.verify_remote_session(
                    remote_session or "",
                    user_id=device.user_id,
                    device_id=device.id,
                    password_version=runtime.repository.user_password_version(
                        device.user_id
                    ),
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

    @app.exception_handler(NotFoundError)
    async def not_found_handler(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ConflictError)
    async def conflict_handler(request: Request, exc: ConflictError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(InvalidCredential)
    @app.exception_handler(InvalidLogicalPath)
    @app.exception_handler(InvalidStorageRoot)
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
            "state": zrok_status["state"],
            "installed": zrok_status["installed"],
            "process_running": zrok_status["process_running"],
            "listener_port": runtime.config.zrok_port,
            "public_url": zrok_status["public_url"],
            "share_type": zrok_status["share_type"],
            "login_required": True,
            "manager_api_exposed": False,
            "automatic_router_changes": False,
        }
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
        return [
            runtime.diagnostics.scan_to_dict(item)
            for item in runtime.diagnostics.list_scans()
        ]

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
            for item in runtime.diagnostics.list_incidents(
                include_resolved=include_resolved
            )
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
        verification = runtime.backup_automation.create_verification(
            job_id, body.target_root_id
        )
        background_tasks.add_task(
            runtime.backup_automation.run_verification, verification.id
        )
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
        return [asdict(item) for item in runtime.repository.list_users()]

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
        )
        return {"user": asdict(user), "personal_space": asdict(space)}

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
        }

    @app.post(
        "/v1/mobile/admin/devices/{device_id}/approve",
        tags=["mobile-control"],
        dependencies=[Depends(require_mobile_admin)],
    )
    def mobile_approve_device(device_id: str) -> dict[str, Any]:
        return asdict(runtime.repository.set_device_status(device_id, "trusted"))

    @app.post(
        "/v1/mobile/admin/devices/{device_id}/revoke",
        tags=["mobile-control"],
        dependencies=[Depends(require_mobile_admin)],
    )
    def mobile_revoke_device(device_id: str) -> dict[str, Any]:
        return asdict(runtime.repository.set_device_status(device_id, "revoked"))

    @app.put(
        "/v1/mobile/admin/server-mode",
        tags=["mobile-control"],
        dependencies=[Depends(require_mobile_admin)],
    )
    def mobile_set_server_mode(body: SetServerModeRequest) -> dict[str, Any]:
        if body.mode == "read_only" and not body.confirmed:
            raise HTTPException(
                status_code=400,
                detail="explicit confirmation is required for emergency read-only mode",
            )
        return runtime.recovery.set_server_mode(body.mode, body.reason)

    @app.post("/v1/admin/shutdown", tags=["manager"], dependencies=[Depends(require_manager)])
    def shutdown(background: BackgroundTasks) -> dict[str, bool]:
        callback = app.state.shutdown_callback
        if callback is None:
            raise HTTPException(status_code=409, detail="shutdown is unavailable in embedded mode")
        background.add_task(callback)
        return {"accepted": True}

    @app.post("/v1/pairing/redeem", tags=["pairing"], status_code=status.HTTP_201_CREATED)
    def redeem_invitation(body: RedeemInvitationRequest, request: Request) -> dict[str, Any]:
        remote = request.client.host if request.client else "unknown"
        limiter = remote_pairing_limiter if request.state.remote_request else pairing_limiter
        if not limiter.allow(remote):
            raise HTTPException(status_code=429, detail="too many pairing attempts")
        result = runtime.repository.redeem_invitation(
            code=body.code,
            password=body.password.get_secret_value(),
            device_name=body.device_name,
            platform=body.platform,
            remote_address=remote,
        )
        return {
            "device": asdict(result.device),
            "device_token": result.device_token,
            "message": "Ожидается подтверждение администратора",
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
        return [asdict(item) for item in runtime.repository.list_spaces_for_user(device.user_id)]

    @app.get("/v1/spaces/{space_id}/entries", tags=["files"])
    def list_entries(
        space_id: str,
        device: DeviceRecord = Depends(require_device),  # noqa: B008
        directory: str = Query(default="", max_length=1024),
    ):
        return runtime.storage.list_entries(space_id, device.user_id, directory)

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
        return {
            "deleted": runtime.storage.delete_directory(
                space_id, device.user_id, logical_path
            )
        }

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

    return app
