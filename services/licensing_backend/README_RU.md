# Licensing backend Creator Assistant

Backend выдаёт одноразовые коды, регистрирует устройства, подписывает короткоживущие
entitlement-токены Ed25519 и является единственным источником истины для тарифов, платежей
и подписок. Telegram-бот обращается к нему только через короткоживущую scoped identity.

Деньги представлены целым числом minor units. В репозитории реализован только локальный
`FakePaymentProvider`; production adapter намеренно не настроен. Fake checkout отключён в production.

## Локальный запуск

1. Скопировать `.env.example` в `.env`.
2. Сгенерировать 32-байтовый Ed25519 seed и длинный `LICENSE_ACTIVATION_PEPPER`.
3. Получить hash admin-токена: `python -m app.admin_cli create-admin --token <случайный-токен>`.
4. Выполнить `docker compose up -d --build postgres redis`.
5. Выполнить миграцию: `docker compose run --rm api alembic upgrade head`.
6. Запустить API: `docker compose up -d api`.
7. Проверить `http://127.0.0.1:18080/health` и `/ready`.

## Тестовая подписка

```powershell
python -m app.admin_cli create-user --email friend@example.test
python -m app.admin_cli grant-subscription --user <USER_ID> --plan beta --days 30
python -m app.admin_cli create-activation-code --user <USER_ID> --ttl-minutes 30
```

Код выводится только один раз. В БД сохраняется только HMAC-SHA256 hash. Production/staging отказываются запускаться без PostgreSQL, pepper и Ed25519 private seed. Admin API выключен, пока не задан `LICENSE_ADMIN_TOKEN_HASH`.
