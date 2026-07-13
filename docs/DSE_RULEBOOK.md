# DSE Rulebook Configuration

Each immutable version contains timezone, weekly days, sessions, auctions, holidays, tick sizes, price bands, settlement, short-selling and leverage policies, minimum quantity, expiry, fee/tax assumptions, and liquidity thresholds.

Statuses are `assumed`, `partially_verified`, `verified`, and `deprecated`. A version records effective date, source, operator approval, change history, and deterministic SHA-256 hash. Corrections create a new version.

Campaigns lock the exact rule-set ID. A controlling campaign rejects rule replacement. Accelerated verification uses explicitly `assumed` rules and makes no official DSE-verification claim.

Before real money, every material rule requires current authoritative DSE, BSEC, CDBL, calendar, broker, and tax verification by a qualified operator.
