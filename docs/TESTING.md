# Testing

Backend tests cover provider normalization/errors, stale/conflicting data, duplicate CSV records, portfolio accounting, immutable imports, delayed backtest fills, costs, walk-forward integrity, risk limits, emergency stop, duplicate orders, stale approval, partial/no fill, insufficient balances, authentication, health and API backtests.

Run `python -m pytest`, `ruff check .`, `mypy app`, `npm run typecheck` and `npm run build`. No test requires live DSE connectivity; mock and CSV fixtures are deterministic.

The verified milestone results are recorded in [VERIFICATION.md](VERIFICATION.md).
