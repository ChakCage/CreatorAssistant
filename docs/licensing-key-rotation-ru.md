# Ротация ключей и секретов лицензирования

Секреты не хранятся в Git и меняются через secret store окружения.

## Ed25519 entitlement keys

1. Создать новый 32-байтовый seed и новый `LICENSE_SIGNING_KEY_ID`.
2. До переключения добавить новый public key в Staging build и выполнить activation/refresh E2E.
3. Выпустить Commercial с набором старого и нового public key.
4. Переключить backend на новый private seed/key id.
5. Оставить старый public key в клиенте дольше максимального срока entitlement/offline grace.
6. Только после этого удалить старый public key из следующего клиента.

Private seed никогда не помещается в EXE. Production и Staging используют разные пары ключей.

## Pepper и service secrets

- Ротация `LICENSE_ACTIVATION_PEPPER` инвалидирует неиспользованные коды; сначала уведомить поддержку.
- `LICENSE_BOT_SERVICE_SECRET` меняется одновременно в backend и bot. Токены живут не более 10 минут,
  поэтому период двойного секрета не требуется при согласованном restart.
- `LICENSE_FAKE_PAYMENT_SECRET` существует только в local/staging и меняется после утечки;
  старые checkout sessions после этого недействительны.
- Telegram bot token перевыпускается через BotFather и никогда не передаётся licensing backend.

После каждой ротации проверить readiness, scoped permissions, webhook replay protection и отсутствие
секретов в structured logs/crash reports.
