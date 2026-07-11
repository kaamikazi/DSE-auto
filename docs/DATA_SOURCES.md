# Data Sources

`MarketDataProvider` defines symbols, quotes, history, summary, index, company, P/E, depth, news, price-sensitive news and health contracts.

- `bdshare`: primary optional public-data adapter; lazy import, normalized output, rate/caching must be configured by operators.
- `bdfinance`: secondary optional adapter. Its public API is version-sensitive and must pass health/contract tests before use.
- `CSVProvider`: guaranteed offline fallback. Files use `timestamp,open,high,low,close,volume,trade_count,turnover`.
- `MockProvider`: deterministic, network-free tests and demonstrations only.

Missing fields stay `null`; they are never invented. Current quote collection should be rate-limited by the scheduler in production.

