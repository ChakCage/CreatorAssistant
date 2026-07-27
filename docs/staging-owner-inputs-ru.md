# Данные, необходимые для публичного staging

Для реального deploy владелец должен предоставить вне Git:

- VPS IP/SSH hostname, SSH user и доступ по ключу;
- базовый domain и возможность изменить DNS;
- email для Let's Encrypt;
- отдельный staging Telegram bot token;
- support username и Telegram admin IDs;
- желаемую длительность beta access;
- HTTPS URL/имя staging installer.

Нужны DNS A/AAAA для API, bot и download. Secrets передаются прямо в protected
env на VPS, не в чат, commit или отчёт. Пока этих данных нет, пакет можно
проверить локально, но staging нельзя считать публично доступным.
