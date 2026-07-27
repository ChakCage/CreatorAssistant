# Secrets staging

Скопируйте `deployment/staging/.env.staging.example` в
`/opt/creator-assistant-staging/shared/.env.staging`, заполните на VPS и
выполните `chmod 600`. Файл не коммитится и не попадает в Docker images.

Обязательны отдельные staging-значения: PostgreSQL/Redis passwords,
activation pepper, Ed25519 license private key и key id, admin token hash,
bot service secret, Telegram token/webhook secret и backup encryption key.
Production keys использовать запрещено. Генерируйте secrets CSPRNG длиной
не менее 32 bytes. Admin token храните только как SHA-256.

Не вставляйте secrets в issue, CI artifact, diagnostic ZIP или командную
строку. Перед deploy запускается `scripts/scan-staging-secrets.ps1`.
При утечке остановите bot/API, замените secret, отзовите sessions, обновите
`.env.staging`, перезапустите stack и зафиксируйте incident.
