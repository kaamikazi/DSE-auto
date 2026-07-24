## Summary

Describe the change and why it is needed.

## Safety checklist

- [ ] `TRADING_MODE=paper`, `LIVE_TRADING_ENABLED=false`, and `BROKER_ADAPTER=disabled` remain enforced.
- [ ] No live broker execution, authentication automation, OTP/CAPTCHA handling, or TLS bypass was added.
- [ ] No credentials, account identifiers, statements, portfolio data, evidence, databases, reports, logs, backups, or audit archives are included.
- [ ] Tests use deterministic or explicitly sanitized public fixtures.
- [ ] Infrastructure-dependent tests are reported accurately as passed, failed, or skipped.
- [ ] Backend and frontend verification relevant to this change passed.

## Verification

List exact commands and results. Do not include secret values or private operational output.
