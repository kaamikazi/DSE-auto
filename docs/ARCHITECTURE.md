# Architecture

The monorepo separates the Next.js operator UI from the FastAPI domain/API. Providers normalize external data before validation and persistence. Portfolio state is derived from immutable transactions. Signals may create proposals, but the deterministic risk engine must approve them both before and after human approval. Only then may the paper broker simulate fills. Audit events form a SHA-256 hash chain.

Critical flow: `provider -> normalize -> validate -> signal -> proposal -> risk -> human approval -> revalidate -> paper broker -> fill -> portfolio/audit`.

PostgreSQL is the production database; SQLite is the local fallback. Redis is optional and is not a correctness dependency. A missing optional service degrades capability rather than bypassing controls.

