from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QMenu, QPushButton


class NavigationCapsule(QPushButton):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("NavigationCapsule")
        self.setAccessibleName("Разделы приложения")
        self.setMinimumHeight(44)
        self.navigation_menu = QMenu(self)
        self.navigation_menu.setObjectName("CapsuleMenu")
        self.setStyleSheet("""
            QPushButton#NavigationCapsule { color: #f2cf8b;
                background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #29323e,stop:1 #191e26);
                border: 1px solid #465261; border-radius: 22px; padding: 9px 25px; }
            QPushButton#NavigationCapsule:hover, QPushButton#NavigationCapsule:focus {
                background: #2a3340; border-color: #f2cf8b; }
            QMenu#CapsuleMenu { background: #171d25; border: 1px solid #465261;
                border-radius: 14px; padding: 6px; }
            QMenu#CapsuleMenu::item { padding: 8px 24px; border-radius: 7px; }
            QMenu#CapsuleMenu::item:selected { background: #f2cf8b; color: #2d2415; }
        """)
        self.clicked.connect(self.open_sections)

    def set_current_section(self, title: str) -> None:
        self.setText(f"{title}  ▴")

    def open_sections(self) -> None:
        size = self.navigation_menu.sizeHint()
        anchor = self.mapToGlobal(QPoint((self.width() - size.width()) // 2, -size.height() - 8))
        self.navigation_menu.popup(anchor)
        current = next(
            (action for action in self.navigation_menu.actions()
             if action.isChecked() and action.isVisible() and action.isEnabled()),
            None,
        )
        if current is not None:
            self.navigation_menu.setActiveAction(current)
