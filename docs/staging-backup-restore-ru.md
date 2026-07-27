# Backup и restore staging

Service `backup` ежедневно создаёт custom-format `pg_dump`, проверяет каталог
через `pg_restore --list`, шифрует AES-256-CBC/PBKDF2 и сохраняет SHA-256.
Хранятся последние 7 дней. Signing keys в database backup не входят.

Ручной backup:

```bash
docker compose --env-file .env.staging exec backup /ops/backup-now.sh
```

Проверка: `sha256sum -c <file>.sha256`; затем расшифруйте во временный файл и
выполните `pg_restore --list`. Restore сначала тестируйте в отдельной БД.
Рабочий restore требует:

```bash
BACKUP_FILE=/backups/<file>.aes256 \
CONFIRM_RESTORE=RESTORE-creator_assistant_staging \
docker compose --env-file .env.staging run --rm backup /ops/restore-backup.sh
```

Перед restore остановите API/bot/worker и сделайте новый backup. Удаление
volume или автоматический restore запрещены.
