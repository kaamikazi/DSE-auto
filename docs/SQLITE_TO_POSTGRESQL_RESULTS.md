# SQLite-to-PostgreSQL Results

Status: **real isolated PostgreSQL verification** on 2026-07-15.

The operational SQLite source was read-only and retained SHA-256 `EF33D63E0E8804998B4BF865E2D64A736DF46689951587552B9A9D6E582BC403` before and after. The successful isolated destination was `dse_m8_migration_20260715_004023`; the primary `dse_autotrader` database was not overwritten.

The copy matched all 33 mapped tables, record counts, normalized deterministic row hashes, 2 foreign keys, and cross-dialect uniqueness semantics. Canonical audit was valid with 535 imported audit records, including 137 canonical and 398 legacy records at copy time. Campaigns, days, incidents, reviews, qualification, rule sets, fee profiles, strategy registrations, and legacy archive metadata were preserved.

Two defects were found and fixed: timezone-aware PostgreSQL timestamps initially produced false hash mismatches, and explicit integer ID copies initially left PostgreSQL sequences behind existing rows. The final run synchronized and verified 10 sequences. Earlier failed isolated databases were preserved as diagnostic evidence and were not used for final runtime verification.

