# Управление закрытой бетой

Команды запускаются на VPS внутри API container по защищённому SSH. Перед
командой передайте admin token только через защищённую переменную
`LICENSE_CLI_ADMIN_TOKEN` (не аргумент командной строки и не log):

```bash
docker compose --env-file .env.staging exec api \
  python -m app.admin_cli create-beta-invite --days 14 --uses 1 \
  --subscription-days 14 --device-limit 1
docker compose --env-file .env.staging exec api python -m app.admin_cli list-beta-invites
docker compose --env-file .env.staging exec api python -m app.admin_cli revoke-beta-invite --invite <id>
docker compose --env-file .env.staging exec api python -m app.admin_cli grant-beta-subscription --telegram-user-id <id> --days 14
```

Invite показывается только при создании, в БД хранится hash. Один Telegram
account может погасить только один beta invite. Для аудита доступны list/show/
export. Отзыв доступа не удаляет данные пользователя и не затрагивает
production.
