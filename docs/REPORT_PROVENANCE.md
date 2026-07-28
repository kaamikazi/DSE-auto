# Report provenance standard

Generated JSON, CSV manifests, Markdown, and HTML evidence reports must expose a provenance
header containing:

- report ID and UTC generation timestamp;
- Git HEAD and application version;
- database role, engine, absolute path/redacted alias, fingerprint, and PostgreSQL database
  name where applicable;
- Alembic migration revision, active canonical audit-chain ID, canonical event count, and
  legacy archive count;
- contributing dataset IDs;
- rule-set, fee-profile, and strategy versions;
- execution mode and environment.

CSV outputs carry the same fields as `provenance_*` columns or are covered by a hashed CSV
manifest that contains them. JSON uses a top-level `provenance` object. Markdown and HTML
place the provenance table before substantive findings.

A report lacking any required field is `legacy_unverified`. This classification does not
mean the underlying file is false or disposable: preserve it unchanged, retain its hash, and
use it only after independently establishing its database, code, audit, and dataset context.
Do not backfill guessed provenance into historical reports.

Report generation is read-only with respect to trading and activation state unless a report
type has a separately documented audit-recording step. Provenance never constitutes approval.
The permanent execution posture is paper-only, live trading disabled, and broker adapter
disabled.
