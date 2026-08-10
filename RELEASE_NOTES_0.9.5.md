# Cloud Storage 0.9.5 — Idea 4

Полный релиз системного центра для домашнего облака.

## Основные приложения

1. `CloudStorage-Server-Setup-0.9.5-windows-x64.exe` — установщик Manager, Core и службы.
2. `CloudStorage-Client-Setup-0.9.5-windows-x64.exe` — установщик Desktop Client, WinFsp и виртуального диска.
3. `CloudStorage-Desktop-Client-0.9.5-windows-x64.zip` — переносимый клиент ПК.
4. `CloudStorage-Server-Manager-0.9.5-windows-x64.zip` — переносимый менеджер ПК.
5. `CloudStorage-Mobile-Client-0.9.5-android.apk` — телефонный клиент.
6. `CloudStorage-Mobile-Manager-0.9.5-android.apk` — телефонный менеджер.

## Idea 4

- единая вкладка «Система»: профили, интерфейс, безопасность, питание, сеть, zrok, боты, отчёты, автоматизации и ячейки;
- простой интерфейс с центральным меню разделов и подробный интерфейс со всей навигацией;
- Telegram-команды `/status`, `/power`, `/storage`, SMTP email и HTTPS webhook с тестом подключения;
- уведомления ИБП, сон по простою, Wake-on-LAN и подтверждаемое аварийное выключение через час без внешнего питания;
- 10 аварийных и 10 обычных шаблонов автоматизации;
- отчёты по выбранным разделам и расписанию;
- изолированные Docker/Podman-ячейки с автоматическими лимитами CPU/RAM/диска;
- личное пространство, одноразовый код, файл входа, ссылка скачивания и email-передача данных нового пользователя;
- выбор и управление всеми накопителями, SSD-приём с последующей выгрузкой на HDD, зеркало и очистка временных данных;
- отдельные Client/Manager сборки для Android, iPhone/iPad Simulator, неподписанные IPA и MacBook DMG.

## Проверка

- Python: полный набор `pytest` и `ruff`;
- мобильный код: TypeScript typecheck и Expo Doctor;
- Windows: PyInstaller, WinFsp-драйвер, Inno Setup и SHA-256;
- Apple: отдельные GitHub Actions сборки для macOS и iOS.

Физическая установка IPA и публикация через TestFlight требуют подписи Apple Developer владельца проекта. Неподписанные IPA предназначены для последующей личной или корпоративной подписи.
