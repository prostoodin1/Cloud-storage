# Компоненты Cloud Storage

Общий код хранится в одном репозитории. Эти каталоги — навигация и метаданные
компонентов; это не шесть независимых копий серверного кода.

| Компонент | Назначение | Поставка b4 |
|---|---|---|
| [Установщик сервера](01-server-installer/README.md) | Менеджеры, ядро, служба | Windows x64 Setup |
| [Установщик клиента](02-client-installer/README.md) | Client и WinFsp | Windows x64 Setup |
| [Desktop Client](03-desktop-client/README.md) | Файлы, передачи, виртуальный диск | Windows x64 ZIP |
| [Server Manager](04-server-manager/README.md) | Пользователи, накопители, диагностика | Windows x64 ZIP с серверными компонентами |
| [Mobile Client](05-mobile-client/README.md) | Пользовательский мобильный интерфейс | Исходники, без APK/IPA в b4 |
| [Mobile Manager](06-mobile-manager/README.md) | Административный мобильный интерфейс | Исходники, без APK/IPA в b4 |

Для Windows используйте [точные ссылки на b4](../README.md).
`component.json` указывает опубликованный Windows-артефакт или явно помечает
мобильный компонент как исходники; исторические имена не считаются свежими сборками.

[История версий](../CHANGELOG.md) · [Разработка](../docs/development.md) ·
[Лицензия проекта](../LICENSE) · [Сторонние компоненты](../THIRD-PARTY-NOTICES.md)
