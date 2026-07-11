# Security

Secrets come from environment variables and `.env` is ignored. Mutating API endpoints require constant-time API-key comparison. CORS is allowlisted, host headers are restricted, inputs are typed, logs redact sensitive-key names, and container processes are non-root where practical.

Before public/network deployment add user authentication with Argon2 password hashing, secure HTTP-only sessions, CSRF tokens for cookie flows, TLS termination, per-user authorization and persistent rate limiting. Rotate a leaked secret immediately and review the audit chain.

