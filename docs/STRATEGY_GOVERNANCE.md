# Strategy Governance

Lifecycle states are `draft`, `research`, `paper_candidate`, `paper_active`, `suspended`, `rejected`, and `archived`. Promotion is always explicit and operator-approved.

Candidate and active promotion require a SHA-256 implementation hash, parameters, data requirements, backtest, walk-forward and sensitivity reports, risk review, minimum sample size, actual sample count, and written approval. Campaigns accept only exact `strategy_id@version` registrations in `paper_active`.

Automatic suspension—but never promotion—covers drawdown, data failures, abnormal turnover, repeated risk rejection, behavioral divergence, insufficient liquidity, and implementation-hash change. Paper evidence does not demonstrate profitability.
