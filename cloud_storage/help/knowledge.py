from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path


@dataclass(frozen=True, slots=True)
class KnowledgeArticle:
    id: str
    category: str
    question: str
    answer: str
    keywords: str
    audience: str = "both"


@dataclass(frozen=True, slots=True)
class SearchResult:
    article: KnowledgeArticle
    score: float


@dataclass(frozen=True, slots=True)
class AssistantAnswer:
    text: str
    sources: tuple[KnowledgeArticle, ...]
    confidence: float


ARTICLES = (
    KnowledgeArticle(
        "core-purpose",
        "Основы",
        "Что такое Server Core и зачем он нужен?",
        "Server Core — отдельный фоновый процесс, который владеет базой, пользователями, "
        "устройствами и файлами. Server Manager только управляет им. Поэтому закрытие окна "
        "Manager не останавливает облако.",
        "ядро core сервер служба фон закрыть manager работает",
        "server",
    ),
    KnowledgeArticle(
        "manager-client-difference",
        "Основы",
        "Чем Server Manager отличается от Desktop Client?",
        "Server Manager предназначен администратору: диски, пользователи, устройства и служба. "
        "Desktop Client предназначен человеку, который видит только разрешённые логические "
        "пространства и свои файлы, но не физические диски сервера.",
        "разница manager client клиент сервер администратор пользователь",
    ),
    KnowledgeArticle(
        "personal-storage",
        "Основы",
        "Как работает личное хранилище пользователя?",
        "При создании пользователя сервер одной транзакцией создаёт пространство «Мои файлы» и "
        "назначает квоту. Пользователь видит логические папки, а Core сам выбирает подходящий "
        "физический диск. Другой пользователь не получает доступ без отдельного разрешения.",
        "личное хранилище мои файлы квота пространство пользователь диск",
    ),
    KnowledgeArticle(
        "create-user",
        "Подключение",
        "Как добавить нового человека?",
        "Запустите Core, откройте «Настройки → Пользователи» в Server Manager, нажмите «Добавить "
        "пользователя», задайте логин, имя и квоту. После создания Manager сразу предложит "
        "одноразовый код и QR.",
        "создать добавить человек пользователь логин квота приглашение",
        "server",
    ),
    KnowledgeArticle(
        "pairing-code",
        "Подключение",
        "Как подключить компьютер по коду?",
        "Администратор создаёт одноразовый код в разделе пользователей. В Desktop Client человек "
        "вводит адрес сервера, код, пароль и название устройства. После этого администратор "
        "подтверждает запрос в разделе «Подключение устройств».",
        "код подключение pairing приглашение компьютер устройство пароль",
    ),
    KnowledgeArticle(
        "pairing-code-expiry",
        "Подключение",
        "Почему код подключения не работает?",
        "Код действует 15 минут, используется только один раз и может быть отменён администратором. "
        "Создайте новый код. Также проверьте адрес сервера и убедитесь, что Server Core запущен.",
        "код не работает истёк просрочен использован отменён новый 15 минут",
    ),
    KnowledgeArticle(
        "pending-device",
        "Подключение",
        "Почему Client пишет «Ожидается подтверждение»?",
        "Это нормальная защита. Код уже принят, но токен устройства пока заблокирован. "
        "Администратор должен открыть Server Manager → Настройки → Подключение устройств и "
        "нажать «Подтвердить».",
        "ожидает подтверждение pending заблокирован администратор устройство",
    ),
    KnowledgeArticle(
        "server-address",
        "Подключение",
        "Какой адрес сервера вводить в Client?",
        "Если Client работает на серверном компьютере, используйте http://127.0.0.1:8765. "
        "Для другого компьютера администратор включает LAN-доступ в расширенных настройках. "
        "Затем нажмите «Найти в сети» или вставьте полное приглашение: HTTPS-адрес и закреплённый "
        "TLS-отпечаток заполнятся автоматически.",
        "адрес url ip 127 localhost порт 8765 другой компьютер сеть",
        "client",
    ),
    KnowledgeArticle(
        "auto-reconnect",
        "Подключение",
        "Нужно ли вводить код после каждого запуска?",
        "Нет. После первого сопряжения Client сохраняет отдельный токен устройства и проверяет "
        "соединение автоматически. Новый код потребуется после локального удаления токена или "
        "отзыва устройства администратором.",
        "автоматическое переподключение запуск снова код токен",
        "client",
    ),
    KnowledgeArticle(
        "token-storage",
        "Безопасность",
        "Где хранится токен устройства?",
        "В Windows токен шифруется DPAPI и не записывается в обычный JSON-профиль. В Linux он "
        "хранится отдельным файлом с правами 0600 до интеграции с Secret Service. У каждого "
        "устройства свой токен, который администратор может немедленно отозвать.",
        "токен ключ dpapi шифрование где хранится linux secret service",
    ),
    KnowledgeArticle(
        "password-storage",
        "Безопасность",
        "Хранит ли сервер пароль пользователя?",
        "Открытый пароль не сохраняется. Server Core хранит только Argon2id-хеш с солью. Коды "
        "приглашений и токены устройств также хранятся не в открытом виде, а как HMAC-отпечатки.",
        "пароль хранение argon2 хеш соль hmac безопасность",
    ),
    KnowledgeArticle(
        "uploaded-execution",
        "Безопасность",
        "Может ли загруженный файл запуститься на сервере?",
        "Core никогда не запускает загруженные файлы и не передаёт их командной оболочке. Они "
        "получают случайные физические имена .blob, хранятся вне каталогов программ, лишаются "
        "Unix-битов исполнения и переводятся в режим только чтения.",
        "запустить файл exe вирус sudo исполнение права blob read only",
    ),
    KnowledgeArticle(
        "tls-state",
        "Безопасность",
        "Можно ли подключиться к серверу по локальной сети?",
        "Да. В Server Manager включите расширенный режим, откройте «Сеть», включите защищённое "
        "подключение и сохраните настройки. Core оставит административный API на 127.0.0.1, а для "
        "устройств откроет отдельный HTTPS-порт. В Client нажмите «Найти в сети» и обязательно "
        "сверьте отпечаток либо используйте полное приглашение администратора.",
        "локальная сеть lan wifi другой компьютер tls https сертификат",
    ),
    KnowledgeArticle(
        "internet-access",
        "Безопасность",
        "Доступен ли сервер из интернета?",
        "Да, но только после явного включения в Server Manager → расширенный режим → Удалённый доступ. "
        "Можно выбрать прямой HTTPS-порт/VPN либо zrok без перенаправления порта. В обоих случаях доступен "
        "только белый список клиентских маршрутов, а административный API скрыт. Для нового устройства "
        "отдельно и временно разрешите подключение по коду, а затем снова выключите его.",
        "интернет удалённый доступ порт роутер zrok туннель outside",
    ),
    KnowledgeArticle(
        "zrok-login",
        "Безопасность",
        "Как подключиться через zrok и зачем снова вводить логин?",
        "Администратор устанавливает и один раз включает zrok, затем активирует его в разделе «Удалённый "
        "доступ». Core публикует только локальный клиентский шлюз и показывает HTTPS-адрес. В Desktop Client "
        "добавьте этот адрес, подключите устройство по одноразовому коду, дождитесь подтверждения и нажмите "
        "«Войти через интернет». Сервер проверит логин, Argon2id-пароль и токен устройства. Пароль не "
        "сохраняется и не передаётся zrok; защищённая интернет-сессия действует ограниченное время.",
        "zrok интернет логин пароль сессия туннель общий интернет защита",
    ),
    KnowledgeArticle(
        "multiple-servers",
        "Подключение",
        "Можно ли подключить Desktop Client к нескольким серверам?",
        "Да. На странице подключения выберите «Добавить сервер». У каждого сервера хранится отдельный профиль, "
        "TLS-отпечаток, состояние устройства и отдельный токен. Переключение не смешивает пространства и "
        "продолжает только очередь выбранного сервера. При удалении профиля файлы на сервере и локальные "
        "офлайн-копии не удаляются, а незавершённые передачи этого сервера ставятся на паузу.",
        "несколько серверов профиль добавить переключить токен очередь multi server",
        "client",
    ),
    KnowledgeArticle(
        "file-placement",
        "Файлы",
        "Как сервер выбирает физический диск для файла?",
        "Core проверяет активные управляемые корни, свободное место, минимальный резерв, предел "
        "заполнения и приоритет. Затем выбирает один подходящий диск. Один файл всегда хранится "
        "целиком на одном диске и не делится на части.",
        "куда сохраняется файл выбор диск приоритет резерв заполнение целиком",
    ),
    KnowledgeArticle(
        "atomic-upload",
        "Файлы",
        "Что будет, если питание пропадёт во время загрузки?",
        "Загрузка сначала пишется во временный файл .part. Только после проверки размера, квоты и "
        "SHA-256 Core атомарно фиксирует объект и метаданные. Незавершённая загрузка не заменяет "
        "последнюю целую версию файла.",
        "питание сбой загрузка атомарно part sha256 повреждение",
    ),
    KnowledgeArticle(
        "file-delete",
        "Файлы",
        "Что происходит при удалении файла?",
        "Удаление мягкое: текущий объект атомарно переносится в серверный каталог .trash, запись "
        "помечается удалённой, а событие попадает в аудит. Функция восстановления интерфейса ещё "
        "будет добавлена.",
        "удалить удаление корзина trash восстановить аудит",
    ),
    KnowledgeArticle(
        "file-versions",
        "Файлы",
        "Сохраняются ли предыдущие версии файла?",
        "Да. При загрузке нового содержимого по тому же логическому пути предыдущий объект "
        "записывается в таблицу версий. Интерфейс просмотра и восстановления версий ещё не "
        "выведен в Client.",
        "версия предыдущая заменить восстановить история файла",
    ),
    KnowledgeArticle(
        "quota",
        "Файлы",
        "Что произойдёт при превышении квоты?",
        "Core проверяет квоту до передачи и повторно внутри транзакции перед фиксацией. Если места "
        "недостаточно, новая версия не сохраняется, а временный файл удаляется. Другие файлы "
        "пользователя не затрагиваются.",
        "квота закончилась место превышение недостаточно 507",
    ),
    KnowledgeArticle(
        "offline-files",
        "Файлы",
        "Как работает офлайн-доступ в Client?",
        "Команда «Скачать для офлайн-доступа» сохраняет локальную копию с той же структурой папок "
        "в выбранный каталог. Индекс сравнивает базовый, локальный и серверный SHA-256. Если обе "
        "версии изменились, Client показывает конфликт и перед скачиванием сохраняет локальную "
        "версию отдельным файлом с пометкой local-conflict.",
        "офлайн offline кэш локальная копия без интернета скачать",
        "client",
    ),
    KnowledgeArticle(
        "formatting-safety",
        "Диски",
        "Форматирует ли программа мои диски?",
        "Нет. Текущие версии не форматируют диски, не меняют разделы и не переносят существующие "
        "файлы. Core создаёт только собственный каталог CloudStorageData или .cloud-storage-data и "
        "отказывается занимать непустой чужой каталог без своего маркера.",
        "форматирование удалить данные раздел диск безопасность",
        "server",
    ),
    KnowledgeArticle(
        "disk-modes",
        "Диски",
        "Что означают режимы диска?",
        "«Активен» разрешает новые записи. «Запись приостановлена» и «Обслуживание» исключают "
        "диск из выбора для новых файлов, но уже сохранённые данные остаются доступны для чтения. "
        "«Отключён» исключает накопитель из управляемого хранилища.",
        "режим активен пауза запись обслуживание отключён диск",
        "server",
    ),
    KnowledgeArticle(
        "fill-reserve",
        "Диски",
        "Зачем нужны предел заполнения и резерв места?",
        "Предел заполнения запрещает Core доводить диск выше заданного процента. Минимальный "
        "резерв оставляет фиксированный объём свободным. Оба ограничения проверяются до выбора "
        "диска для новой загрузки.",
        "предел заполнения процент резерв свободное место диск",
        "server",
    ),
    KnowledgeArticle(
        "advanced-mode",
        "Интерфейс",
        "Почему нужен расширенный режим настроек?",
        "Простой режим показывает только безопасные основные параметры. Расширенный открывает "
        "сеть, права, автоматизацию, журналы и обслуживание. Выбор режима теперь сохраняется сразу "
        "и не сбрасывается при автоматическом обновлении.",
        "расширенный простой режим настройки сбрасывается возвращается",
        "server",
    ),
    KnowledgeArticle(
        "core-offline",
        "Диагностика",
        "Что делать, если Server Core выключен?",
        "Откройте Server Manager → Настройки → Сервер и нажмите «Запустить ядро». Если запуск не "
        "удался, откройте раздел диагностики и файл core.log. Принудительная остановка или "
        "удаление базы автоматически не выполняются.",
        "core выключен не запускается ошибка журнал core.log",
        "server",
    ),
    KnowledgeArticle(
        "client-offline",
        "Диагностика",
        "Почему Desktop Client показывает «Офлайн»?",
        "Проверьте, что Core запущен и адрес совпадает. Для второго компьютера должен быть включён "
        "защищённый LAN-вход. Client повторяет проверку каждые 10 секунд и восстановит доступ с "
        "тем же токеном, когда сервер снова появится.",
        "client офлайн недоступен соединение адрес core переподключение",
        "client",
    ),
    KnowledgeArticle(
        "backup-not-redirect",
        "Резервные копии",
        "Является ли второй диск резервной копией?",
        "Нет. Перенаправление новых файлов на дополнительный диск увеличивает ёмкость, но не "
        "создаёт вторую копию. Резервное копирование и зеркало являются отдельными функциями "
        "этапа 4.",
        "резервная копия backup второй диск зеркало перенаправление",
    ),
    KnowledgeArticle(
        "resumable-transfers",
        "Файлы",
        "Продолжится ли большой файл после обрыва или перезапуска?",
        "Да. Очередь Client хранится в SQLite. Для загрузки Core запоминает подтверждённое смещение "
        "и принимает следующий блок только с него; скачивание продолжается из проверенного файла "
        "с суффиксом .part. Кнопки паузы и продолжения доступны во вкладке «Передачи» и в трее.",
        "докачка продолжить передача пауза перезапуск обрыв большой файл очередь",
        "client",
    ),
    KnowledgeArticle(
        "verified-migration",
        "Обслуживание",
        "Как безопасно перенести данные на другой диск?",
        "Сначала приостановите запись на исходный диск, оставив целевой активным. Затем откройте "
        "расширенные настройки → «Обслуживание». Core копирует каждый объект через staging, "
        "сверяет размер и SHA-256 и атомарно переключает метаданные. Исходные объекты в Alpha "
        "0.4.0a2 не удаляются и остаются страховочными копиями.",
        "перенос миграция другой диск sha обслуживание пауза страховочная копия",
        "server",
    ),
    KnowledgeArticle(
        "verified-backup",
        "Резервные копии",
        "Что находится в проверенном резервном снимке?",
        "Снимок содержит согласованную SQLite-копию метаданных, manifest.json, текущие объекты "
        "и историю версий. Диск должен иметь роль «Резервные копии» и не используется для "
        "обычных загрузок. Снимок появляется в каталоге backups только после проверки размера и "
        "SHA-256 каждого объекта.",
        "backup резервная копия снимок manifest sqlite sha проверить восстановление",
        "server",
    ),
    KnowledgeArticle(
        "verified-mirror",
        "Автоматизация",
        "Как работает зеркало и что будет при отказе основного диска?",
        "Диск с ролью «Зеркало» не принимает обычные загрузки как дополнительное место. После "
        "фиксации новой версии Core создаёт на нём отдельную копию через staging и проверяет "
        "размер и SHA-256. Если зеркало временно недоступно, основная загрузка сохраняется, а "
        "Manager показывает деградацию. Команда «Проверить и восстановить зеркало» пересоздаёт "
        "повреждённые реплики. При недоступности основного объекта Core может скачать актуальную "
        "зеркальную копию.",
        "зеркало mirror реплика отказ диск восстановить sha деградация копия",
        "server",
    ),
    KnowledgeArticle(
        "server-diagnostics",
        "Диагностика",
        "Что проверяет диагностика сервера?",
        "Быстрая проверка контролирует SQLite, внешние ключи, доступность и заполнение дисков, "
        "маркеры управляемых каталогов и ошибки фоновых заданий. Она запускается после старта "
        "Core и повторяется каждые пять минут. Полная проверка дополнительно читает все текущие "
        "объекты, версии и зеркальные реплики и пересчитывает SHA-256. Найденная проблема создаёт "
        "постоянный инцидент с рекомендацией, но не изменяет файл автоматически.",
        "диагностика мониторинг инцидент проверка sha sqlite диск ошибка целостность",
        "server",
    ),
    KnowledgeArticle(
        "verified-restore",
        "Резервные копии",
        "Как восстановить повреждённый или пропавший файл из снимка?",
        "Откройте расширенные настройки → «Резервные копии», выберите активный основной диск "
        "и нажмите «Восстановить повреждённые объекты» у готового снимка. Core проверит размер "
        "и SHA-256. Он вернёт только отсутствующие или повреждённые объекты, которые всё ещё "
        "соответствуют снимку; исправные и более новые файлы не перезаписываются.",
        "восстановить recovery снимок backup пропал поврежден sha новый файл откат",
        "server",
    ),
    KnowledgeArticle(
        "emergency-read-only",
        "Диагностика",
        "Что делает аварийный режим только чтения?",
        "В Server Manager откройте «Настройки → Сервер» и включите аварийный режим. Core "
        "остановит новые загрузки, удаления, подключения устройств и фоновые задания, но "
        "оставит скачивание, диагностику, отзыв устройств и проверяемое восстановление. Причина "
        "и смена режима сохраняются в журнале. Возвращайте обычный режим только после устранения "
        "инцидента.",
        "аварийный режим только чтение read only остановить запись инцидент",
        "server",
    ),
    KnowledgeArticle(
        "automatic-backup-policy",
        "Резервные копии",
        "Как настроить автоматические копии и сколько снимков хранить?",
        "Откройте расширенные настройки → «Резервные копии». Выберите backup-диск и основной "
        "диск для проверки, задайте интервал и количество сохраняемых снимков, затем включите "
        "автоматизацию. После каждого снимка Core выполняет пробное восстановление базы и всех "
        "объектов. Старые копии удаляются только после успешной проверки нового снимка; "
        "непроверенные копии и снимки активного восстановления не затрагиваются.",
        "автоматический backup расписание интервал хранить ротация очистка снимок",
        "server",
    ),
    KnowledgeArticle(
        "backup-restore-drill",
        "Резервные копии",
        "Что означает проверка пробным восстановлением?",
        "Core открывает SQLite-копию снимка в режиме только чтения, проверяет её целостность и "
        "по одному временно копирует каждый объект на выбранный основной диск. Размер и SHA-256 "
        "должны совпасть. Тестовые файлы удаляются, а рабочие файлы и метаданные не меняются. "
        "Так проверяется не только наличие копии, но и возможность реально прочитать и записать её.",
        "пробное восстановление проверка копии sqlite sha тест backup пригодность",
        "server",
    ),
)

_WORD = re.compile(r"[a-zа-яё0-9]{2,}", re.IGNORECASE)
_STOP_WORDS = {
    "как",
    "что",
    "где",
    "для",
    "или",
    "это",
    "если",
    "при",
    "мой",
    "мои",
    "мне",
    "почему",
    "будет",
    "работает",
    "ли",
    "умеет",
    "система",
    "программа",
    "приложение",
}


class KnowledgeBase:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS articles (
                    id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    keywords TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS unanswered_questions (
                    normalized_question TEXT NOT NULL,
                    audience TEXT NOT NULL,
                    original_question TEXT NOT NULL,
                    asked_at TEXT NOT NULL,
                    ask_count INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(normalized_question, audience)
                );
                """
            )
            now = datetime.now(UTC).isoformat(timespec="seconds")
            connection.executemany(
                """
                INSERT INTO articles(id, category, question, answer, keywords, audience, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    category = excluded.category,
                    question = excluded.question,
                    answer = excluded.answer,
                    keywords = excluded.keywords,
                    audience = excluded.audience,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        item.id,
                        item.category,
                        item.question,
                        item.answer,
                        item.keywords,
                        item.audience,
                        now,
                    )
                    for item in ARTICLES
                ],
            )

    def categories(self, audience: str) -> list[str]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT category FROM articles
                WHERE audience IN ('both', ?)
                ORDER BY category COLLATE NOCASE
                """,
                (audience,),
            ).fetchall()
        return [str(row["category"]) for row in rows]

    def search(
        self,
        query: str,
        audience: str,
        *,
        category: str = "",
        limit: int = 8,
    ) -> list[SearchResult]:
        conditions = ["audience IN ('both', ?)"]
        parameters: list[str] = [audience]
        if category:
            conditions.append("category = ?")
            parameters.append(category)
        with self._session() as connection:
            rows = connection.execute(
                "SELECT id, category, question, answer, keywords, audience "
                "FROM articles WHERE " + " AND ".join(conditions),
                parameters,
            ).fetchall()
        articles = [self._article(row) for row in rows]
        normalized = self._normalize(query)
        if not normalized:
            return [SearchResult(item, 1.0) for item in articles[:limit]]
        tokens = [
            item for item in _WORD.findall(normalized) if len(item) >= 3 and item not in _STOP_WORDS
        ]
        results = [SearchResult(item, self._score(normalized, tokens, item)) for item in articles]
        results.sort(key=lambda item: (-item.score, item.article.question.casefold()))
        return [item for item in results if item.score > 0][:limit]

    def ask(self, question: str, audience: str) -> AssistantAnswer:
        results = self.search(question, audience, limit=3)
        if not results or results[0].score < 1.35:
            self._remember_unanswered(question, audience)
            return AssistantAnswer(
                text=(
                    "В локальной базе пока нет достаточно точного ответа. Я сохранил вопрос "
                    "в список пробелов базы знаний. Попробуйте спросить короче или выберите "
                    "подходящую статью слева."
                ),
                sources=(),
                confidence=0.0,
            )
        top = results[0]
        related = tuple(item.article for item in results[1:] if item.score >= top.score * 0.55)
        return AssistantAnswer(
            text=top.article.answer,
            sources=(top.article, *related),
            confidence=min(1.0, top.score / 7.0),
        )

    def unanswered_count(self) -> int:
        with self._session() as connection:
            return int(
                connection.execute(
                    "SELECT COALESCE(sum(ask_count), 0) FROM unanswered_questions"
                ).fetchone()[0]
            )

    def _remember_unanswered(self, question: str, audience: str) -> None:
        normalized = self._normalize(question)
        if len(normalized) < 3:
            return
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO unanswered_questions(
                    normalized_question, audience, original_question, asked_at, ask_count
                ) VALUES(?, ?, ?, ?, 1)
                ON CONFLICT(normalized_question, audience) DO UPDATE SET
                    original_question = excluded.original_question,
                    asked_at = excluded.asked_at,
                    ask_count = unanswered_questions.ask_count + 1
                """,
                (
                    normalized,
                    audience,
                    question.strip()[:500],
                    datetime.now(UTC).isoformat(timespec="seconds"),
                ),
            )

    @classmethod
    def _score(
        cls,
        normalized_query: str,
        tokens: list[str],
        article: KnowledgeArticle,
    ) -> float:
        question = cls._normalize(article.question)
        answer = cls._normalize(article.answer)
        keywords = cls._normalize(article.keywords)
        score = SequenceMatcher(None, normalized_query, question).ratio() * 2.5
        for token in tokens:
            stems = {token, token[: max(4, len(token) - 2)]}
            if any(stem in question for stem in stems):
                score += 2.0
            if any(stem in keywords for stem in stems):
                score += 1.5
            if any(stem in answer for stem in stems):
                score += 0.45
        if normalized_query in question or question in normalized_query:
            score += 3.0
        return score

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(_WORD.findall(value.casefold().replace("ё", "е")))

    @staticmethod
    def _article(row: sqlite3.Row) -> KnowledgeArticle:
        return KnowledgeArticle(
            id=row["id"],
            category=row["category"],
            question=row["question"],
            answer=row["answer"],
            keywords=row["keywords"],
            audience=row["audience"],
        )
