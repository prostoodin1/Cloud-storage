from __future__ import annotations

import os
import uuid

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication

from cloud_storage.single_instance import SingleInstance


def test_second_gui_instance_notifies_first_instead_of_starting() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    name = f"cloud-storage-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    second = SingleInstance(name)
    activated: list[bool] = []
    try:
        assert first.acquire() is True
        first.set_activation_handler(lambda: activated.append(True))
        assert second.acquire() is False
        for _ in range(10):
            app.processEvents()
            if activated:
                break
        assert activated == [True]
    finally:
        second.close()
        first.close()


def test_single_instance_endpoint_can_be_reacquired_after_close() -> None:
    QCoreApplication.instance() or QCoreApplication([])
    name = f"cloud-storage-test-{uuid.uuid4().hex}"
    first = SingleInstance(name)
    replacement = SingleInstance(name)
    assert first.acquire() is True
    first.close()
    try:
        assert replacement.acquire() is True
    finally:
        replacement.close()
