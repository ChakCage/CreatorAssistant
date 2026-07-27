# Telegram-бот закрытой беты

Создайте отдельного staging-бота через @BotFather и задайте меню без оплаты.
Token передаётся только в protected env. Production mode использует webhook
`https://bot-staging.<domain>/telegram/webhook` и secret token; local mode
оставляет long polling.

Меню: тестовый доступ, подписка, activation code, devices, download,
инструкция, feedback и support. Invite вводится командой
`/beta BETA-XXXX-XXXX`. Бот не отправляет entitlement token и не показывает
Developer/production installer.

Проверяйте `getWebhookInfo`, `/health`, `/ready` и delivery errors. Не
включайте `CREATOR_BOT_MANAGE_WEBHOOK` постоянно: webhook меняется только
операционной командой.
