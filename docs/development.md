# Разработка и сборка

## Среда

- Python 3.11+ и виртуальная среда.
- Go 1.25+ для `core_go` и `drive_windows`.
- Inno Setup 6 для Windows Setup.
- Для mobile — Node.js и pnpm версии из [mobile/package.json](../mobile/package.json).
  Нативная iOS-сборка требует macOS/Xcode; JS-export не является проверкой телефона.

Установка в Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev,build]"
```

В Linux/macOS используйте `python3 -m venv .venv` и `.venv/bin/python`.

## Отдельные данные для разработки

Не запускайте эксперименты с каталогом установленной службы. Пример переменных
для отдельного стенда в текущем PowerShell:

```powershell
$env:CLOUD_STORAGE_CORE_DATA_DIR = Join-Path $PWD 'work\dev-core'
$env:CLOUD_STORAGE_CONFIG_DIR = Join-Path $PWD 'work\dev-manager'
$env:CLOUD_STORAGE_CLIENT_DATA_DIR = Join-Path $PWD 'work\dev-client'
$env:CLOUD_STORAGE_CORE_PORT = '18765'
$env:CLOUD_STORAGE_LAN_ENABLED = '0'
$env:CLOUD_STORAGE_REMOTE_ENABLED = '0'
```

Проверьте, что порт свободен. Эти переменные ограничены текущим процессом и его
потомками. Если открываете другое окно терминала, задайте их там тоже.
При уже запущенном Manager его single-instance механизм может активировать
существующее окно; используйте отдельную тестовую сессию/машину для GUI-экспериментов.

```powershell
# Ядро / Manager / Client запускаются отдельно по необходимости.
.\.venv\Scripts\python -m cloud_storage.core
.\.venv\Scripts\python -m cloud_storage
.\.venv\Scripts\python -m cloud_storage.client
```

Один серверный процесс занимает терминал; следующие команды запускайте в другом
окне с теми же переменными. Административная документация — loopback `/docs`.
Каталог `work/` исключён из Git; содержащиеся там секреты всё равно нельзя публиковать.

## Проверки

```powershell
.\.venv\Scripts\python -m pytest
.\.venv\Scripts\python -m ruff check cloud_storage tests
```

Для Go выполните `go test ./...` и `go vet ./...` отдельно внутри `core_go/`
и `drive_windows/`. В `mobile/`: `pnpm install --frozen-lockfile`,
`pnpm typecheck`, `pnpm test:transfers`.

Тесты используют дисковые ограничения настоящего хранилища: заполненный системный
том может привести к ожидаемому отказу приёма. Выберите отдельный свободный
временный каталог через `TEMP`/`TMP` или новый `pytest --basetemp` внутри выделенного
стенда. Pytest управляет этим каталогом, поэтому не указывайте путь с ценными данными.

## Сборка Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-installers.ps1
```

Если инструменты не в стандартном месте, задайте `CLOUD_STORAGE_GO`,
`CLOUD_STORAGE_ISCC` и при необходимости `CLOUD_STORAGE_PYTHON`.
Полный сценарий выполняет тесты, собирает приложения и проверяет закреплённый
SHA-256 скачанного WinFsp. Результаты:

- `work/release-<version>/dist/` — компоненты;
- `outputs/` — versioned Setup, переносные ZIP и сетевые bootstrap-установщики.

Для проверки пакета: `scripts/verify-package.ps1 -Dist <dist> -ExpectedVersion <version>`;
для нативного маршрута добавьте `-NativeCore`.
`scripts/verify-mounted-drive.ps1` требует установленного WinFsp и свободной буквы диска.
Запускайте их в отдельной среде с выделенным `TEMP` и сохраните JSON-результаты.

В Linux/macOS базовая сборка — `bash scripts/build.sh`; дополнительные Apple-сценарии
находятся в `scripts/`. Их наличие не является доказательством проверки физического устройства.

## Выпуски

Меняйте номер версии согласованно в Python-метаданных, `cloud_storage/__init__.py`
и обоих Inno Setup-сценариях. Исходники выпуска должны соответствовать его Git-тегу.
Перед публикацией проверьте состав архивов, лицензии, версии, SHA-256 и скачивание
без авторизации. Секрет подписи обновлений не хранится в Git или артефактах CI.

[Сетевой стенд](network-lab.md) требует отдельной disposable Linux/WSL-среды с root;
обычные тесты не должны менять сетевые интерфейсы рабочего компьютера.
