# Локальная проверка лицензии Commercial

Этот сценарий предназначен только для разработки. Он не подключает оплату, Telegram-бота или внешний API. Production-сборка получает URL backend и открытые ключи Ed25519 на этапе сборки; пользователь не может заменить их через `settings.json`.

## 1. Секреты тестового окружения

Создайте отдельные случайные значения. Никогда не коммитьте их:

```powershell
$env:LICENSE_ENV = 'local'
$env:LICENSE_DATABASE_URL = 'postgresql+psycopg://creator_assistant:creator_assistant@postgres:5432/creator_assistant'
$env:LICENSE_REDIS_URL = 'redis://redis:6379/0'
$env:LICENSE_ACTIVATION_PEPPER = '<случайная строка не короче 32 символов>'
$env:LICENSE_SIGNING_KEY_ID = 'local-e2e-1'
$env:LICENSE_SIGNING_PRIVATE_KEY = '<base64url от 32 случайных байтов>'
$env:LICENSE_ADMIN_TOKEN_HASH = '<sha256 от тестового admin token>'
```

Hash admin token можно получить безопасной локальной командой (она не пишет token в базу):

```powershell
python -m app.admin_cli create-admin --token '<временный token>'
```

## 2. Backend и чистая PostgreSQL

Из `services/licensing_backend`:

```powershell
docker compose up -d postgres redis
docker compose run --rm api alembic upgrade head
docker compose up -d api
Invoke-RestMethod http://127.0.0.1:18080/health
Invoke-RestMethod http://127.0.0.1:18080/ready
```

Порт API опубликован только на `127.0.0.1:18080`.

## 3. Тестовая подписка и одноразовый код

```powershell
$headers = @{ 'X-Admin-Token' = '<временный token>' }
$user = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18080/v1/admin/users -Headers $headers -ContentType application/json -Body '{"email":"local-e2e@example.test"}'
$grantBody = @{ user_id=$user.id; plan_code='beta'; days=30; reason='local-e2e'; idempotency_key='local-e2e-grant-1' } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18080/v1/admin/subscriptions/grant -Headers $headers -ContentType application/json -Body $grantBody
$codeBody = @{ user_id=$user.id; ttl_minutes=30; reason='local-e2e' } | ConvertTo-Json
$code = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18080/v1/admin/activation-codes -Headers $headers -ContentType application/json -Body $codeBody
$code.activation_code
```

Код показывается только в этом ответе; в PostgreSQL хранится HMAC-hash.

## 4. Commercial Dev-сборка

Получите public key из `GET /v1/licenses/keys`, затем передайте профиль на этапе сборки:

```powershell
$env:CREATOR_ASSISTANT_LICENSE_PROFILE = 'local'
$env:CREATOR_ASSISTANT_LICENSE_URL = 'http://127.0.0.1:18080'
$env:CREATOR_ASSISTANT_LICENSE_PUBLIC_KEYS = '{"local-e2e-1":"<public key base64url>"}'
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Edition commercial
```

Обычная поставляемая Commercial-сборка должна собираться с production-профилем и HTTPS URL.

## 5. Ручной сценарий

1. Запустите Commercial с чистым профилем Windows.
2. Убедитесь, что справка и AI-onboarding открываются, а новый анализ заблокирован.
3. Откройте «Лицензия», введите одноразовый код и проверьте тариф `beta` и срок.
4. Создайте Shorts-проект и запустите AI-анализ.
5. Перезапустите EXE: лицензия должна сохраниться в Windows Credential Manager.
6. Остановите backend. До `offline_grace_until` платные операции разрешены с предупреждением.
7. Отключите текущее устройство. Новые платные операции должны блокироваться после онлайн-проверки/окончания короткого entitlement.
8. Выдайте новый одноразовый код и активируйте снова.
9. Запустите Developer Edition: она работает без backend и вообще не создаёт license client.

Существующие проекты и MP4 не удаляются при истечении подписки. Диагностика показывает только статус, даты, backend URL, error code и request ID — entitlement/refresh credentials туда не попадают.

## 6. Завершение

```powershell
docker compose down
```

Не используйте `down -v`, если хотите сохранить локальную тестовую базу.
