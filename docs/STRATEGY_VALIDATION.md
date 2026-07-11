# Strategy Validation

Initial research instruments are GP, SQURPHARMA, BRACBANK, BATBC, ACI, RENATA, CITYBANK and BEXIMCO. They are test instruments, not recommendations.

Validate buy-and-hold, 20/50 MA, momentum with DSEX filter and volume breakout across multiple windows. Include fees/slippage, inspect parameter sensitivity, and maintain ordered train/validation/untouched-test partitions. Signals execute on the next bar to prevent same-bar look-ahead. A test result may not tune the same strategy version.

Record missing corporate actions, liquidity assumptions, constituent history, failed fills and provider warnings with every report.

