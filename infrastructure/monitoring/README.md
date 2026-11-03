# Operational monitoring

`docker compose ps` reports database/Redis/API health. `docker compose logs -f --since 1h api worker inventory scheduler caddy` streams JSON HTTP access logs and application/queue diagnostics. Docker rotates every service's logs at 5 × 20 MB; configure a host log shipper (Vector/Loki/Filebeat) to export these logs to your existing platform. Runtime metrics and node failures are displayed by the authenticated dashboard; `/health` is the server health probe.

Do not expose PostgreSQL, Redis, or API container ports publicly. An external HTTPS uptime monitor can poll `/health`. Alert on failed probes, worker crash loops, queue growth, database disk use, and backup age. A reverse proxy health probe is not proof that a Windows node or Steam is operational.

Database node-metric/notification retention defaults to 30 days through RETENTION_DAYS; audit retention is 365 days. Backups should be encrypted and copied off-host; test a restore on an isolated server periodically. Backup scripts retain files until an operator rotates them.
