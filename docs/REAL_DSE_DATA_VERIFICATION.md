# Real DSE Data Verification

Run `scripts\verify_real_dse_data.ps1 -Provider bdshare -Symbol GP` only with network access and explicit operator intent. It records redacted responses, capabilities, normalization failures and timestamp classification under `data/reports/provider_diagnostics`. Receipt time is never promoted to exchange time; a quote without trustworthy exchange time cannot authorize a paper order. Public endpoint availability remains externally controlled.

## 2026-07-13 smoke-test findings

- `bdshare`: installed contract loaded, but `dsebd.org` failed TLS certificate verification and the fallback `dsebd.com.bd` failed DNS resolution. Symbols, quote, history, summary and DSEX were therefore unavailable. No timestamp was accepted.
- `bdfinance`: adapter contract loaded, but the external package was not installed in the active virtual environment. Its 0.x contract explicitly lacks public symbol-list and market-summary methods. No timestamp was accepted.
- Redacted machine-readable reports are retained in `data/reports/provider_diagnostics`. These failures block trustworthy real-data operation but do not affect deterministic offline tests.
