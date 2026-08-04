# 6. Телефонный менеджер

Административная часть мобильного приложения: пользователи и пароли, подтверждение устройств, диагностика, режим только чтения, накопители, резервные копии, автоматизация, zrok и перезапуск Core.

- [Скачать телефонный менеджер для Android](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Mobile-Manager-0.9.0-android.apk)
- [Скачать iPhone Simulator](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.0/CloudStorage-Mobile-0.9.0-iphone-simulator.zip)
- [Экран управления сервером](../../mobile/src/screens/ServerControlScreen.tsx)
- [Клиент защищённого API](../../mobile/src/api.ts)
- [Серверные подтверждения](../../cloud_storage/core/api.py)
- [Скриншот iPhone](../../screenshots/CloudStorage-Mobile-0.9.0-iphone.png)
- [История изменений раздела](https://github.com/prostoodin1/Cloud-storage/commits/codex/cloud-storage-alpha-0.4.0a2/apps/06-mobile-manager)

Менеджер имеет отдельный package ID и принимает только административный режим. Опасные операции требуют пароль, биометрию при наличии и одноразовое серверное подтверждение.
