from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.ui.widgets import make_header


class ControlCenterPage(QWidget):
    refresh_requested = Signal()
    settings_save_requested = Signal(dict)
    preset_requested = Signal(str)
    template_requested = Signal(str)
    report_requested = Signal(dict)
    cell_create_requested = Signal(dict)
    cell_run_requested = Signal(str)
    docker_install_requested = Signal()
    ssh_enable_requested = Signal(dict)
    ssh_disable_requested = Signal()
    open_updates_requested = Signal()
    open_network_settings_requested = Signal()
    integration_test_requested = Signal(str)
    wake_on_lan_requested = Signal(str, str)

    def __init__(self) -> None:
        super().__init__()
        self._data: dict = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 16, 18)
        layout.setSpacing(14)
        header = make_header(
            "Система",
            "Idea 4: режимы, питание, сеть, Docker, SSH, автоматизации и контейнеры.",
        )
        layout.addWidget(header)
        top = QHBoxLayout()
        self.status = QLabel("Core выключен")
        self.status.setProperty("muted", True)
        refresh = QPushButton("Обновить")
        refresh.clicked.connect(self.refresh_requested)
        updates = QPushButton("Обновления")
        updates.clicked.connect(self.open_updates_requested)
        top.addWidget(self.status, 1)
        top.addWidget(updates)
        top.addWidget(refresh)
        layout.addLayout(top)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_profile_tab(), "Режим и профиль")
        self.tabs.addTab(self._build_power_tab(), "Питание")
        self.tabs.addTab(self._build_network_tab(), "Сеть и боты")
        self.tabs.addTab(self._build_automation_tab(), "Автоматизации")
        self.tabs.addTab(self._build_report_tab(), "Отчёты")
        self.tabs.addTab(self._build_host_tools_tab(), "Docker и SSH")
        self.tabs.addTab(self._build_cell_tab(), "Контейнеры")
        layout.addWidget(self.tabs, 1)

    @staticmethod
    def _scroll(widget: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        area.setWidget(widget)
        return area

    def _build_profile_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        profile_card = QFrame()
        profile_card.setProperty("card", True)
        form = QFormLayout(profile_card)
        self.profile = QComboBox()
        self.interface_mode = QComboBox()
        self.interface_mode.addItem("Простой — меньше пунктов меню", "simple")
        self.interface_mode.addItem("Подробный — все разделы отдельно", "detailed")
        self.security_mode = QComboBox()
        self.security_mode.addItem("Базовая защита", "basic")
        self.security_mode.addItem("Расширенная защита", "advanced")
        self.storage_strategy = QComboBox()
        self.storage_strategy.addItem("Сбалансированно", "balanced")
        self.storage_strategy.addItem("SSD-кэш → HDD", "staging")
        self.storage_strategy.addItem("Зеркало", "mirror")
        form.addRow("Готовый профиль", self.profile)
        form.addRow("Интерфейс", self.interface_mode)
        form.addRow("Безопасность", self.security_mode)
        form.addRow("Стратегия дисков", self.storage_strategy)
        apply_preset = QPushButton("Применить выбранный профиль")
        apply_preset.setProperty("primary", True)
        apply_preset.clicked.connect(self._emit_preset)
        save = QPushButton("Сохранить режимы")
        save.clicked.connect(self._emit_settings)
        form.addRow(apply_preset, save)
        layout.addWidget(profile_card)
        self.profile_description = QLabel()
        self.profile_description.setWordWrap(True)
        self.profile_description.setProperty("muted", True)
        layout.addWidget(self.profile_description)
        layout.addStretch()
        self.profile.currentIndexChanged.connect(self._update_profile_description)
        return self._scroll(content)

    def _build_power_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        card = QFrame()
        card.setProperty("card", True)
        form = QFormLayout(card)
        self.idle_sleep = QCheckBox("Разрешить переход в сон после простоя")
        self.allow_os_sleep = QCheckBox("Разрешить Core выполнить системную команду сна")
        self.allow_os_shutdown = QCheckBox(
            "Разрешить аварийное выключение через час без внешнего питания"
        )
        self.idle_minutes = QSpinBox()
        self.idle_minutes.setRange(5, 1440)
        self.idle_minutes.setSuffix(" мин")
        self.power_notify = QCheckBox("Сообщать о пропадании питания")
        self.power_threshold = QSpinBox()
        self.power_threshold.setRange(1, 1440)
        self.power_threshold.setSuffix(" мин осталось")
        form.addRow(self.idle_sleep)
        form.addRow(self.allow_os_sleep)
        form.addRow(self.allow_os_shutdown)
        form.addRow("Сон через", self.idle_minutes)
        form.addRow(self.power_notify)
        form.addRow("Повторная тревога", self.power_threshold)
        save = QPushButton("Сохранить питание")
        save.setProperty("primary", True)
        save.clicked.connect(self._emit_settings)
        form.addRow(save)
        layout.addWidget(card)
        self.power_status = QLabel("Данные питания недоступны")
        self.power_status.setWordWrap(True)
        self.power_status.setProperty("muted", True)
        layout.addWidget(self.power_status)
        note = QLabel(
            "Важно: пробуждение по сети выполняет сетевой адаптер, а не спящий Core. "
            "Wake-on-LAN нужно включить также в BIOS/UEFI и драйвере сетевой карты."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        layout.addStretch()
        return self._scroll(content)

    def _build_network_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        card = QFrame()
        card.setProperty("card", True)
        form = QFormLayout(card)
        self.local_address = QLineEdit()
        self.local_address.setReadOnly(True)
        self.browser_access = QComboBox()
        self.browser_access.addItem("Все вошедшие пользователи", "all")
        self.browser_access.addItem("Только доверенные устройства", "approved")
        self.browser_access.addItem("Никто через браузер", "nobody")
        form.addRow("Локальный адрес", self.local_address)
        copy_address = QPushButton("Копировать адрес")
        copy_address.clicked.connect(self._copy_local_address)
        form.addRow(copy_address)
        form.addRow("Доступ через zrok", self.browser_access)
        advanced_network = QPushButton("Адрес zrok, порты и локальный HTTPS")
        advanced_network.clicked.connect(self.open_network_settings_requested)
        form.addRow(advanced_network)
        self.wol_mac = QLineEdit()
        self.wol_mac.setPlaceholderText("AA:BB:CC:DD:EE:FF")
        self.wol_broadcast = QLineEdit("255.255.255.255")
        wake = QPushButton("Отправить Wake-on-LAN")
        wake.clicked.connect(
            lambda: self.wake_on_lan_requested.emit(
                self.wol_mac.text().strip(), self.wol_broadcast.text().strip()
            )
        )
        form.addRow("MAC сервера", self.wol_mac)
        form.addRow("Broadcast", self.wol_broadcast)
        form.addRow(wake)
        layout.addWidget(card)

        bot = QFrame()
        bot.setProperty("card", True)
        bot_form = QFormLayout(bot)
        self.telegram_enabled = QCheckBox("Telegram включён")
        self.telegram_chat = QLineEdit()
        self.telegram_token = QLineEdit()
        self.telegram_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.telegram_token.setPlaceholderText("оставьте пустым, чтобы не менять токен")
        self.email_enabled = QCheckBox("Email включён")
        self.email_host = QLineEdit()
        self.email_port = QSpinBox()
        self.email_port.setRange(1, 65535)
        self.email_user = QLineEdit()
        self.email_password = QLineEdit()
        self.email_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.email_password.setPlaceholderText("оставьте пустым, чтобы не менять пароль")
        self.email_sender = QLineEdit()
        self.email_recipient = QLineEdit()
        self.webhook_enabled = QCheckBox("HTTPS webhook включён")
        self.webhook_url = QLineEdit()
        bot_form.addRow(self.telegram_enabled)
        bot_form.addRow("Telegram chat ID", self.telegram_chat)
        bot_form.addRow("Telegram token", self.telegram_token)
        bot_form.addRow(self.email_enabled)
        bot_form.addRow("SMTP server", self.email_host)
        bot_form.addRow("SMTP port", self.email_port)
        bot_form.addRow("SMTP user", self.email_user)
        bot_form.addRow("SMTP password", self.email_password)
        bot_form.addRow("From", self.email_sender)
        bot_form.addRow("Кому по умолчанию", self.email_recipient)
        bot_form.addRow(self.webhook_enabled)
        bot_form.addRow("Webhook URL", self.webhook_url)
        tests = QHBoxLayout()
        for label, provider_id in (
            ("Проверить Telegram", "telegram"),
            ("Проверить Email", "email"),
            ("Проверить webhook", "webhook"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda checked=False, value=provider_id: self.integration_test_requested.emit(value)
            )
            tests.addWidget(button)
        bot_form.addRow("Тест после сохранения", tests)
        save = QPushButton("Сохранить сеть и ботов")
        save.setProperty("primary", True)
        save.clicked.connect(self._emit_settings)
        bot_form.addRow(save)
        layout.addWidget(bot)
        layout.addStretch()
        return self._scroll(content)

    def _build_automation_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        note = QLabel(
            "10 аварийных и 10 обычных шаблонов. Каждый собран из безопасных блоков "
            "«когда → действие → пауза» и не выполняет произвольный код на сервере."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        self.templates = QListWidget()
        self.templates.setWordWrap(True)
        layout.addWidget(self.templates, 1)
        install = QPushButton("Добавить выбранную автоматизацию")
        install.setProperty("primary", True)
        install.clicked.connect(self._emit_template)
        layout.addWidget(install)
        return content

    def _build_report_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        form = QFormLayout()
        self.report_name = QLineEdit("Ежедневный статус")
        self.report_interval = QSpinBox()
        self.report_interval.setRange(1, 8760)
        self.report_interval.setValue(24)
        self.report_interval.setSuffix(" ч")
        self.report_sections = QLineEdit(
            "system,disks,storage,connections,internet,diagnostics,power"
        )
        self.report_channels = QLineEdit("manager-inbox")
        form.addRow("Название", self.report_name)
        form.addRow("Период", self.report_interval)
        form.addRow("Разделы через запятую", self.report_sections)
        form.addRow("Каналы через запятую", self.report_channels)
        layout.addLayout(form)
        create = QPushButton("Создать расписание отчёта")
        create.setProperty("primary", True)
        create.clicked.connect(self._emit_report)
        layout.addWidget(create)
        self.report_list = QListWidget()
        layout.addWidget(self.report_list, 1)
        return content

    def _build_cell_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        self.cell_capabilities = QLabel()
        self.cell_capabilities.setWordWrap(True)
        self.cell_capabilities.setProperty("muted", True)
        layout.addWidget(self.cell_capabilities)
        explanation = QLabel(
            "Каждый скрипт, бот или обработчик запускается отдельно внутри контейнера. "
            "Он не получает прямой доступ к Windows и дискам сервера; сеть выключена, "
            "пока вы явно её не разрешите."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        form = QFormLayout()
        self.cell_name = QLineEdit("Тестовая ячейка")
        self.cell_image = QLineEdit("python:3.12-alpine")
        self.cell_command = QLineEdit("python,-c,print('Cloud Storage sandbox OK')")
        self.cell_cpu = QLineEdit("0.5")
        self.cell_memory = QSpinBox()
        self.cell_memory.setRange(64, 1048576)
        self.cell_memory.setValue(256)
        self.cell_storage = QSpinBox()
        self.cell_storage.setRange(64, 10240)
        self.cell_storage.setValue(512)
        self.cell_network = QCheckBox("Разрешить контейнеру выход в сеть")
        self.cell_network.setToolTip(
            "Нужно, например, Telegram-боту. Оставьте выключенным для локальных скриптов."
        )
        form.addRow("Название", self.cell_name)
        form.addRow("Container image", self.cell_image)
        form.addRow("Команда (аргументы через запятую)", self.cell_command)
        form.addRow("CPU", self.cell_cpu)
        form.addRow("RAM MiB", self.cell_memory)
        form.addRow("Рабочее место MiB", self.cell_storage)
        form.addRow("Сеть", self.cell_network)
        layout.addLayout(form)
        row = QHBoxLayout()
        create = QPushButton("Создать ячейку")
        create.setProperty("primary", True)
        create.clicked.connect(self._emit_cell)
        run = QPushButton("Запустить выбранную")
        run.clicked.connect(self._emit_cell_run)
        row.addWidget(create)
        row.addWidget(run)
        layout.addLayout(row)
        self.cell_list = QListWidget()
        layout.addWidget(self.cell_list, 1)
        return self._scroll(content)

    def _build_host_tools_tab(self) -> QWidget:
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(16, 16, 16, 24)
        layout.setSpacing(14)

        docker_card = QFrame()
        docker_card.setProperty("card", True)
        docker_layout = QVBoxLayout(docker_card)
        docker_title = QLabel("Docker Desktop для контейнеров")
        docker_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        docker_text = QLabel(
            "Docker нужен, чтобы ячейки для скриптов и ботов действительно запускались "
            "изолированно. Установка выполняется в фоне с официального сайта Docker."
        )
        docker_text.setWordWrap(True)
        self.docker_status = QLabel("Проверяем Docker…")
        self.docker_status.setWordWrap(True)
        self.docker_status.setProperty("muted", True)
        docker_docs = QLabel(
            '<a href="https://docs.docker.com/desktop/setup/install/windows-install/">'
            "Условия и официальная инструкция Docker Desktop</a>"
        )
        docker_docs.setOpenExternalLinks(True)
        install_docker = QPushButton("Скачать и установить Docker Desktop")
        install_docker.setProperty("primary", True)
        install_docker.clicked.connect(self.docker_install_requested)
        self.install_docker_button = install_docker
        docker_layout.addWidget(docker_title)
        docker_layout.addWidget(docker_text)
        docker_layout.addWidget(self.docker_status)
        docker_layout.addWidget(docker_docs)
        docker_layout.addWidget(install_docker)
        layout.addWidget(docker_card)

        ssh_card = QFrame()
        ssh_card.setProperty("card", True)
        ssh_layout = QVBoxLayout(ssh_card)
        ssh_title = QLabel("SSH-подключение к серверу")
        ssh_title.setStyleSheet("font-size: 16px; font-weight: 700;")
        ssh_text = QLabel(
            "Менеджер установит системный OpenSSH Server, разрешит вход только выбранному "
            "локальному администратору Windows и только по публичному ключу. Парольный вход "
            "будет выключен, а порт открыт лишь для профиля «Частная сеть»."
        )
        ssh_text.setWordWrap(True)
        self.ssh_status = QLabel("Проверяем SSH…")
        self.ssh_status.setWordWrap(True)
        self.ssh_status.setProperty("muted", True)
        ssh_form = QFormLayout()
        self.ssh_username = QLineEdit(os.environ.get("USERNAME", ""))
        self.ssh_username.setPlaceholderText("локальный администратор Windows")
        self.ssh_port = QSpinBox()
        self.ssh_port.setRange(1, 65535)
        self.ssh_port.setValue(22)
        self.ssh_public_key = QPlainTextEdit()
        self.ssh_public_key.setPlaceholderText("ssh-ed25519 AAAA… имя-устройства")
        self.ssh_public_key.setMaximumHeight(92)
        ssh_form.addRow("Windows-логин", self.ssh_username)
        ssh_form.addRow("TCP-порт", self.ssh_port)
        ssh_form.addRow("Публичный SSH-ключ", self.ssh_public_key)
        ssh_help = QLabel(
            "На компьютере, с которого будете входить: ssh-keygen -t ed25519. "
            "Сюда вставьте содержимое файла id_ed25519.pub; закрытый ключ никому не отправляйте."
        )
        ssh_help.setWordWrap(True)
        ssh_help.setProperty("muted", True)
        ssh_actions = QHBoxLayout()
        enable_ssh = QPushButton("Установить и включить SSH")
        enable_ssh.setProperty("primary", True)
        enable_ssh.clicked.connect(self._emit_ssh_enable)
        disable_ssh = QPushButton("Отключить SSH")
        disable_ssh.clicked.connect(self.ssh_disable_requested)
        ssh_actions.addWidget(enable_ssh)
        ssh_actions.addWidget(disable_ssh)
        ssh_layout.addWidget(ssh_title)
        ssh_layout.addWidget(ssh_text)
        ssh_layout.addWidget(self.ssh_status)
        ssh_layout.addLayout(ssh_form)
        ssh_layout.addWidget(ssh_help)
        ssh_layout.addLayout(ssh_actions)
        layout.addWidget(ssh_card)
        layout.addStretch()
        return self._scroll(content)

    def update_data(self, data: dict, *, online: bool) -> None:
        self._data = data or {}
        self.status.setText("Core работает · Idea 4 активна" if online else "Core выключен")
        self.setEnabled(online)
        if not online:
            return
        settings = data.get("settings", {})
        self.profile.blockSignals(True)
        self.profile.clear()
        for preset in data.get("presets", []):
            self.profile.addItem(str(preset.get("name", preset["id"])), preset["id"])
            self.profile.setItemData(self.profile.count() - 1, preset, Qt.ItemDataRole.UserRole + 1)
        self._select(self.profile, settings.get("profile"))
        self.profile.blockSignals(False)
        self._select(self.interface_mode, settings.get("interface_mode"))
        self._select(self.security_mode, settings.get("security", {}).get("mode"))
        self._select(self.storage_strategy, settings.get("storage_strategy"))
        self._select(self.browser_access, settings.get("browser_access"))
        power = settings.get("power", {})
        self.idle_sleep.setChecked(bool(power.get("idle_sleep_enabled")))
        self.allow_os_sleep.setChecked(bool(power.get("allow_os_sleep")))
        self.allow_os_shutdown.setChecked(bool(power.get("allow_os_shutdown")))
        self.idle_minutes.setValue(int(power.get("idle_minutes", 20)))
        self.power_notify.setChecked(bool(power.get("notify_on_outage", True)))
        self.power_threshold.setValue(int(power.get("notify_below_minutes", 20)))
        power_status = data.get("power", {})
        self.power_status.setText(
            f"Источник: {power_status.get('source', 'unknown')} · "
            f"заряд: {power_status.get('percent', '—')}% · "
            f"осталось: {power_status.get('minutes_left', '—')} мин · "
            f"простой: {power_status.get('idle_seconds', 0)} с"
        )
        network = data.get("network", {})
        self.local_address.setText(str(network.get("local_address", "")))
        integrations = settings.get("integrations", {})
        telegram = integrations.get("telegram", {})
        self.telegram_enabled.setChecked(bool(telegram.get("enabled")))
        self.telegram_chat.setText(str(telegram.get("chat_id", "")))
        email = integrations.get("email", {})
        self.email_enabled.setChecked(bool(email.get("enabled")))
        self.email_host.setText(str(email.get("host", "")))
        self.email_port.setValue(int(email.get("port", 587)))
        self.email_user.setText(str(email.get("username", "")))
        self.email_sender.setText(str(email.get("sender", "")))
        self.email_recipient.setText(str(email.get("recipient", "")))
        webhook = integrations.get("webhook", {})
        self.webhook_enabled.setChecked(bool(webhook.get("enabled")))
        self.webhook_url.setText(str(webhook.get("url", "")))
        self._render_templates(data.get("automation_templates", []))
        self._render_reports(data.get("reports", {}).get("schedules", []))
        self._render_cells(data.get("cells", []), data.get("sandbox", {}))
        self._render_host_tools(data.get("host_tools", {}))
        self._update_profile_description()

    @staticmethod
    def _select(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _update_profile_description(self) -> None:
        preset = self.profile.itemData(self.profile.currentIndex(), Qt.ItemDataRole.UserRole + 1)
        self.profile_description.setText(
            str(preset.get("description", "")) if isinstance(preset, dict) else ""
        )

    def _render_templates(self, templates: list[dict]) -> None:
        self.templates.clear()
        for template in templates:
            prefix = "АВАРИЯ" if template.get("category") == "emergency" else "ОБЫЧНАЯ"
            item = QListWidgetItem(
                f"[{prefix}] {template.get('name')}\n{template.get('description')}"
            )
            item.setData(Qt.ItemDataRole.UserRole, template.get("id"))
            self.templates.addItem(item)

    def _render_reports(self, reports: list[dict]) -> None:
        self.report_list.clear()
        for report in reports:
            self.report_list.addItem(
                f"{report.get('name')} · каждые {report.get('interval_hours')} ч · "
                f"следующий: {report.get('next_run_at') or 'выключен'}"
            )

    def _render_cells(self, cells: list[dict], capabilities: dict) -> None:
        runtime = capabilities.get("runtime") or "не найден"
        maximum = capabilities.get("automatic_max", {})
        self.cell_capabilities.setText(
            f"Runtime: {runtime}. Хост-команды запрещены. Автолимит: "
            f"CPU {maximum.get('cpu', '—')}, RAM {maximum.get('memory_mib', '—')} MiB, "
            f"параллельно {maximum.get('parallel_cells', '—')}."
        )
        self.cell_list.clear()
        for cell in cells:
            item = QListWidgetItem(
                f"{cell.get('name')} · {cell.get('image')} · {cell.get('status')}\n"
                f"{str(cell.get('last_result', ''))[-240:]}"
            )
            item.setData(Qt.ItemDataRole.UserRole, cell.get("id"))
            self.cell_list.addItem(item)

    def _render_host_tools(self, tools: dict) -> None:
        docker = tools.get("docker", {})
        docker_job = docker.get("job", {})
        if docker.get("engine_ready"):
            docker_text = f"Docker работает · версия {docker.get('version') or 'определяется'}"
        elif docker.get("installed"):
            docker_text = "Docker установлен, но движок ещё не запущен. Откройте Docker Desktop."
        elif not docker.get("supported", True):
            docker_text = "Автоустановка Docker Desktop недоступна на этой системе."
        else:
            docker_text = "Docker пока не установлен. Контейнеры запускаться не будут."
        if docker_job.get("status") not in {None, "", "idle"}:
            docker_text += (
                f"\nУстановка: {docker_job.get('status')} · {docker_job.get('message', '')}"
            )
        self.docker_status.setText(docker_text)
        self.install_docker_button.setEnabled(
            bool(docker.get("supported", True))
            and not bool(docker.get("installed"))
            and docker_job.get("status") not in {"queued", "running"}
        )

        ssh = tools.get("ssh", {})
        ssh_job = ssh.get("job", {})
        state = "работает" if ssh.get("running") else "выключен"
        ssh_text = (
            f"OpenSSH: {state} · порт {ssh.get('port', 22)} · "
            f"вход по ключу: {'да' if ssh.get('key_only') else 'нет'}"
        )
        if ssh.get("username"):
            ssh_text += f" · пользователь {ssh.get('username')}"
        addresses = ssh.get("addresses", [])
        if ssh.get("running") and addresses and ssh.get("username"):
            host = str(addresses[0]).rsplit(":", 1)[0]
            ssh_text += (
                f"\nКоманда подключения: ssh -p {ssh.get('port', 22)} {ssh.get('username')}@{host}"
            )
        if ssh_job.get("status") not in {None, "", "idle"}:
            ssh_text += f"\nНастройка: {ssh_job.get('status')} · {ssh_job.get('message', '')}"
        self.ssh_status.setText(ssh_text)
        if ssh.get("port"):
            self.ssh_port.setValue(int(ssh.get("port", 22)))
        if ssh.get("username") and not self.ssh_username.text().strip():
            self.ssh_username.setText(str(ssh.get("username")))

    def _emit_settings(self) -> None:
        secrets = {}
        if self.telegram_token.text():
            secrets["telegram_bot_token"] = self.telegram_token.text()
        if self.email_password.text():
            secrets["smtp_password"] = self.email_password.text()
        self.settings_save_requested.emit(
            {
                "profile": self.profile.currentData(),
                "interface_mode": self.interface_mode.currentData(),
                "storage_strategy": self.storage_strategy.currentData(),
                "browser_access": self.browser_access.currentData(),
                "security": {"mode": self.security_mode.currentData()},
                "power": {
                    "idle_sleep_enabled": self.idle_sleep.isChecked(),
                    "allow_os_sleep": self.allow_os_sleep.isChecked(),
                    "allow_os_shutdown": self.allow_os_shutdown.isChecked(),
                    "idle_minutes": self.idle_minutes.value(),
                    "notify_on_outage": self.power_notify.isChecked(),
                    "notify_below_minutes": self.power_threshold.value(),
                },
                "integrations": {
                    "telegram": {
                        "enabled": self.telegram_enabled.isChecked(),
                        "chat_id": self.telegram_chat.text().strip(),
                    },
                    "email": {
                        "enabled": self.email_enabled.isChecked(),
                        "host": self.email_host.text().strip(),
                        "port": self.email_port.value(),
                        "username": self.email_user.text().strip(),
                        "sender": self.email_sender.text().strip(),
                        "recipient": self.email_recipient.text().strip(),
                        "starttls": True,
                    },
                    "webhook": {
                        "enabled": self.webhook_enabled.isChecked(),
                        "url": self.webhook_url.text().strip(),
                    },
                },
                "secrets": secrets,
            }
        )

    def _copy_local_address(self) -> None:
        self.local_address.selectAll()
        self.local_address.copy()
        self.local_address.deselect()

    def _emit_preset(self) -> None:
        if self.profile.currentData():
            self.preset_requested.emit(str(self.profile.currentData()))

    def _emit_template(self) -> None:
        current = self.templates.currentItem()
        if current is not None:
            self.template_requested.emit(str(current.data(Qt.ItemDataRole.UserRole)))

    def _emit_report(self) -> None:
        sections = [item.strip() for item in self.report_sections.text().split(",") if item.strip()]
        channels = [item.strip() for item in self.report_channels.text().split(",") if item.strip()]
        self.report_requested.emit(
            {
                "name": self.report_name.text().strip(),
                "enabled": True,
                "interval_hours": self.report_interval.value(),
                "sections": sections,
                "delivery_channels": channels,
            }
        )

    def _emit_cell(self) -> None:
        try:
            cpu = float(self.cell_cpu.text())
        except ValueError:
            cpu = 0.0
        self.cell_create_requested.emit(
            {
                "name": self.cell_name.text().strip(),
                "image": self.cell_image.text().strip(),
                "command": [item.strip() for item in self.cell_command.text().split(",")],
                "cpu_limit": cpu,
                "memory_mib": self.cell_memory.value(),
                "storage_mib": self.cell_storage.value(),
                "timeout_seconds": 300,
                "network_enabled": self.cell_network.isChecked(),
            }
        )

    def _emit_cell_run(self) -> None:
        current = self.cell_list.currentItem()
        if current is not None:
            self.cell_run_requested.emit(str(current.data(Qt.ItemDataRole.UserRole)))

    def _emit_ssh_enable(self) -> None:
        self.ssh_enable_requested.emit(
            {
                "username": self.ssh_username.text().strip(),
                "public_key": self.ssh_public_key.toPlainText().strip(),
                "port": self.ssh_port.value(),
            }
        )
