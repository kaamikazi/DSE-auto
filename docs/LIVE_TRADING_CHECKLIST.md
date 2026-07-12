# Live Trading Checklist

Milestone 3 remains paper-only. Real money stays blocked until provider timestamp reliability, long-run evidence, reconciliation, fill assumptions, calendar governance, restore drills and soak failures receive independent review. Passing this checklist does not enable a broker adapter.

All items require written sign-off:

- At least 60 DSE trading days of representative paper results
- No unresolved critical defects
- Official broker API documentation and explicit broker permission
- Successful holdings/cash/open-order reconciliation tests
- Duplicate-order and restart-recovery tests
- Kill-switch and stale/conflicting-data tests
- Security review, secret rotation and audit-storage durability
- Small-capital pilot with strict caps
- Manual approval for every pilot order
- Written acceptance of financial and operational risk

Milestone 1 cannot be enabled for live trading even if these boxes are checked; code and configuration changes plus a formal release are required.
