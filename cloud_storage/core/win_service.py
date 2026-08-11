from __future__ import annotations

import sys

import servicemanager
import win32service
import win32serviceutil

from cloud_storage.core.config import CoreConfig
from cloud_storage.core.main import AlreadyRunningError, CoreServerGroup, PidGuard


class CloudStorageCoreService(win32serviceutil.ServiceFramework):
    _svc_name_ = "CloudStorageServerCore"
    _svc_display_name_ = "Cloud Storage Server Core"
    _svc_description_ = "Secure background service for the Cloud Storage home server."

    def __init__(self, args) -> None:
        super().__init__(args)
        self.servers: CoreServerGroup | None = None
        self.stop_requested = False

    def SvcStop(self) -> None:
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        self.stop_requested = True
        if self.servers is not None:
            self.servers.request_shutdown(delay=False)

    def SvcDoRun(self) -> None:
        servicemanager.LogInfoMsg("Cloud Storage Server Core is starting")
        config = CoreConfig.from_environment()
        try:
            with PidGuard(config.pid_path):
                while not self.stop_requested:
                    self.servers = CoreServerGroup(config)
                    self.servers.run()
                    if not self.servers.restart_requested:
                        break
        except AlreadyRunningError as exc:
            servicemanager.LogErrorMsg(f"Cloud Storage Server Core did not start: {exc}")
        servicemanager.LogInfoMsg("Cloud Storage Server Core has stopped")


def main() -> int:
    if "--smoke-test" in sys.argv:
        return 0
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(CloudStorageCoreService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(CloudStorageCoreService)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
