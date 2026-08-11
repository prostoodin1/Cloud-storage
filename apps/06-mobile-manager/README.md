# 6. Телефонный менеджер

Административная часть мобильного приложения: пользователи и пароли, подтверждение устройств, диагностика, режим только чтения, накопители, резервные копии, автоматизация, zrok и перезапуск Core.

- [Скачать телефонный менеджер для Android](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.7/CloudStorage-Mobile-Manager-0.9.7-android.apk)
- [Скачать iPhone/iPad Simulator](https://github.com/prostoodin1/Cloud-storage/releases/download/v0.9.7/CloudStorage-Mobile-Manager-0.9.7-apple-simulator.zip)
- [Экран управления сервером](../../mobile/src/screens/ServerControlScreen.tsx)
- [Клиент защищённого API](../../mobile/src/api.ts)
- [Серверные подтверждения](../../cloud_storage/core/api.py)
- [Скриншот iPhone](../../screenshots/CloudStorage-Mobile-manager-0.9.7-iPhone.png)
- [История изменений раздела](https://github.com/prostoodin1/Cloud-storage/commits/codex/cloud-storage-alpha-0.4.0a2/apps/06-mobile-manager)

Менеджер имеет отдельный package ID и принимает только административный режим. Опасные операции требуют пароль, биометрию при наличии и одноразовое серверное подтверждение.
