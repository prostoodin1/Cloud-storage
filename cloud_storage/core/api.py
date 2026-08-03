from __future__ import annotations

import asyncio
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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cloud_storage import __version__
from cloud_storage.core.config import CoreConfig, CoreSecrets
from cloud_storage.core.database import Database
from cloud_storage.core.diagnostics import DiagnosticsService
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
from cloud_storage.core.tls import TlsIdentity, lan_endpoints, load_or_create_tls_identity


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    display_name: str = Field(min_length=1, max_length=80)
    quota_gib: int = Field(default=100, ge=1, le=1_000_000)
    role: str = Field(default="member", pattern="^(admin|member)$")


class CreateInvitationRequest(BaseModel):
    user_id: str
    ttl_seconds: int = Field(default=900, ge=60, le=3600)


class RedeemInvitationRequest(BaseModel):
    code: str = Field(min_length=8, max_length=16)
    password: SecretStr
    device_name: str = Field(min_length=1, max_length=100)
    platform: str = Field(min_length=1, max_length=50)


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


class CreateDiagnosticScanRequest(BaseModel):
    kind: str = Field(default="quick", pattern="^(quick|full)$")


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


class SyncStorageRootsRequest(BaseModel):
    roots: list[StorageRootRequest] = Field(max_length=128)


@dataclass(slots=True)
class CoreRuntime:
    config: CoreConfig
    secrets: CoreSecrets
    database: Database
    repository: CoreRepository
    storage: StorageService
    diagnostics: DiagnosticsService
    recovery: RecoveryService
    tls_identity: TlsIdentity | None
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
    storage.recover_interrupted_maintenance()
    diagnostics = DiagnosticsService(config, database, repository)
    diagnostics.recover_interrupted_scans()
    recovery = RecoveryService(database, repository, storage)
    recovery.recover_interrupted_jobs()
    tls_identity = load_or_create_tls_identity(config) if config.lan_enabled else None
    return CoreRuntime(
        config=config,
        secrets=secrets_store,
        database=database,
        repository=repository,
        storage=storage,
        diagnostics=diagnostics,
        recovery=recovery,
        tls_identity=tls_identity,
        started_monotonic=time.monotonic(),
    )


def create_app(config: CoreConfig | None = None) -> FastAPI:
    runtime = build_runtime(config)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        runtime.diagnostics.start_monitor()
        try:
            yield
        finally:
            runtime.diagnostics.stop_monitor()

    app = FastAPI(
        title="Cloud Storage Server Core",
        version=__version__,
        description="Local API for the Cloud Storage Server Manager and trusted devices.",
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    app.state.shutdown_callback = None
    bearer = HTTPBearer(auto_error=False)
    pairing_limiter = SlidingWindowLimiter(limit=10, window_seconds=300)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=(
            ["*"]
            if runtime.config.lan_enabled
            else ["127.0.0.1", "localhost", "[::1]", "testserver"]
        ),
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        server = request.scope.get("server")
        lan_request = bool(
            runtime.config.lan_enabled
            and server
            and len(server) > 1
            and server[1] == runtime.config.lan_port
        )
        restricted_path = request.url.path.startswith("/v1/admin") or request.url.path in {
            "/docs",
            "/redoc",
            "/openapi.json",
        }
        if lan_request and restricted_path:
            response = JSONResponse(status_code=404, content={"detail": "not found"})
        elif request.method in {"POST", "PUT", "PATCH", "DELETE"} and (
            runtime.recovery.server_mode()["mode"] == "read_only"
            and not request.url.path.startswith(
                (
                    "/v1/admin/server-mode",
                    "/v1/admin/diagnostics",
                    "/v1/admin/restores",
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
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),  # noqa: B008
    ) -> DeviceRecord:
        if credentials is None or credentials.scheme.casefold() != "bearer":
            raise HTTPException(status_code=401, detail="device authorization required")
        try:
            return runtime.repository.authenticate_device(credentials.credentials)
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
        }

    @app.get("/v1/admin/summary", tags=["manager"], dependencies=[Depends(require_manager)])
    def admin_summary() -> dict[str, Any]:
        return runtime.repository.summary()

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
        background_tasks.add_task(runtime.storage.run_backup_job, job.id)
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
        background_tasks.add_task(runtime.storage.run_backup_job, job.id)
        return runtime.storage.backup_to_dict(job)

    @app.delete(
        "/v1/admin/backups/{job_id}",
        tags=["manager"],
        dependencies=[Depends(require_manager)],
    )
    def cancel_backup(job_id: str) -> dict[str, Any]:
        return runtime.storage.backup_to_dict(runtime.storage.cancel_backup_job(job_id))

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
        )
        return {"user": asdict(user), "personal_space": asdict(space)}

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
        if not pairing_limiter.allow(remote):
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

    @app.get("/v1/pairing/status", tags=["pairing"])
    def pairing_status(
        device: DeviceRecord = Depends(require_pairing_device),  # noqa: B008
    ):
        return {"device_id": device.id, "status": device.status}

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
        return FileResponse(
            physical_path,
            media_type=record.content_type,
            filename=filename,
            headers={
                "ETag": f'"sha256:{record.sha256}"',
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
