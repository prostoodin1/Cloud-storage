# 2. Установщик клиента

Windows-установщик ставит Desktop Client, виртуальный диск Cloud Storage для Проводника и необходимые компоненты WinFsp. Он также создаёт ярлыки и записи удаления/обновления.

- [Скачать Client Setup 0.9.10](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.10/CloudStorage-Client-Setup-0.9.10-windows-x64.exe)
- [Сценарий Inno Setup](../../packaging/CloudStorageClient.iss)
- [Сценарий клиентской сборки](../../scripts/build-client-installer.ps1)
- [PyInstaller Client](../../packaging/CloudStorageClient.spec)
- [Исходники диска Проводника](../../drive_windows)
- [История изменений раздела](https://github.com/prostoodin1/Cloud-storage/commits/codex/cloud-storage-alpha-0.4.0a2/apps/02-client-installer)

Сборка: `powershell -ExecutionPolicy Bypass -File scripts/build-client-installer.ps1`.
