# Известные ограничения закрытой беты

- Сборки без платного Authenticode-сертификата отмечаются как `UNSIGNED BETA`; SmartScreen может предупреждать.
- Production release backend на этапе 6A не развёрнут. Обновления тестируются локальным staging backend.
- Реальная отправка telemetry выключена; crash reports и support ZIP остаются локальными.
- Платёжный provider и настоящий Telegram-бот не подключены.
- Модели Ollama/Whisper и сторонние инструменты не входят в установщик.
