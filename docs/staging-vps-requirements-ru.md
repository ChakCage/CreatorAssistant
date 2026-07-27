# VPS для закрытой беты

Минимум: Linux x86_64, 2 vCPU, 4 ГБ RAM, 60 ГБ SSD, публичный IPv4/IPv6,
Ubuntu 24.04 LTS или Debian 12, Docker Engine 27+ и Compose v2. Должны быть
открыты только SSH, TCP 80 и 443. PostgreSQL и Redis наружу не публикуются.

Рекомендуется 4 vCPU, 8 ГБ RAM и 100 ГБ SSD. Включите автоматические security
updates, отдельного непривилегированного deploy-пользователя, SSH-ключи,
firewall и синхронизацию времени. Каталог `/opt/creator-assistant-staging`
должен вмещать releases, volumes и минимум семь ежедневных backup.

Production на этот VPS не разворачивается. Staging использует отдельные bot,
БД, Redis и ключи.
