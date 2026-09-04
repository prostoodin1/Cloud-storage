"""Render changed widgets with fixture data only; no installed service/profile."""
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from cloud_storage.models import AppSettings
from cloud_storage.ui.connection_page import ConnectionCodePanel
from cloud_storage.ui.pages import DisksPage
from cloud_storage.ui.theme import apply_theme


def main():
    app = QApplication.instance() or QApplication([])
    # Qt's offscreen platform does not enumerate Windows system fonts.
    if os.name == "nt":
        for name in ("segoeui.ttf", "segoeuib.ttf"):
            QFontDatabase.addApplicationFont(str(Path(os.environ["WINDIR"]) / "Fonts" / name))
    apply_theme(app)
    output = Path(sys.argv[1]).resolve()
    output.mkdir(parents=True, exist_ok=True)
    person = {"id": "qa", "display_name": "Тестовый пользователь", "username": "qa", "enabled": True}
    panel = ConnectionCodePanel()
    panel.set_data({"lan": {"enabled": True, "endpoints": ["https://192.168.1.2:8766"]}}, [person], {})
    panel.dynamic_target.setCurrentIndex(1)
    panel.resize(1060, 840)
    panel.show()
    app.processEvents()
    panel.grab().save(str(output / "connection-fixture.png"))
    page = DisksPage()
    personal = {"id": "a", "owner_user_id": "qa", "kind": "personal", "name": "Мои файлы",
                "quota_bytes": 3 * 1024**3, "members": [{"user_id": "qa", "display_name": person["display_name"]}]}
    shared = {"id": "b", "kind": "shared", "name": "Семья", "quota_bytes": 5 * 1024**3}
    archived = {"id": "c", "kind": "shared", "name": "Старое пространство", "archived_at": "2026-09-04", "quota_bytes": 1024**3}
    page.update_data([], AppSettings(), spaces=[personal, shared, archived])
    page._set_view("roles")
    page.resize(1060, 700)
    page.show()
    app.processEvents()
    page.grab().save(str(output / "spaces-fixture.png"))
    page.archive_toggle.setChecked(True)
    app.processEvents()
    page.grab().save(str(output / "archive-fixture.png"))
    panel.close()
    page.close()


if __name__ == "__main__":
    main()
