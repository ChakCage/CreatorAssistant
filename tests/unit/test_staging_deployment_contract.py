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
    assert "respond @admin 404" in caddy


def test_staging_secrets_template_contains_no_values_or_private_keys():
    template = (STAGING / ".env.staging.example").read_text(encoding="utf-8")
    for name in (
        "POSTGRES_PASSWORD", "REDIS_PASSWORD", "LICENSE_ACTIVATION_PEPPER",
        "LICENSE_SIGNING_PRIVATE_KEY", "LICENSE_ADMIN_TOKEN_HASH",
        "LICENSE_BOT_SERVICE_SECRET", "CREATOR_BOT_TOKEN",
        "CREATOR_BOT_WEBHOOK_SECRET", "BACKUP_ENCRYPTION_KEY",
    ):
        assert f"{name}=\n" in template
    assert "BEGIN PRIVATE KEY" not in template


def test_restore_requires_explicit_database_specific_confirmation():
    restore = (STAGING / "ops" / "restore-backup.sh").read_text(encoding="utf-8")
    assert 'CONFIRM_RESTORE:-' in restore
    assert "RESTORE-${PGDATABASE}" in restore
    assert "sha256sum -c" in restore
    assert "pg_restore --list" in restore


def test_deploy_and_rollback_require_manual_confirmation():
    deploy = (ROOT / "scripts" / "deploy-staging.ps1").read_text(encoding="utf-8")
    rollback = (ROOT / "scripts" / "rollback-staging.ps1").read_text(encoding="utf-8")
    assert "ConfirmDeploy" in deploy
    assert "Refusing to deploy a dirty worktree" in deploy
    assert "ConfirmRollback" in rollback
    assert "verify-staging.ps1" in deploy
    assert "rollback-staging.ps1" in deploy
    assert "docker compose --env-file .env.staging build" in rollback


def test_telegram_webhook_is_bound_to_secret_and_not_mutated_at_every_start():
    main = (ROOT / "services" / "creator_assistant_bot" / "bot" / "main.py").read_text(encoding="utf-8")
    assert "secret_token=settings.webhook_secret" in main
    assert "if settings.manage_webhook:" in main
    assert "delete_webhook" not in main
    explicit = (STAGING / "ops" / "set-webhook.sh").read_text(encoding="utf-8")
    assert "DELETE_OLD_WEBHOOK" in explicit
    assert "secret_token=${CREATOR_BOT_WEBHOOK_SECRET}" in explicit
