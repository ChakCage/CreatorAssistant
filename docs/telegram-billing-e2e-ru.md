# E2E: Telegram bot и тестовая оплата

Этот сценарий предназначен только для локального/стендового окружения. Он не списывает деньги.
`FakePaymentProvider` отсутствует в production registry, а его checkout-страница отвечает 404.

## Подготовка

1. Скопировать `services/licensing_backend/.env.example` в `.env` и заменить все секреты.
2. Сгенерировать отдельные значения для `LICENSE_ACTIVATION_PEPPER`,
   `LICENSE_FAKE_PAYMENT_SECRET`, `LICENSE_BOT_SERVICE_SECRET` и Ed25519 seed.
3. Запустить PostgreSQL и Redis: `docker compose up -d postgres redis`.
4. Применить миграции: `docker compose run --rm api alembic upgrade head`.
5. Создать цену в minor units:
   `docker compose run --rm api python -m app.admin_cli create-price --plan beta --provider fake --amount-minor 99000 --currency RUB`.
6. Запустить API и mock-бот: `docker compose up -d --build api bot`.
7. Проверить `http://127.0.0.1:18080/ready` и `http://127.0.0.1:18081/ready`.

## Проверка покупки

1. Бот регистрирует Telegram user через scoped service identity.
2. Выбрать тариф. Цена берётся из backend, бот не передаёт сумму.
3. Открыть одноразовую ссылку checkout и нажать «Успешная оплата».
4. Повторно отправить тот же webhook командой CLI с тем же event id. Срок подписки
   не должен увеличиться второй раз.
5. Проверить `list-payments`, `list-payment-events`, подписку и очередь уведомлений.
6. Получить код активации. Предыдущий неиспользованный код должен стать `REVOKED`.
7. Активировать Commercial Staging, перезапустить его и проверить refresh entitlement.

## Негативные сценарии

- `fake-payment-event --invalid-signature` возвращает ошибку и не меняет Payment.
- Несовпадение amount/currency переводит событие в `REVIEW_REQUIRED`.
- Успешный платёж при BLOCKED subscription не разблокирует её автоматически.
- Слишком большой webhook получает HTTP 413 до JSON parsing.
- Отказ отправки Telegram notification создаёт retry с exponential backoff.

Логи не должны содержать activation code, checkout token, service token, bot token или webhook secret.
