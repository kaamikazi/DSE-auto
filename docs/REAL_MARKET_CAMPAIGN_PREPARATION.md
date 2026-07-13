# 60-Day Real-Market Campaign Preparation

Use `config/real_market_campaign_template.json`; do not edit synthetic/imported campaigns into real-market campaigns. A new campaign must use `evidence_class=real_market`, a passing licensed-provider certification ID, exchange-verified timestamps, an approved symbol universe, governed strategy versions, versioned DSE rules/fees, and named daily reviewers.

Universe approval records listing status, liquidity/risk rationale, corporate-action status, provider coverage, and operator/reviewer sign-off. Each day requires data, signal/rejection, proposal/fill, risk-intervention, incident, reconciliation, audit, backup, and independent-review evidence. Weekly review covers drift, data quality, execution assumptions, risk controls, incidents, missed/rejected trades, fees/slippage, and qualification status without profitability claims.

Pause on provider/certification/timestamp, scheduler/worker, audit, reconciliation, backup, rule, risk, or critical-incident failures. Invalidate a day when data classes are mixed, evidence changes after acceptance, the feed is unlicensed, or safety assertions fail.

Qualification has an explicit `real_market` scope. Only real-market campaign days marked `real_market` and `provider_certified` may count. Synthetic/imported accelerated days remain useful engineering evidence but count as zero real-market days.
