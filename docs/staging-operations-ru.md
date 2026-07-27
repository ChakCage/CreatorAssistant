# Эксплуатация staging

Ежедневно проверяйте `docker compose ps`, health API/bot, последние errors,
notification queue, свободное место и `.last-success` backup. Логи ограничены
5 файлами по 10 МБ. Нельзя логировать codes, tokens, entitlement или keys.

Обновление выполняется `deploy-staging.ps1`; миграция завершается до запуска
нового API. Installer кладётся только в releases volume под именем
`CreatorAssistant-Commercial-Staging-<version>.exe`; directory listing закрыт.
Release в БД обязан быть `commercial/beta` и иметь подписанный manifest.

Остановка:

```bash
cd /opt/creator-assistant-staging/current/deployment/staging
docker compose --env-file .env.staging down
```

Volumes не удалять. Production Commercial и Developer не менять.
