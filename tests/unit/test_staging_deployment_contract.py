from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STAGING = ROOT / "deployment" / "staging"


def test_staging_compose_does_not_publish_database_redis_or_admin_api():
    compose = (STAGING / "docker-compose.yml").read_text(encoding="utf-8")
    assert "postgres:16-alpine" in compose
    assert "redis:7-alpine" in compose
    assert "notification-worker:" in compose
    assert "health-monitor:" in compose
    assert "backup:" in compose
    assert "internal: true" in compose
    postgres = compose.split("  postgres:", 1)[1].split("\n  redis:", 1)[0]
    redis = compose.split("  redis:", 1)[1].split("\n  migrate:", 1)[0]
    assert "ports:" not in postgres
    assert "ports:" not in redis
    caddy = (STAGING / "Caddyfile").read_text(encoding="utf-8")
    assert "@admin path /v1/admin/*" in caddy
    assert "handle @admin" in caddy
    assert "respond 404" in caddy
    assert "@installer path_regexp installer ^/downloads/CreatorAssistant-Commercial-Staging-" in caddy
    assert "uri strip_prefix /downloads" in caddy
    assert "handle @allowed" in caddy
    assert "handle @installer" in caddy


def test_staging_compose_maps_release_metadata_to_each_settings_prefix():
    compose = (STAGING / "docker-compose.yml").read_text(encoding="utf-8")
    backend_env = compose.split("x-backend-env:", 1)[1].split("x-bot-env:", 1)[0]
    bot_env = compose.split("x-bot-env:", 1)[1].split("services:", 1)[0]
    assert "CREATOR_RELEASE_VERSION: ${CREATOR_RELEASE_VERSION" in backend_env
    assert "CREATOR_RELEASE_COMMIT: ${CREATOR_RELEASE_COMMIT" in backend_env
    assert "CREATOR_BOT_RELEASE_VERSION: ${CREATOR_RELEASE_VERSION" in bot_env
    assert "CREATOR_BOT_RELEASE_COMMIT: ${CREATOR_RELEASE_COMMIT" in bot_env


def test_staging_secrets_template_contains_no_values_or_private_keys():
    template = (STAGING / ".env.staging.example").read_text(encoding="utf-8")
    for name in (
        "POSTGRES_PASSWORD", "REDIS_PASSWORD", "LICENSE_ACTIVATION_PEPPER",
        "STAGING_LICENSE_PRIVATE_KEY_FILE", "LICENSE_ADMIN_TOKEN_HASH",
        "LICENSE_BOT_SERVICE_SECRET", "CREATOR_BOT_TOKEN",
        "CREATOR_BOT_WEBHOOK_SECRET", "BACKUP_ENCRYPTION_KEY",
    ):
        assert f"{name}=\n" in template
    assert "BEGIN PRIVATE KEY" not in template
    compose = (STAGING / "docker-compose.yml").read_text(encoding="utf-8")
    assert "LICENSE_SIGNING_PRIVATE_KEY_FILE: /run/secrets/license-signing-private-key" in compose
    assert "file: ${STAGING_LICENSE_PRIVATE_KEY_FILE}" in compose
    assert "${STAGING_RELEASES_DIRECTORY}:/srv/releases:ro" in compose


def test_dpapi_backup_is_strictly_limited_to_staging_keys_and_verifies_restore():
    script = (ROOT / "scripts" / "backup-staging-keys.ps1").read_text(encoding="utf-8")
    assert '"license-staging.private"' in script
    assert '"update-beta.private"' in script
    assert "production-do-not-deploy-to-staging" not in script
    assert "DPAPI restore verification passed" in script
    assert "Get-FileHash" in script


def test_beta_semver_is_mapped_to_numeric_windows_installer_version():
    builder = (ROOT / "scripts" / "build-installers.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "installer" / "CreatorAssistant.iss").read_text(encoding="utf-8")
    assert "$WindowsVersion" in builder
    assert "/DWindowsVersion=$WindowsVersion" in builder
    assert "VersionInfoVersion={#WindowsVersion}" in installer


def test_restore_requires_explicit_database_specific_confirmation():
    restore = (STAGING / "ops" / "restore-backup.sh").read_text(encoding="utf-8")
    assert 'CONFIRM_RESTORE:-' in restore
    assert "RESTORE-${PGDATABASE}" in restore
    assert "sha256sum -c" in restore
    assert "pg_restore --list" in restore
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "*.sh text eol=lf" in attributes
    compose = (STAGING / "docker-compose.yml").read_text(encoding="utf-8")
    assert "./ops:/ops:ro" not in compose
    for name in ("Dockerfile.backup", "Dockerfile.monitor"):
        dockerfile = (STAGING / "ops" / name).read_text(encoding="utf-8")
        assert "sed -i 's/\\r$//'" in dockerfile


def test_deploy_and_rollback_require_manual_confirmation():
    deploy = (ROOT / "scripts" / "deploy-staging.ps1").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts" / "rollback-staging.ps1").read_text(encoding="utf-8")
    assert "ConfirmDeploy" in deploy
    assert "Refusing to deploy a dirty worktree" in deploy
    assert "ConfirmRollback" in rollback
    assert "verify-staging.ps1" in deploy
    assert "rollback-staging.ps1" in deploy
    assert "docker compose --env-file .env.staging build" in rollback
    verify = (ROOT / "scripts" / "verify-staging.ps1").read_text(encoding="utf-8")
    assert '.Replace("`r`n", "`n")' in deploy
    assert '.Replace("`r`n", "`n")' in rollback
    assert '.Replace("`r`n", "`n")' in verify


def test_telegram_webhook_is_bound_to_secret_and_not_mutated_at_every_start():
    main = (ROOT / "services" / "creator_assistant_bot" / "bot" / "main.py").read_text(encoding="utf-8")
    assert "secret_token=settings.webhook_secret" in main
    assert "if settings.manage_webhook:" in main
    assert "delete_webhook" not in main
    explicit = (STAGING / "ops" / "set-webhook.sh").read_text(encoding="utf-8")
    assert "webhook_readiness.py" in explicit
    assert "--repair --force-set" in explicit
    assert "deleteWebhook" not in explicit


def test_deploy_requires_operational_webhook_pre_and_post_checks():
    deploy = (ROOT / "scripts" / "deploy-staging.ps1").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify-staging.ps1").read_text(encoding="utf-8")
    readiness = (STAGING / "ops" / "webhook_readiness.py").read_text(encoding="utf-8")
    assert "--phase pre --repair --clear-stale-error" in deploy
    assert "--phase post --repair --force-set --clear-stale-error --require-empty" in deploy
    assert "bot_health" in deploy
    assert "--phase verify --require-empty" in verify
    assert '"drop_pending_updates": "false"' in readiness
    assert '"deleteWebhook", {"drop_pending_updates": "false"}' in readiness
    assert "last_error_message" in readiness
    assert "pending_update_count is growing" in readiness
    assert "X-Telegram-Bot-Api-Secret-Token" in readiness


def test_monitor_checks_webhook_url_error_queue_and_runtime_endpoint():
    monitor = (STAGING / "ops" / "monitor-loop.sh").read_text(encoding="utf-8")
    compose = (STAGING / "docker-compose.yml").read_text(encoding="utf-8")
    for marker in (
        "telegram-webhook-url",
        "telegram-webhook-last-error",
        "telegram-webhook-queue-growing",
        "telegram-webhook-operational-readiness",
    ):
        assert marker in monitor
    assert "CREATOR_BOT_WEBHOOK_SECRET: ${CREATOR_BOT_WEBHOOK_SECRET}" in compose
