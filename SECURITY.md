# Security Policy

## Scope

DSE AutoTrader is a local, paper-trading research project. Live broker execution is not implemented. The project is not approved for public internet deployment, custody of credentials, or real-money trading.

## Reporting a vulnerability

Use GitHub's private security-advisory reporting channel for this repository. Do not disclose an exploitable vulnerability in a public issue or pull request.

Never submit any of the following to GitHub issues, discussions, pull requests, logs, screenshots, or test fixtures:

- broker usernames or credentials
- passwords, PINs, OTPs, recovery codes, or session cookies
- API keys, Telegram bot tokens, or private chat IDs
- BO/account numbers, investor identifiers, or personal identity records
- account statements or transaction histories
- holdings, cash balances, dividends, bonus-share records, or real portfolio details
- private evidence files, database dumps, backups, audit archives, or recovery bundles

If sensitive information has been exposed, revoke or rotate it immediately and contact the relevant provider. Deleting a file from the latest commit does not remove it from Git history.

## Supported security posture

- `TRADING_MODE=paper`
- `LIVE_TRADING_ENABLED=false`
- `BROKER_ADAPTER=disabled`
- loopback-only service bindings
- local operator access

Requests to enable live execution, bypass TLS verification, automate broker authentication, or handle OTP/CAPTCHA flows are outside the supported security model.
