# Secrets staging

Скопируйте `deployment/staging/.env.staging.example` в
`/opt/creator-assistant-staging/shared/.env.staging`, заполните на VPS и
выполните `chmod 600`. Файл не коммитится и не попадает в Docker images.

Обязательны отдельные staging-значения: PostgreSQL/Redis passwords,
activation pepper, Ed25519 license private key и key id, admin token hash,
bot service secret, Telegram token/webhook secret и backup encryption key.
Production keys использовать запрещено. Генерируйте secrets CSPRNG длиной
не менее 32 bytes. Admin token храните только как SHA-256.

Для первичной подготовки используйте:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\initialize-staging-secrets.ps1
```

Telegram token вводится через `Read-Host -AsSecureString`. Постоянная локальная
копия создаётся только в DPAPI-хранилище текущего Windows-пользователя вне Git.
Временные upload-файлы защищаются ACL текущего пользователя и SYSTEM; после
успешной передачи на VPS их следует перезаписать и удалить. Staging license
private key передаётся отдельным файлом mode 600 и монтируется в контейнер как
Docker secret, а не помещается в image или installer.

Не вставляйте secrets в issue, CI artifact, diagnostic ZIP или командную
строку. Перед deploy запускается `scripts/scan-staging-secrets.ps1`.
При утечке остановите bot/API, замените secret, отзовите sessions, обновите
`.env.staging`, перезапустите stack и зафиксируйте incident.
