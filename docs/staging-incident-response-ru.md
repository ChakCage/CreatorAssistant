# Реагирование на инциденты staging

1. Ограничьте доступ firewall или остановите затронутый service.
2. Сохраните время, request IDs и redacted logs; secrets не копируйте.
3. При утечке bot token отзовите его в BotFather. При утечке signing key
   отключите releases/activations, создайте новый staging key id и выпустите
   новый Staging build с новым public key.
4. При компрометации БД замените DB/Redis/admin/service secrets и отзовите
   sessions.
5. Восстанавливайте backup только в отдельную test DB, затем с явным
   подтверждением.
6. После исправления выполните verify и внешний E2E.

Production keys/данные не должны находиться на staging, поэтому staging
incident не требует автоматической ротации production без признаков утечки.
