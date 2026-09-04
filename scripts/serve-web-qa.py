"""Temporary, isolated browser QA server. Stops automatically; never uses production data."""
import json
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloud_storage.client.api_client import ClientApi
from cloud_storage.core.config import CoreConfig
from cloud_storage.core.main import CoreServerGroup


def port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    folder = Path(tempfile.mkdtemp(prefix="cloud-storage-web-qa-"))
    config = CoreConfig(data_directory=folder, port=port(), lan_enabled=True, lan_port=port(), discovery_port=port(), server_name="Cloud Storage — проверка 0.9.21")
    group = CoreServerGroup(config)
    with group.application.state.runtime.database.transaction() as connection:
        connection.execute("UPDATE storage_roots SET min_free_bytes=0, max_fill_percent=99")
    worker = threading.Thread(target=group.run, daemon=True)
    worker.start()
    api = ClientApi(f"http://127.0.0.1:{config.port}", token=group.application.state.runtime.secrets.manager_token)
    try:
        for _ in range(100):
            try:
                api.health()
                break
            except Exception:
                time.sleep(.1)
        print(json.dumps({"url": f"http://127.0.0.1:{config.port}", "code": api._json_request("/v1/admin/dynamic-pairing-code")["code"], "data": str(folder)}, ensure_ascii=False), flush=True)
        time.sleep(900)
    finally:
        group.request_shutdown(delay=False)
        worker.join(timeout=15)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
