# Развёртывание staging

1. Подготовьте VPS, DNS и protected `.env.staging`.
2. Импортируйте только staging private keys по инструкции
   `staging-key-deployment-ru.md`.
3. Убедитесь, что branch clean и тесты проходят.
4. Выполните:

```powershell
.\scripts\deploy-staging.ps1 -SshHost <host> -SshUser <user> `
  -SshKey <path> -ConfirmDeploy
```

Скрипт проверяет clean worktree, тесты, secret scan, создаёт immutable release,
строит images, применяет Alembic, запускает stack и проверяет HTTPS. При ошибке
запрашивается rollback. Первый deploy без предыдущего release требует ручного
исправления, rollback невозможен.

Webhook устанавливается отдельно и явно на VPS (host должен иметь `curl`):

```bash
bash /opt/creator-assistant-staging/current/deployment/staging/ops/set-webhook.sh \
  /opt/creator-assistant-staging/shared/.env.staging
```

`deleteWebhook`
выполняется только при явном `DELETE_OLD_WEBHOOK=true`.

Rollback:

```powershell
.\scripts\rollback-staging.ps1 -SshHost <host> -SshUser <user> -ConfirmRollback
```
