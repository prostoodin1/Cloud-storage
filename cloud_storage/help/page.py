from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from cloud_storage.help.knowledge import KnowledgeArticle, KnowledgeBase
from cloud_storage.ui.widgets import make_header


class HelpPage(QWidget):
    """FAQ browser and private retrieval assistant shared by Manager and Client."""

    def __init__(
        self,
        knowledge: KnowledgeBase,
        audience: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        if audience not in {"server", "client"}:
            raise ValueError("audience must be 'server' or 'client'")
        self.knowledge = knowledge
        self.audience = audience
        self._build_ui()
        self._load_categories()
        self.refresh_articles()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 16, 18)
        layout.setSpacing(14)
        layout.addWidget(
            make_header(
                "Помощь и ответы",
                "Разберитесь, как работают функции, или задайте вопрос локальному помощнику.",
            )
        )

        privacy = QFrame()
        privacy.setProperty("accent", "blue")
        privacy_layout = QHBoxLayout(privacy)
        privacy_layout.setContentsMargins(14, 10, 14, 10)
        privacy_title = QLabel("ЛОКАЛЬНЫЙ ПОМОЩНИК")
        privacy_title.setStyleSheet("font-weight: 700; color: #4d9df8;")
        privacy_text = QLabel(
            "Ищет ответы только в базе этого приложения · без интернета · без передачи файлов и токенов"
        )
        privacy_text.setProperty("muted", True)
        privacy_text.setWordWrap(True)
        privacy_layout.addWidget(privacy_title)
        privacy_layout.addWidget(privacy_text, 1)
        layout.addWidget(privacy)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        browser_card = QFrame()
        browser_card.setProperty("card", True)
        browser_layout = QVBoxLayout(browser_card)
        browser_layout.setContentsMargins(16, 15, 16, 16)
        browser_layout.setSpacing(10)
        browser_title = QLabel("ВОПРОСЫ И ОТВЕТЫ")
        browser_title.setStyleSheet("font-weight: 700; font-size: 12px;")
        browser_layout.addWidget(browser_title)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Найти: код, личное хранилище, файлы…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.textChanged.connect(self.refresh_articles)
        browser_layout.addWidget(self.search_input)
        self.category_selector = QComboBox()
        self.category_selector.currentIndexChanged.connect(self.refresh_articles)
        browser_layout.addWidget(self.category_selector)
        self.article_list = QListWidget()
        self.article_list.setSpacing(3)
        self.article_list.setWordWrap(True)
        self.article_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.article_list.setStyleSheet("QListWidget::item { padding: 6px 4px; }")
        self.article_list.currentItemChanged.connect(self._show_selected_article)
        browser_layout.addWidget(self.article_list, 1)
        self.result_count = QLabel()
        self.result_count.setProperty("muted", True)
        browser_layout.addWidget(self.result_count)

        article_card = QFrame()
        article_card.setProperty("card", True)
        article_layout = QVBoxLayout(article_card)
        article_layout.setContentsMargins(22, 19, 22, 18)
        article_layout.setSpacing(8)
        self.article_category = QLabel("ВЫБЕРИТЕ ВОПРОС")
        self.article_category.setStyleSheet(
            "font-weight: 700; font-size: 11px; color: #4d9df8; letter-spacing: 1px;"
        )
        self.article_title = QLabel("Ответ появится здесь")
        self.article_title.setObjectName("SectionTitle")
        self.article_title.setWordWrap(True)
        self.article_answer = QTextBrowser()
        self.article_answer.setReadOnly(True)
        self.article_answer.setOpenExternalLinks(False)
        self.article_answer.setFrameShape(QFrame.Shape.NoFrame)
        self.article_answer.setStyleSheet(
            "QTextBrowser { background: transparent; color: #d9dde3; padding: 4px 0; }"
        )
        article_layout.addWidget(self.article_category)
        article_layout.addWidget(self.article_title)
        article_layout.addWidget(self.article_answer, 1)

        splitter.addWidget(browser_card)
        splitter.addWidget(article_card)
        splitter.setStretchFactor(0, 2)
        splitter.setStretchFactor(1, 3)
        splitter.setSizes([390, 610])
        layout.addWidget(splitter, 1)

        assistant = QFrame()
        assistant.setProperty("card", True)
        assistant_layout = QVBoxLayout(assistant)
        assistant_layout.setContentsMargins(16, 13, 16, 13)
        assistant_layout.setSpacing(8)
        assistant_header = QHBoxLayout()
        assistant_title = QLabel("СПРОСИТЬ ПОМОЩНИКА")
        assistant_title.setStyleSheet("font-weight: 700; font-size: 12px;")
        self.knowledge_status = QLabel()
        self.knowledge_status.setProperty("muted", True)
        self.knowledge_status.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        assistant_header.addWidget(assistant_title)
        assistant_header.addWidget(self.knowledge_status, 1)
        assistant_layout.addLayout(assistant_header)
        ask_row = QHBoxLayout()
        self.question_input = QLineEdit()
        self.question_input.setPlaceholderText("Например: как подключить человека по коду?")
        self.question_input.returnPressed.connect(self.ask_question)
        self.ask_button = QPushButton("Спросить")
        self.ask_button.setProperty("primary", True)
        self.ask_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.ask_button.clicked.connect(self.ask_question)
        ask_row.addWidget(self.question_input, 1)
        ask_row.addWidget(self.ask_button)
        assistant_layout.addLayout(ask_row)
        self.assistant_answer = QLabel("Введите вопрос — помощник покажет ответ и его источник.")
        self.assistant_answer.setWordWrap(True)
        self.assistant_answer.setProperty("muted", True)
        self.assistant_source = QLabel()
        self.assistant_source.setWordWrap(True)
        self.assistant_source.setStyleSheet("font-size: 12px; color: #4d9df8;")
        assistant_layout.addWidget(self.assistant_answer)
        assistant_layout.addWidget(self.assistant_source)
        layout.addWidget(assistant)

    def _load_categories(self) -> None:
        self.category_selector.blockSignals(True)
        self.category_selector.clear()
        self.category_selector.addItem("Все темы", "")
        for category in self.knowledge.categories(self.audience):
            self.category_selector.addItem(category, category)
        self.category_selector.blockSignals(False)

    def refresh_articles(self) -> None:
        selected_id = ""
        current = self.article_list.currentItem()
        if current is not None:
            article = current.data(Qt.ItemDataRole.UserRole)
            if isinstance(article, KnowledgeArticle):
                selected_id = article.id
        category = self.category_selector.currentData() or ""
        results = self.knowledge.search(
            self.search_input.text(),
            self.audience,
            category=category,
            limit=100,
        )
        self.article_list.blockSignals(True)
        self.article_list.clear()
        restore_row = 0
        for row, result in enumerate(results):
            item = QListWidgetItem(result.article.question)
            item.setData(Qt.ItemDataRole.UserRole, result.article)
            item.setToolTip(result.article.category)
            self.article_list.addItem(item)
            if result.article.id == selected_id:
                restore_row = row
        self.article_list.blockSignals(False)
        self.result_count.setText(f"Найдено ответов: {len(results)}")
        if results:
            self.article_list.setCurrentRow(restore_row)
            self._show_selected_article(self.article_list.currentItem())
        else:
            self.article_category.setText("НИЧЕГО НЕ НАЙДЕНО")
            self.article_title.setText("Попробуйте другой запрос")
            self.article_answer.setPlainText(
                "Можно задать вопрос локальному помощнику — непонятные вопросы сохраняются как пробелы базы знаний."
            )
        self._update_knowledge_status()

    def _show_selected_article(
        self,
        current: QListWidgetItem | None,
        _previous: QListWidgetItem | None = None,
    ) -> None:
        if current is None:
            return
        article = current.data(Qt.ItemDataRole.UserRole)
        if not isinstance(article, KnowledgeArticle):
            return
        self.article_category.setText(article.category.upper())
        self.article_title.setText(article.question)
        self.article_answer.setPlainText(article.answer)

    def ask_question(self) -> None:
        question = self.question_input.text().strip()
        if not question:
            self.question_input.setFocus()
            return
        answer = self.knowledge.ask(question, self.audience)
        self.assistant_answer.setText(answer.text)
        if answer.sources:
            confidence = round(answer.confidence * 100)
            source_names = " · ".join(item.question for item in answer.sources)
            self.assistant_source.setText(f"Источник: {source_names}  ·  совпадение {confidence}%")
            primary_id = answer.sources[0].id
            for row in range(self.article_list.count()):
                item = self.article_list.item(row)
                article = item.data(Qt.ItemDataRole.UserRole)
                if isinstance(article, KnowledgeArticle) and article.id == primary_id:
                    self.article_list.setCurrentRow(row)
                    break
        else:
            self.assistant_source.setText("Ответ не придуман: вопрос сохранён для пополнения базы.")
        self._update_knowledge_status()

    def _update_knowledge_status(self) -> None:
        unanswered = self.knowledge.unanswered_count()
        suffix = f" · вопросов без ответа: {unanswered}" if unanswered else ""
        self.knowledge_status.setText(
            f"SQLite-база · {len(self.knowledge.search('', self.audience, limit=100))} статей{suffix}"
        )
