# Database source-of-truth policy

## Canonical roles

| Role | Purpose | Canonical location |
|---|---|---|
| `operational` | Local paper-trading and governance state | `backend/data/dse_autotrader.db` |
| `research` | Large derived research candidates | An explicitly named database below `reports/research_data_quality/` |
| `test` | Unit and focused integration tests | A process-unique database below the operating-system temporary directory |
| `recovery` | Read-only restore and recovery verification | An explicitly named file below `reports/recovery/` or `data/backups/` |
| `postgres_verification` | PostgreSQL migration/integration verification | The explicitly configured PostgreSQL database name/alias |
| `simulation` | Accelerated or fault-injection exercises | A run-specific non-operational database |

The operational SQLite location is resolved relative to the backend directory, never the
caller's current working directory. PostgreSQL development and test databases are distinct
logical targets. Their availability and identity must be measured; an offline instance must
be reported as unavailable, never inferred from old evidence.

Every process declares `DATABASE_ROLE`. A test or simulation process that resolves to the
canonical operational SQLite file refuses startup unless the operator supplies the explicit
`ALLOW_DATABASE_ROLE_OVERRIDE=true` exception. Tests use a PID-qualified temporary SQLite
file, preventing focused and full suites from sharing mutable state.

## Required identity disclosure

Every CLI, script, test run, or evidence report records the database role, absolute SQLite
path or redacted connection alias, environment, migration revision, and active audit-chain
ID. SQLite evidence also records a SHA-256 fingerprint; PostgreSQL evidence records its
database name. Backups, restored databases, test files, and research candidates remain
separate. They must not be merged, deleted, or promoted merely because their schemas match.

## Resolved count discrepancy

The prior `sqlite:///./data/dse_autotrader.db` was current-working-directory relative. A
backend-launched command used the populated backend database, while a repository-root command
could create or inspect an empty shadow database at `data/dse_autotrader.db`. In addition,
some earlier zero-count statements described an isolated test database or the prospective
scope of an approval pack without carrying database provenance. Those statements cannot be
reconciled as global operational counts and are classified `legacy_unverified`.

The preserved operational database contains 3 campaigns, 5 sessions, 5 orders, and 2
transactions. Their presence predates this audit; this work does not create, delete, merge,
or relabel those rows.
