from __future__ import annotations

import hashlib
import json
import locale
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cloud_storage.core.config import CoreConfig, CoreSecrets


class CoreUnavailable(ConnectionError):
    pass


class CoreApiError(RuntimeError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(slots=True)
class CoreClient:
    config: CoreConfig

    @classmethod
    def from_environment(cls) -> CoreClient:
        return cls(CoreConfig.from_environment())

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self.config.host in {"localhost", "::1"} else self.config.host
        return f"http://{host}:{self.config.port}"

    def health(self, timeout: float = 0.6) -> dict[str, Any]:
        return self._request("/v1/health", timeout=timeout)

    def try_health(self, timeout: float = 0.4) -> dict[str, Any] | None:
        try:
            return self.health(timeout)
        except (CoreUnavailable, CoreApiError):
            return None

    def summary(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/summary")

    def diagnostics(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/diagnostics")

    def tunnels(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/tunnels")

    def integrations(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/integrations")

    def control_center(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/control-center", timeout=5.0)

    def update_control_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/settings", method="PUT", payload=values
        )

    def apply_system_preset(self, preset_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/control-center/presets/{preset_id}/apply", method="POST"
        )

    def install_automation_template(self, template_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/control-center/automations/{template_id}/install",
            method="POST",
        )

    def create_report_schedule(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/reports", method="POST", payload=values
        )

    def run_control_report(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/reports/run",
            method="POST",
            payload=values,
            timeout=15.0,
        )

    def create_sandbox_cell(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/cells", method="POST", payload=values
        )

    def list_sandbox_cells(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/control-center/cells")

    def update_sandbox_cell(self, cell_id: str, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/control-center/cells/{cell_id}", method="PUT", payload=values
        )

    def delete_sandbox_cell(self, cell_id: str) -> None:
        self._manager_request(
            f"/v1/admin/control-center/cells/{cell_id}", method="DELETE"
        )

    def run_sandbox_cell(self, cell_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/control-center/cells/{cell_id}/run",
            method="POST",
            timeout=300.0,
        )

    def install_docker_desktop(self) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/host-tools/docker/install",
            method="POST",
            payload={"confirmed": True},
        )

    def enable_windows_ssh(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/host-tools/ssh/enable",
            method="POST",
            payload={**values, "confirmed": True},
        )

    def disable_windows_ssh(self) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/host-tools/ssh/disable",
            method="POST",
            payload={"confirmed": True},
        )

    def test_integration(self, provider_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/integrations/{provider_id}/test",
            method="POST",
        )

    def wake_on_lan(self, mac_address: str, broadcast: str) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/control-center/wake-on-lan",
            method="POST",
            payload={"mac_address": mac_address, "broadcast": broadcast},
        )

    def list_notifications(
        self,
        *,
        include_acknowledged: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        include = "true" if include_acknowledged else "false"
        return self._manager_request(
            f"/v1/admin/notifications?include_acknowledged={include}&limit={safe_limit}"
        )

    def acknowledge_notification(self, notification_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/notifications/{notification_id}/acknowledge",
            method="POST",
        )

    def automation(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/automation")

    def update_automation_settings(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/automation/settings",
            method="PUT",
            payload=values,
        )

    def preview_automation(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/automation/preview")

    def preview_automation_rule(self, rule_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/automation/rules/{rule_id}/preview")

    def create_automation_rule(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/automation/rules",
            method="POST",
            payload=values,
        )

    def update_automation_rule(
        self,
        rule_id: str,
        values: dict[str, Any],
    ) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/automation/rules/{rule_id}",
            method="PUT",
            payload=values,
        )

    def delete_automation_rule(self, rule_id: str) -> None:
        self._manager_request(
            f"/v1/admin/automation/rules/{rule_id}",
            method="DELETE",
        )

    def evaluate_automation(self) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/automation/evaluate",
            method="POST",
            timeout=10.0,
        )

    def restart_tunnel(self, provider_id: str) -> dict[str, Any]:
        safe_provider_id = provider_id.strip().casefold()
        if not safe_provider_id or not safe_provider_id.replace("-", "").isalnum():
            raise ValueError("invalid tunnel provider id")
        return self._manager_request(
            f"/v1/admin/tunnels/{safe_provider_id}/restart",
            method="POST",
            timeout=10.0,
        )

    def download_support_bundle(self, destination: Path) -> dict[str, Any]:
        destination = destination.expanduser().resolve()
        destination.parent.mkdir(parents=True, exist_ok=True)
        token = CoreSecrets.load_or_create(self.config).manager_token
        request = urllib.request.Request(
            self.base_url + "/v1/admin/support-bundle",
            headers={
                "Accept": "application/zip",
                "Authorization": f"Bearer {token}",
            },
            method="GET",
        )
        temporary = destination.with_name(destination.name + ".tmp")
        try:
            with urllib.request.urlopen(request, timeout=15.0) as response:
                payload = response.read()
                expected = str(response.headers.get("X-Support-Bundle-SHA256", ""))
            actual = hashlib.sha256(payload).hexdigest()
            if len(expected) != 64 or not secrets.compare_digest(actual, expected.casefold()):
                raise CoreUnavailable("support bundle checksum validation failed")
            with temporary.open("wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            return {"path": str(destination), "sha256": actual, "size_bytes": len(payload)}
        except urllib.error.HTTPError as exc:
            try:
                payload_error = json.loads(exc.read().decode("utf-8"))
                detail = payload_error.get("detail", str(exc))
            except (ValueError, UnicodeDecodeError):
                detail = str(exc)
            raise CoreApiError(exc.code, str(detail)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise CoreUnavailable("support bundle could not be downloaded") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def server_mode(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/server-mode")

    def set_server_mode(
        self,
        mode: str,
        reason: str = "",
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/server-mode",
            method="PUT",
            payload={"mode": mode, "reason": reason, "confirmed": confirmed},
        )

    def start_diagnostic_scan(self, kind: str) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/diagnostics/scans",
            method="POST",
            payload={"kind": kind},
            timeout=10.0,
        )

    def remediate_diagnostic_incident(
        self,
        incident_id: str,
        action: str,
        *,
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/diagnostics/incidents/{incident_id}/remediate",
            method="POST",
            payload={"action": action, "confirmed": confirmed},
            timeout=10.0,
        )

    def sync_storage_roots(self, roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._manager_request(
            "/v1/admin/storage-roots",
            method="PUT",
            payload={"roots": roots},
        )

    def list_storage_roots(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/storage-roots")

    def cleanup_storage_root(self, root_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/storage-roots/{root_id}/cleanup",
            method="POST",
            payload={"confirmed": True},
        )

    def transfers(self, limit: int = 100) -> dict[str, Any]:
        safe_limit = max(1, min(int(limit), 500))
        return self._manager_request(f"/v1/admin/transfers?limit={safe_limit}")

    def update_transfer_settings(
        self,
        *,
        staging_enabled: bool,
        staging_path: str,
    ) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/transfers/settings",
            method="PUT",
            payload={
                "staging_enabled": staging_enabled,
                "staging_path": staging_path,
            },
        )

    def retry_transfer(self, transfer_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/transfers/{transfer_id}/retry",
            method="POST",
            timeout=120.0,
        )

    def list_maintenance_jobs(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/maintenance/jobs")

    def create_migration(self, source_root_id: str, target_root_id: str) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/maintenance/migrations",
            method="POST",
            payload={
                "source_root_id": source_root_id,
                "target_root_id": target_root_id,
            },
            timeout=10.0,
        )

    def resume_maintenance_job(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/maintenance/jobs/{job_id}/resume",
            method="POST",
        )

    def cancel_maintenance_job(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/maintenance/jobs/{job_id}",
            method="DELETE",
        )

    def list_backups(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/backups")

    def create_backup(self, target_root_id: str) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/backups",
            method="POST",
            payload={"target_root_id": target_root_id},
            timeout=10.0,
        )

    def backup_automation(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/backup-automation")

    def set_backup_policy(
        self,
        target_root_id: str,
        *,
        enabled: bool,
        interval_hours: int,
        keep_last: int,
        verification_root_id: str | None,
    ) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/backup-policies/{target_root_id}",
            method="PUT",
            payload={
                "enabled": enabled,
                "interval_hours": interval_hours,
                "keep_last": keep_last,
                "verification_root_id": verification_root_id,
            },
        )

    def run_backup_policy(self, target_root_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/backup-policies/{target_root_id}/run",
            method="POST",
            timeout=10.0,
        )

    def resume_backup(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/backups/{job_id}/resume", method="POST")

    def cancel_backup(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/backups/{job_id}", method="DELETE")

    def verify_backup(self, job_id: str, target_root_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/backups/{job_id}/verify",
            method="POST",
            payload={"target_root_id": target_root_id},
            timeout=10.0,
        )

    def cancel_backup_verification(self, verification_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/backup-verifications/{verification_id}",
            method="DELETE",
        )

    def list_restores(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/restores")

    def create_restore(self, backup_job_id: str, target_root_id: str) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/restores",
            method="POST",
            payload={
                "backup_job_id": backup_job_id,
                "target_root_id": target_root_id,
            },
            timeout=10.0,
        )

    def resume_restore(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/restores/{job_id}/resume", method="POST")

    def cancel_restore(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/restores/{job_id}", method="DELETE")

    def mirror_status(self) -> dict[str, Any]:
        return self._manager_request("/v1/admin/mirrors")

    def reconcile_mirror(self, root_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/mirrors/{root_id}/reconcile",
            method="POST",
            timeout=10.0,
        )

    def resume_mirror_job(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/mirrors/jobs/{job_id}/resume",
            method="POST",
        )

    def cancel_mirror_job(self, job_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/mirrors/jobs/{job_id}", method="DELETE")

    def list_users(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/users")

    def create_user(
        self,
        username: str,
        display_name: str,
        quota_gib: int,
        role: str = "member",
        password: str | None = None,
        email: str = "",
        prepare_access: bool = True,
    ) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/users",
            method="POST",
            payload={
                "username": username,
                "display_name": display_name,
                "quota_gib": quota_gib,
                "role": role,
                "password": password,
                "email": email,
                "prepare_access": prepare_access,
            },
        )

    def prepare_user_access(
        self, user_id: str, *, email: str = "", ttl_seconds: int = 3600
    ) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/users/{user_id}/access-package",
            method="POST",
            payload={"email": email, "ttl_seconds": ttl_seconds},
        )

    def set_user_password(self, user_id: str, password: str) -> dict[str, Any]:
        return self._manager_request(
            f"/v1/admin/users/{user_id}/password",
            method="PUT",
            payload={"password": password},
        )

    def create_invitation(self, user_id: str, ttl_seconds: int = 900) -> dict[str, Any]:
        return self._manager_request(
            "/v1/admin/invitations",
            method="POST",
            payload={"user_id": user_id, "ttl_seconds": ttl_seconds},
        )

    def cancel_invitation(self, invitation_id: str) -> bool:
        result = self._manager_request(f"/v1/admin/invitations/{invitation_id}", method="DELETE")
        return bool(result.get("cancelled"))

    def list_devices(self) -> list[dict[str, Any]]:
        return self._manager_request("/v1/admin/devices")

    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 500))
        return self._manager_request(f"/v1/admin/audit?limit={safe_limit}")

    def approve_device(self, device_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/devices/{device_id}/approve", method="POST")

    def revoke_device(self, device_id: str) -> dict[str, Any]:
        return self._manager_request(f"/v1/admin/devices/{device_id}/revoke", method="POST")

    def shutdown(self) -> bool:
        result = self._manager_request("/v1/admin/shutdown", method="POST")
        return bool(result.get("accepted"))

    def _manager_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 2.0,
    ) -> Any:
        token = CoreSecrets.load_or_create(self.config).manager_token
        return self._request(
            path,
            method=method,
            payload=payload,
            timeout=timeout,
            token=token,
        )

    def _request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: dict[str, Any] | None = None,
        timeout: float = 2.0,
        token: str | None = None,
    ) -> Any:
        body = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
                return json.loads(content) if content else None
        except urllib.error.HTTPError as exc:
            try:
                payload_error = json.loads(exc.read().decode("utf-8"))
                detail = payload_error.get("detail", str(exc))
            except (ValueError, UnicodeDecodeError):
                detail = str(exc)
            raise CoreApiError(exc.code, str(detail)) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise CoreUnavailable("server core is not reachable") from exc


class CoreSupervisor:
    def __init__(self, client: CoreClient) -> None:
        self.client = client
        self.config = client.config

    def start(self, timeout_seconds: float = 30.0) -> dict[str, Any]:
        current = self.client.try_health()
        if current:
            return current
        self.config.ensure_directories()
        self.config.persist()
        if self._windows_service_installed():
            status = self._windows_service_status_code()
            if status == 3:  # STOP_PENDING: a previous graceful shutdown is still finishing.
                if not self._wait_for_windows_service_state(1, min(timeout_seconds, 30.0)):
                    raise CoreUnavailable(
                        "Служба Windows не завершила предыдущую остановку за отведённое время. "
                        f"Состояние службы: {self._windows_service_state()}"
                    )
                status = 1
            elif status == 2:  # START_PENDING: do not send a duplicate start command.
                deadline = time.monotonic() + min(timeout_seconds, 15.0)
                while time.monotonic() < deadline:
                    health = self.client.try_health(timeout=0.4)
                    if health:
                        return health
                    if self._windows_service_status_code() not in {2, 4}:
                        break
                    time.sleep(0.15)
                status = self._windows_service_status_code()
            if status == 4 and not self.client.try_health(timeout=1.0):
                # SCM can still report RUNNING after the API has disappeared during a
                # graceful restart. Stop it fully before attempting a new start.
                self._run_windows_service_command("stop", accepted_codes={0, 1062})
                if not self._wait_for_windows_service_state(1, min(timeout_seconds, 30.0)):
                    raise CoreUnavailable(
                        "Служба Windows работает без API и не смогла корректно остановиться. "
                        f"Состояние службы: {self._windows_service_state()}"
                    )
            try:
                completed = subprocess.run(
                    ["sc.exe", "start", "CloudStorageServerCore"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=False,
                    timeout=15,
                    check=False,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise CoreUnavailable(
                    f"Не удалось выполнить команду запуска службы Windows: {exc}. "
                    "Проверьте, что Manager запущен с правами администратора."
                ) from exc
            if completed.returncode not in {0, 1056}:
                details = self._decode_command_output(completed.stderr or completed.stdout).strip()
                reason = {
                    5: "Windows отказала в доступе — нужны права администратора",
                    1060: "служба CloudStorageServerCore не установлена",
                }.get(completed.returncode, "Service Control Manager отклонил запуск")
                raise CoreUnavailable(
                    f"Служба Windows не запущена: {reason} (код {completed.returncode})."
                    + (f" Ответ системы: {details}" if details else "")
                )
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                health = self.client.try_health(timeout=0.4)
                if health:
                    return health
                time.sleep(0.15)
            state = self._windows_service_state()
            raise CoreUnavailable(
                "Служба была запущена, но API ядра не ответил за "
                f"{timeout_seconds:g} с. Состояние службы: {state}. {self._log_tail()}"
            )
        command = self._core_command()
        environment = os.environ.copy()
        environment["CLOUD_STORAGE_CORE_DATA_DIR"] = str(self.config.data_directory)
        environment["CLOUD_STORAGE_CORE_HOST"] = self.config.host
        environment["CLOUD_STORAGE_CORE_PORT"] = str(self.config.port)
        environment["CLOUD_STORAGE_LAN_ENABLED"] = "1" if self.config.lan_enabled else "0"
        environment["CLOUD_STORAGE_LAN_HOST"] = self.config.lan_host
        environment["CLOUD_STORAGE_LAN_PORT"] = str(self.config.lan_port)
        environment["CLOUD_STORAGE_DISCOVERY_PORT"] = str(self.config.discovery_port)
        environment["CLOUD_STORAGE_REMOTE_ENABLED"] = "1" if self.config.remote_enabled else "0"
        environment["CLOUD_STORAGE_REMOTE_HOST"] = self.config.remote_host
        environment["CLOUD_STORAGE_REMOTE_PORT"] = str(self.config.remote_port)
        environment["CLOUD_STORAGE_REMOTE_PUBLIC_URL"] = self.config.remote_public_url
        environment["CLOUD_STORAGE_REMOTE_PAIRING_ENABLED"] = (
            "1" if self.config.remote_pairing_enabled else "0"
        )
        environment["CLOUD_STORAGE_ZROK_ENABLED"] = "1" if self.config.zrok_enabled else "0"
        environment["CLOUD_STORAGE_ZROK_HOST"] = self.config.zrok_host
        environment["CLOUD_STORAGE_ZROK_PORT"] = str(self.config.zrok_port)
        environment["CLOUD_STORAGE_ZROK_EXECUTABLE"] = self.config.zrok_executable
        environment["CLOUD_STORAGE_ZROK_SHARE_NAME"] = self.config.zrok_share_name
        environment["CLOUD_STORAGE_SERVER_NAME"] = self.config.server_name
        log_handle = self.config.log_path.open("ab")
        try:
            arguments: dict[str, Any] = {
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "cwd": str(self.config.data_directory),
                "env": environment,
                "close_fds": True,
            }
            if os.name == "nt":
                arguments["creationflags"] = (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NO_WINDOW
                )
            else:
                arguments["start_new_session"] = True
            try:
                process = subprocess.Popen(command, **arguments)
            except OSError as exc:
                raise CoreUnavailable(
                    f"Не удалось создать процесс Server Core ({command[0]}): {exc}"
                ) from exc
        finally:
            log_handle.close()
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise CoreUnavailable(
                    f"Server Core завершился во время запуска с кодом {process.returncode}. "
                    f"{self._log_tail()}"
                )
            health = self.client.try_health(timeout=0.4)
            if health:
                return health
            time.sleep(0.15)
        raise CoreUnavailable(
            f"Процесс Server Core работает, но API не ответил за {timeout_seconds:g} с. "
            f"Возможны занятый порт, ошибка конфигурации или недоступный каталог. {self._log_tail()}"
        )

    def _log_tail(self, maximum_lines: int = 8) -> str:
        try:
            lines = self.config.log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f"Журнал недоступен: {self.config.log_path}"
        tail = " | ".join(line.strip() for line in lines[-maximum_lines:] if line.strip())
        return f"Последние записи: {tail}" if tail else f"Журнал пуст: {self.config.log_path}"

    @staticmethod
    def _windows_service_state() -> str:
        try:
            result = subprocess.run(
                ["sc.exe", "query", "CloudStorageServerCore"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"не удалось проверить ({exc})"
        text = " ".join(CoreSupervisor._decode_command_output(result.stdout or result.stderr).split())
        return text[-500:] or f"код {result.returncode}"

    @staticmethod
    def _windows_service_status_code() -> int | None:
        """Return the language-independent numeric state reported by SCM."""
        try:
            result = subprocess.run(
                ["sc.exe", "query", "CloudStorageServerCore"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        output = CoreSupervisor._decode_command_output(result.stdout or result.stderr)
        match = re.search(
            r"(?im)^\s*(?:STATE|ESTADO|ÉTAT|STATO|STATUS)\s*:\s*([1-7])\b",
            output,
        )
        if match is None:
            match = re.search(
                r"(?im)^\s*[^:\r\n]+:\s*([1-7])\s+"
                r"(?:STOPPED|START_PENDING|STOP_PENDING|RUNNING|CONTINUE_PENDING|"
                r"PAUSE_PENDING|PAUSED)\b",
                output,
            )
        return int(match.group(1)) if match else None

    @staticmethod
    def _run_windows_service_command(
        command: str,
        *,
        accepted_codes: set[int],
    ) -> subprocess.CompletedProcess[bytes]:
        try:
            completed = subprocess.run(
                ["sc.exe", command, "CloudStorageServerCore"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                timeout=15,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CoreUnavailable(f"Не удалось выполнить sc.exe {command}: {exc}") from exc
        if completed.returncode not in accepted_codes:
            details = CoreSupervisor._decode_command_output(
                completed.stderr or completed.stdout
            ).strip()
            raise CoreUnavailable(
                f"Windows не выполнила команду службы {command} "
                f"(код {completed.returncode})." + (f" Ответ: {details}" if details else "")
            )
        return completed

    @classmethod
    def _wait_for_windows_service_state(cls, state: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if cls._windows_service_status_code() == state:
                return True
            time.sleep(0.15)
        return cls._windows_service_status_code() == state

    @staticmethod
    def _decode_command_output(payload: bytes | str | None) -> str:
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        encodings: list[str] = []
        if os.name == "nt":
            try:
                import ctypes

                encodings.append(f"cp{ctypes.windll.kernel32.GetOEMCP()}")
            except (AttributeError, OSError, ValueError):
                pass
        encodings.extend([locale.getpreferredencoding(False), "utf-8"])
        for encoding in encodings:
            try:
                return payload.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return payload.decode("utf-8", errors="replace")

    def stop(self, timeout_seconds: float = 30.0) -> bool:
        service_installed = self._windows_service_installed()
        health = self.client.try_health()
        if health:
            self.client.shutdown()
        elif not service_installed:
            return True

        if service_installed and not health:
            state = self._windows_service_status_code()
            if state == 1:
                return True
            if state in {2, 4, 5, 6, 7}:
                try:
                    self._run_windows_service_command("stop", accepted_codes={0, 1062})
                except CoreUnavailable:
                    return False

        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            api_stopped = not self.client.try_health(timeout=0.2)
            service_stopped = (
                not service_installed or self._windows_service_status_code() == 1
            )
            if api_stopped and service_stopped:
                return True
            time.sleep(0.15)
        return (
            not self.client.try_health(timeout=0.2)
            and (not service_installed or self._windows_service_status_code() == 1)
        )

    @staticmethod
    def _core_command() -> list[str]:
        if getattr(sys, "frozen", False):
            name = "CloudStorageServerCore.exe" if os.name == "nt" else "CloudStorageServerCore"
            current_directory = Path(sys.executable).resolve().parent
            candidates = [
                current_directory / name,
                current_directory.parent / "CloudStorageServerCore" / name,
            ]
            executable = next((item for item in candidates if item.exists()), None)
            if executable is None:
                raise CoreUnavailable("CloudStorageServerCore executable is missing")
            return [str(executable)]
        return [sys.executable, "-m", "cloud_storage.core"]

    @staticmethod
    def _windows_service_installed() -> bool:
        if os.name != "nt" or not getattr(sys, "frozen", False):
            return False
        try:
            result = subprocess.run(
                ["sc.exe", "query", "CloudStorageServerCore"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False
