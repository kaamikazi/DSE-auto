# Paper Trading Guide

Keep `TRADING_MODE=paper` and `LIVE_TRADING_ENABLED=false`. Review Data Health, scheduler failures and pending approvals before operating. Telegram `/approve` consumes its token once and rechecks timestamp, provider trust, duplicates and risk. Use `/pause` for a controlled halt and `/emergency_stop` whenever integrity is uncertain.

1. **Portfolio Setup**: Import initial transactions via Next.js dashboard CSV upload or `/portfolio/transactions` API endpoint to initialize cash and holdings.
2. **Persistent Background Execution**: The scheduler runs continuously, polling quotes, updating the DSEX index, checking for news, and scanning for strategy signals.
3. **Supervised Signal Generation**: When a strategy triggers a buy/sell signal, it generates a proposed order. If approved by the pre-trade risk engine, it enters `awaiting_approval` and generates a 6-character, 15-minute expiring approval token.
4. **Telegram Supervisor Approvals**:
   - The bot posts a message to the operator chat with order details and the token (e.g., `A9B8C7`).
   - Send `/approve A9B8C7` to authorize execution, or `/reject A9B8C7` to cancel the proposal.
   - Upon receipt of `/approve`, the system fetches a fresh quote, re-checks risk limits, and routes the order to the Paper Broker.
5. **Paper Broker Execution**: Simulates slippage, spreads, partial fills, and updates cash/positions. Reconciled transaction entries are logged in the immutable audit trail.
6. **System Status Monitoring**:
   - Send `/status` or `/portfolio` via Telegram to fetch live metrics.
   - Review active jobs, provider circuit breaker states, and fresh disagreement ratios on the Next.js dashboard.
7. **Database Administration**:
   - Run `.\scripts\backup.ps1` to create timestamped database snapshots.
   - Run `.\scripts\restore.ps1 -BackupFile <path>` to restore the database state.
