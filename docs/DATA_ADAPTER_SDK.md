# Data Adapter SDK

`DataAdapter` extends the normalized provider interface with a declarative `AdapterDescriptor` and health record. Every adapter must declare capabilities, timestamp source/trust, update frequency, estimated latency, licensing status, authentication method, rate limits, documentation, and current health.

Capabilities cover streaming and polling quotes, history, DSEX, depth, corporate actions, and price-sensitive news. An adapter must return normalized `Quote`/`HistoricalBar` objects and preserve timestamp provenance as `exchange_verified`, `provider_asserted`, `operator_attested`, `receipt_only`, or `unknown`.

`FakeCertifiedFeedAdapter` implements the complete contract deterministically for tests. Its license is `test_only`; outside `APP_ENV=test` it is unsuitable for signals or order approval. It must never be configured as proof of a real market feed.

New vendor adapters must include contract fixtures, license/auth review, rate-limit behavior, source-clock interpretation, health degradation, and explicit order-approval suitability. Scraping, TLS verification bypass, inferred exchange timestamps, and undeclared source substitution are forbidden.
