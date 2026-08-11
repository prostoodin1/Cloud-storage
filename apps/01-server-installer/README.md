# 1. Установщик сервера

Windows-установщик разворачивает Server Manager, Core API и фоновую службу, создаёт каталоги данных и ярлыки, а также настраивает удаление и обновление.

- [Скачать Server Setup 0.9.9](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.9/CloudStorage-Server-Setup-0.9.9-windows-x64.exe)
- [Сценарий Inno Setup](../../packaging/CloudStorageServer.iss)
- [Общий сценарий сборки](../../scripts/build-installers.ps1)
- [PyInstaller Manager](../../packaging/CloudStorageServer.spec)
- [PyInstaller Core](../../packaging/CloudStorageCore.spec)
- [PyInstaller Service](../../packaging/CloudStorageWindowsService.spec)
- [История изменений раздела](https://github.com/prostoodin1/Cloud-storage/commits/codex/cloud-storage-alpha-0.4.0a2/apps/01-server-installer)

Сборка: `powershell -ExecutionPolicy Bypass -File scripts/build-installers.ps1`.
