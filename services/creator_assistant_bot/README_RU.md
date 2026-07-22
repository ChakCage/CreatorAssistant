# Telegram-бот Creator Assistant

Бот продаёт и обслуживает лицензии Commercial через licensing backend. Он не хранит цены,
платежные статусы или подписки локально. Для доступа используется короткоживущий сервисный
токен с ограниченным набором разрешений; admin token боту не передаётся.

Локально допускается только `FakePaymentProvider`. В production тестовая страница отключена,
webhook Telegram требует HTTPS и secret token. Секреты задаются переменными окружения по
образцу `.env.example` и не добавляются в Git.

Запуск: `python -m bot.main`. Проверки: `pytest -q`.
