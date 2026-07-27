# Checklist выпуска закрытой беты

- [ ] Git status чистый; версия и release notes обновлены.
- [ ] Desktop/backend/bot tests пройдены.
- [ ] Три onedir EXE и три независимых installer собраны.
- [ ] SHA256SUMS рассчитан после Authenticode signing.
- [ ] Release manifests подписаны отдельными update keys.
- [ ] Secrets/OAuth/private keys/user media не включены.
- [ ] Production Commercial не доверяет staging update/license keys.
- [ ] Commercial не содержит Autopilot/Publishing/OAuth.
- [ ] Developer не содержит licensing.
- [ ] Clean install, parallel install, upgrade и uninstall проверены.
- [ ] Обновление сохраняет projects/settings/license/brand assets/models.
- [ ] Support ZIP проверен на отсутствие секретов.
- [ ] SmartScreen/UNSIGNED BETA и known issues документированы.
- [ ] Proxy recovery regression из `6663601` пройден.
