# Licensed DSE Data Vendor Onboarding

The vendor must answer and evidence the following before technical certification:

- Legal entity, DSE authorization, subscriber/use/redistribution rights, retention limits, audit rights, and termination/export terms.
- Quote/trade/index/depth/corporate-action schemas; identifiers; currencies; units; null/correction semantics; and change-management notice.
- Exchange event timestamp source, timezone, precision, sequence number, receipt timestamp, clock synchronization, correction/cancel handling, and provenance warranty.
- Expected/maximum latency by channel, freshness SLA, availability SLA, incident notice, planned maintenance, and support escalation.
- Symbol/DSEX coverage, listing/delisting lifecycle, historical depth, survivorship policy, and backfill/correction procedure.
- Corporate actions, dividends, splits/bonuses/rights, suspensions, trading-status changes, price limits, and market-calendar coverage.
- Level-1/market-depth fields, snapshot/incremental semantics, ordering, duplicates, gaps, and reconnect/replay behavior.
- Authentication (API key/OAuth2/mTLS/vendor SDK), secret rotation, IP restrictions, sandbox credentials, and least privilege.
- Rate/burst limits, quotas, retry guidance, concurrent connections, and permitted caching.
- Production-like test environment, fixtures, outage/reconnect testing, certification contacts, and signed acceptance criteria.

Restrictions must never be bypassed. TLS verification remains enabled; custom CAs must be explicitly configured and validated. No evaluation, scraped, test-only, or unknown-license feed may become operational.
