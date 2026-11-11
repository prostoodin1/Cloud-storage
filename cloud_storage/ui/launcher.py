from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)


class ManagerLauncher(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("ManagerLauncher")
        self.setStyleSheet("""
            QWidget#ManagerLauncher { background: #0b0e12; }
            QFrame#LaunchCard { background: #151a21; border: 1px solid #39424d;
                border-radius: 20px; }
            QLabel#LaunchTitle { color: #f0f6ff; font-size: 34px; font-weight: 700; }
            QLabel#LaunchDetail { color: #bbcddd; font-size: 14px; }
            QLabel#LaunchStage { color: #f2cf8b; }
            QProgressBar { border: 0; background: #2d3c4e; border-radius: 3px; }
            QProgressBar::chunk { background: #f2cf8b; border-radius: 3px; }
        """)

        loading_body = self._panel()
        loading_body.addWidget(self._label("Cloud Storage", "LaunchTitle"))
        self.stage = self._label("Проверяем ответ сервера…", "LaunchStage")
        loading_body.addWidget(self.stage)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(6)
        loading_body.addWidget(self.progress)

    @staticmethod
    def _label(text: str, name: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName(name)
        label.setWordWrap(True)
        return label

    def _panel(self) -> QVBoxLayout:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 24, 24, 24)
        panel = QFrame()
        panel.setObjectName("LaunchCard")
        panel.setMaximumWidth(700)
        body = QVBoxLayout(panel)
        body.setContentsMargins(32, 32, 32, 32)
        body.setSpacing(18)
        outer.addStretch()
        outer.addWidget(panel, alignment=Qt.AlignmentFlag.AlignHCenter)
        outer.addStretch()
        return body
