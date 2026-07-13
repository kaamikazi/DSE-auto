# Security Hardening

The application binds to `127.0.0.1` by default. Compose may listen on all interfaces inside a container, but publishes only to localhost. Public exposure, reverse proxies, and remote access are outside the approved configuration.

Operator and reviewer credentials are distinct. API keys use constant-time comparison; optional bearer sessions store only token hashes, expire after `SESSION_TTL_SECONDS`, and can be revoked. Failed logins are fingerprinted and throttled within a bounded window. Reviewers may read audit/operational evidence and submit review attestations, but operator mutations require the operator role.

Production startup refuses SQLite, missing Redis, embedded scheduling, weak/default secrets, equal operator/reviewer secrets, live trading, or unsupported broker adapters. `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled` remain mandatory.

Run:

```powershell
backend\.venv\Scripts\python.exe scripts\security_preflight.py
powershell -ExecutionPolicy Bypass -File scripts\check_config_permissions.ps1
```

Milestone 7 secret scanning found no tracked literal secret. `.env` inheritance was removed; its ACL is restricted to the operator, Administrators, SYSTEM, and the Codex sandbox workspace group. Backup scripts similarly restrict output ACLs. Recheck ACLs after copying the repository or restoring to another machine.

## Dependency review

Node dependencies and overrides are exact in `package.json`/lockfile; `npm audit` reported zero vulnerabilities. Python dependencies use bounded compatibility ranges rather than a fully hashed lock; PostgreSQL/Redis clients are bounded to major versions. A Python advisory scanner and reproducible hashed production lock are still required before internet-facing or real-money consideration.

No Selenium, browser automation, OTP/CAPTCHA handling, unofficial broker execution, AI approval, or TLS verification bypass is present.
