from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from cloud_storage.services.core_client import CoreSupervisor


def _supervisor(client: Mock | None = None) -> CoreSupervisor:
    core_client = client or Mock()
    core_client.config = SimpleNamespace(
        ensure_directories=Mock(),
        persist=Mock(),
    )
    return CoreSupervisor(core_client)


def test_windows_service_status_parser_is_independent_of_sc_language() -> None:
    result = subprocess.CompletedProcess(
        ["sc.exe", "query", "CloudStorageServerCore"],
        0,
        stdout=b"NOMBRE_DE_SERVICIO: CloudStorageServerCore\r\n        ESTADO : 3 STOP_PENDING\r\n",
        stderr=b"",
    )
    with patch("cloud_storage.services.core_client.subprocess.run", return_value=result):
        assert CoreSupervisor._windows_service_status_code() == 3


def test_stop_waits_for_api_and_windows_service_to_finish() -> None:
    client = Mock()
    client.config = SimpleNamespace()
    client.try_health.side_effect = [{"status": "ok"}, None, None]
    supervisor = CoreSupervisor(client)
    with (
        patch.object(supervisor, "_windows_service_installed", return_value=True),
        patch.object(supervisor, "_windows_service_status_code", side_effect=[3, 1]),
        patch("cloud_storage.services.core_client.time.sleep"),
    ):
        assert supervisor.stop(timeout_seconds=1) is True

    client.shutdown.assert_called_once_with()


def test_start_waits_out_previous_stop_pending_before_sc_start() -> None:
    client = Mock()
    client.config = SimpleNamespace(ensure_directories=Mock(), persist=Mock())
    health = {"status": "ok", "runtime": "go"}
    client.try_health.side_effect = [None, health]
    supervisor = CoreSupervisor(client)
    started = subprocess.CompletedProcess(["sc.exe", "start"], 0, stdout=b"", stderr=b"")
    with (
        patch.object(supervisor, "_windows_service_installed", return_value=True),
        patch.object(supervisor, "_windows_service_status_code", return_value=3),
        patch.object(supervisor, "_wait_for_windows_service_state", return_value=True),
        patch("cloud_storage.services.core_client.subprocess.run", return_value=started) as run,
    ):
        assert supervisor.start(timeout_seconds=1) == health

    assert run.call_args.args[0][:2] == ["sc.exe", "start"]
