# Risk Validation

`validate_risk_controls` generates a hashed report using deterministic adverse scenarios for position/trade value, concentration, daily loss, campaign drawdown, liquidity, stale data, provider disagreement, repeated-loss cooldown, strategy suspension, emergency stop, restart reconciliation, and account reconciliation mismatch.

The report records triggers, prevented paper exposure, rejected orders, false-positive candidates, missed-risk candidates, and operator overrides. An override request is audit-recorded with `risk_bypassed=false`; it cannot turn a rejected order into an approval.

The Milestone 7 deterministic suite triggered every expected control with no missed-risk candidate. This proves code paths under synthetic inputs, not calibration quality under real DSE liquidity, volatility, price bands, fees, or operational stress. Independent threshold review and real-market paper evidence remain mandatory.
