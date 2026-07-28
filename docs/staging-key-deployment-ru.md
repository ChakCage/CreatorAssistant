# Ключи staging: инвентаризация, backup и перенос

Временный источник на рабочей станции:
`C:\tmp\creator-assistant-6a-keys`. Он не является постоянным хранилищем и
автоматически не удаляется.

Staging использует только `license-staging.private/public` и
`update-beta.private/public`. `license-production*`, `update-stable*` и
`update-developer*` на VPS staging не переносятся. Public keys встраиваются
в Commercial Staging на build-time; private keys монтируются/передаются через
protected env и никогда не копируются в image.

Инвентаризация выводит только filename, размер, key id и SHA-256 public key.
Private key fingerprint вычисляется по соответствующему public key; содержимое
private key не печатается.

Перед переносом создайте encrypted backup:

```powershell
.\scripts\backup-staging-keys.ps1 `
  -Source C:\tmp\creator-assistant-6a-keys `
  -Output C:\tmp\creator-assistant-staging-keys-backup.dpapi.json
```

Backup защищён Windows DPAPI текущего пользователя. Для VPS передавайте
private keys через SSH/SFTP в root/deploy-owned файл mode 600, импортируйте
значение в protected `.env.staging`, затем безопасно удалите transport copy
только после проверки. Исходные ключи автоматически не удаляются.

Скрипт включает строго четыре файла `license-staging.private/public` и
`update-beta.private/public`; любые production/developer keys игнорируются.
После записи backup он восстанавливает четыре файла во временный каталог,
сверяет SHA-256 с источником и перезаписывает временные private files нулями
перед удалением. В инвентаризации остаются только имена, key id, размеры и
fingerprints публичных ключей.

Emergency rotation: отключить active release, сгенерировать новый staging
key id, сохранить encrypted backup, обновить server secret, пересобрать
Commercial Staging с новым public key, опубликовать новый signed beta manifest,
отозвать sessions при license-key incident и выполнить внешний E2E.
