# Known Limitations

- **No Real-Money Broker Execution**: The system is restricted to simulated paper trading. The `OfficialBrokerAdapter` is inactive and throws a runtime exception upon invocation.
- **Provider API Layout Sensitivity**: Scrapers like `bdshare` are sensitive to DSE HTML layout alterations. The dual-source `ReliableDataProvider` and fallback CSV provider are configured to mitigate this risk.
- **Corporate-Action Adjustments**: Dividend and bonus share adjustments depend on inputs supplied in transaction logs or history files.
- **Operator-Grade Security**: The API key mechanism (`X-API-Key`) is designed for local operators and single-process development, not multi-user internet-facing environments.
- **No Leverage or Short Selling**: Transactions and backtests simulate cash-only (long-only) operations without leverage.
