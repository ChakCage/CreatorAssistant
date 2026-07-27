# Домены и DNS staging

Создайте три A/AAAA-записи на IP staging VPS:

- `api-staging.<domain>` — licensing API;
- `bot-staging.<domain>` — Telegram webhook;
- `download-staging.<domain>` — установщик Commercial Staging.

До deploy дождитесь распространения DNS. Caddy получает сертификаты
Let's Encrypt автоматически; порт 80 нужен для challenge/redirect. Desktop
собирается только с `https://api-staging.<domain>` и стандартной проверкой
hostname/certificate. IP, HTTP и отключение TLS verification запрещены.

Проверьте `Resolve-DnsName`, затем:
`scripts/verify-staging.ps1 -SshHost ... -SshUser ...`.
