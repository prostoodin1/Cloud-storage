# 1. Установщик сервера

Ставит Server Manager, Container Manager, нативное ядро, compatibility engine и Windows-службу.

**Windows b4, beta:** [скачать CloudStorage-Server-Setup-0.10.8b4-windows-x64.exe](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.10.8b4/CloudStorage-Server-Setup-0.10.8b4-windows-x64.exe).
[Все файлы и SHA-256](https://github.com/prostoodin1/Cloud-storage/releases/tag/v0.10.8b4).

ZIP является переносным комплектом, а versioned Setup — полным установщиком.
Старые постоянные сетевые установщики используют отдельный каталог версий.

## Исходники

- [packaging/CloudStorageServer.iss](../../packaging/CloudStorageServer.iss)
- [packaging/CloudStorageServer.spec](../../packaging/CloudStorageServer.spec)
- [packaging/CloudStorageContainerManager.spec](../../packaging/CloudStorageContainerManager.spec)
- [packaging/CloudStorageLegacyCore.spec](../../packaging/CloudStorageLegacyCore.spec)
- [core_go](../../core_go)
- [scripts/build-core-go.ps1](../../scripts/build-core-go.ps1)
- [scripts/build-installers.ps1](../../scripts/build-installers.ps1)

[Первое подключение](../../docs/getting-started.md) ·
[Разработка и сборка](../../docs/development.md) ·
[Поддержка](../../SUPPORT.md) ·
[История раздела](https://github.com/prostoodin1/Cloud-storage/commits/main/apps/01-server-installer)
