# Настройка публикации YouTube и TikTok

Creator Assistant по умолчанию работает в режиме **DRY_RUN**: сетевые запросы публикации не выполняются.

## YouTube

1. Создайте проект в Google Cloud Console.
2. Включите **YouTube Data API v3**.
3. Настройте OAuth consent screen и добавьте свой Google-аккаунт как test user, если приложение находится в Testing.
4. Создайте OAuth Client типа **Desktop app** и скачайте JSON.
5. В Creator Assistant откройте **Настройки → Публикация и аккаунты**.
6. Нажмите **Импортировать OAuth-конфигурацию Google**, затем **Подключить канал YouTube**.

JSON читается один раз. Client secret и OAuth tokens сохраняются в Windows Credential Manager, а не в настройках или проекте.

Официальная документация:

- https://developers.google.com/youtube/v3/guides/auth/installed-apps
- https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol

## TikTok

1. Создайте приложение в TikTok for Developers.
2. Подключите Content Posting API.
3. Настройте redirect URI и scopes `user.info.basic`, `video.upload`, при необходимости `video.publish`.
4. В **Настройки → Публикация и аккаунты** нажмите **Настроить TikTok App**.
5. Введите Client Key, Client Secret и тот же зарегистрированный redirect URI.
6. Нажмите **Подключить TikTok**.

Неаудированное приложение может быть ограничено private-only/SELF_ONLY. Creator Assistant показывает это как ограничение и не пытается его обходить.

Официальная документация:

- https://developers.tiktok.com/doc/content-posting-api-get-started/
- https://developers.tiktok.com/doc/content-posting-api-reference-direct-post
- https://developers.tiktok.com/doc/content-posting-api-reference-upload-video

## Безопасный первый запуск

1. Оставьте `DRY_RUN` и проверьте очередь публикаций.
2. Выполните один `PRIVATE_TEST` после явного подтверждения.
3. `REAL` становится доступен только после успешного private test и ручного включения. Ограничения app review продолжают действовать.

Никогда не добавляйте OAuth client JSON, access token, refresh token или client secret в Git.
