# Timestamp Trust Policy

Normalized quotes and bars declare one provenance:

- `exchange_verified`: independently verified exchange time; acceptable.
- `operator_attested`: operator-approved imported timestamp; acceptable for paper validation.
- `provider_asserted`: provider claim without independent exchange verification; blocked.
- `receipt_only`: local receipt time substituted for missing market time; blocked.
- `unknown`: provenance absent; blocked.

Proposal and approval both re-fetch the provider quote and fail closed unless provenance is `exchange_verified` or `operator_attested`. Receipt time is never fabricated as exchange time.
