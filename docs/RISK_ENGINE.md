# Risk Engine

Risk rules are deterministic and versioned (`1.0.0`). Milestone 1 enforces non-healthy kill switch, stale/unsafe data, provider disagreement, trade value, quantity, concentration, daily/open-order counts, liquidity, symbol restrictions, price deviation and spread.

Every decision contains outcome, codes, readable reasons, input snapshot, version and timestamp. Every rejection is audited. Risk uncertainty fails closed. Additional daily/weekly loss, drawdown, sector, turnover, cancellation and cooldown limits are required before a live pilot.

