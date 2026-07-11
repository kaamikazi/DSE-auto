# Paper Trading Guide

1. Import transactions or begin with the paper cash account.
2. Fetch and compare a quote; resolve stale/conflicting data.
3. Generate a versioned signal or create a proposal.
4. Review quantity, limit price, fees, slippage, freshness, agreement and stop plan.
5. Approve; the system repeats risk and freshness checks.
6. Execute only against the paper broker with available market volume.
7. Review fills, positions, cash and the audit trail.

Market orders are unavailable. Partial fills, spread/slippage, liquidity caps and insufficient balances are simulated. Emergency stop blocks proposals until reconciliation and audit verification pass.

