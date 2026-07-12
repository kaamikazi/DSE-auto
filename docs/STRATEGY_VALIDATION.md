# Strategy Validation

Milestone 2 reports Sharpe, Sortino, Calmar, maximum drawdown, drawdown duration, expectancy, profit factor, turnover and benchmark alpha. Walk-forward splits and parameter sensitivity generate per-symbol HTML/JSON evidence for GP, SQURPHARMA, BRACBANK, BATBC, ACI, RENATA, CITYBANK and BEXIMCO when usable data exists; missing data is reported rather than inferred. These reports are not profitability claims.

Initial research instruments are GP, SQURPHARMA, BRACBANK, BATBC, ACI, RENATA, CITYBANK and BEXIMCO. They are test instruments, not recommendations.

## Extended Performance Metrics
Every backtest computes the following metrics to assess viability:
- **Profit Factor**: Ratio of gross profits to gross losses (gross profits / gross losses).
- **Expectancy**: Expected P&L per trade (win rate * average win - loss rate * average loss).
- **Turnover Rate**: Traded volume divided by starting capital.
- **Benchmark Alpha**: Excess annualized return of the strategy compared to DSEX index returns.
- **Drawdown Duration**: Longest consecutive number of bars the portfolio equity stays below its historical peak.

## Walk-Forward & Parameter Sensitivity
- **Walk-Forward Validation**: Partitions historical data into sequential splits (train, validation, test) to simulate walk-forward performance.
- **Parameter Sensitivity Matrix**: Evaluates strategy performance (MA Crossover fast/slow parameters: 10/30, 20/50, 30/100) and outputs returns, Sharpe ratios, and max drawdowns for comparison.

## Report Generation
Backtests generate structured outputs:
- **JSON**: Detailed metrics, transaction histories, and equity curves.
- **HTML**: Self-contained, styled operator reports featuring metric tables.
- All backtests enforce next-bar execution rules to prevent same-bar look-ahead bias.
- Results are fixture-based research instruments; do not claim profitability.
