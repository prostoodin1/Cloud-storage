# Cloud Storage 0.9.0

## Шесть основных загрузок

1. [Установщик сервера](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Server-Setup-0.9.0-windows-x64.exe)
2. [Установщик клиента](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Client-Setup-0.9.0-windows-x64.exe)
3. [Клиент на ПК](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Desktop-Client-0.9.0-windows-x64.zip)
4. [Менеджер на ПК](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Server-Manager-0.9.0-windows-x64.zip)
5. [Телефонный клиент](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Mobile-Client-0.9.0-android.apk)
6. [Телефонный менеджер](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Mobile-Manager-0.9.0-android.apk)

Телефонный клиент и менеджер имеют разные Android package ID и могут быть установлены одновременно. Дополнительно в релизе сохранена iPhone Simulator-сборка и два служебных каталога, необходимые для автоматического обновления Windows-приложений.

Основной мобильный этап завершён.

- Android и iPhone используют общий React Native/Expo-код с нативным Keychain/Keystore.
- Фото и видео автоматически попадают в постоянную возобновляемую очередь; доступны ограничения по сети и питанию.
- Добавлены поиск и отзываемые временные ссылки на файлы и папки.
- Администратор может создавать и отключать пользователей, менять пароли, одобрять устройства и управлять серверным обслуживанием.
- Каждое опасное мобильное действие требует свежий пароль, локальную биометрию при наличии и одноразовый подтверждающий токен.
- Выпуск содержит Windows Server/Client installers, подписанные Android APK/AAB, iPhone Simulator app, исходники и скриншоты.

Физический iPhone принимает приложение только с сертификатом Apple Developer владельца. Поэтому публичный выпуск содержит неподписанную Simulator-сборку; Xcode-проект генерируется из опубликованных исходников.
