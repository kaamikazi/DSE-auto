# ma_crossover historical research execution

The operator authorized one research-only execution of `ma_crossover@1.0.0` at Git
HEAD `488cf33284de276917b0b1188f6b10571215568b`. The runner fails closed unless the
registration, code, parameters, approved dataset, safety configuration, database,
and canonical audit identities match their pinned values.

The 2026-07-29 execution used the adjusted research view for GP, ACI, and BRACBANK,
with close-derived signals observable only after each bar and execution no earlier
than the next source-present open. It modeled a 0.40% non-authoritative fee, 0.25%
pessimistic slippage, no leverage, no short selling, and a maximum five-percent
participation capacity. Unadjusted rows were validation-only; DSEX was unavailable
and not substituted.

Net total returns were 77.63% for GP, 43.65% for ACI, 562.55% for BRACBANK, and
227.95% for the equal-weight portfolio. The equal-weight buy-and-hold comparison
was 261.34%. These descriptive historical values are not a profitability claim or
a forecast. Maximum drawdowns were -48.67%, -52.06%, -28.13%, and -23.80%,
respectively. Only 65 closed trades were observed across the three symbols.

The final verdict is `insufficient_evidence`. It fails closed because performance
shows material symbol dependence, symbol-level final holdouts are inconsistent,
the effective independent trade sample is limited, and unresolved corporate-action
uncertainty remains material. Tier-1-only samples were insufficient for an
equal-weight comparison. The registered 20/50 pair was at the 66.67th percentile
of the bounded nine-pair grid; all grid portfolios were positive, but this does not
overcome the other limitations.

Private evidence is retained under
`reports/strategy_research/ma-crossover-20260729T083719Z-de0dac8b/`. Its manifest
hash is `b2a2cbb97619d2e64686e90d20e0d288c23488a3e3610876064941b8ccf23ab8`.
The pack contains JSON, CSV, Markdown, and self-contained semantic HTML. Browser
visual QA was not performed. Seven separate canonical audit events record the
authorization through the promotion-prohibited conclusion.

No strategy promotion, campaign, paper session, signal, proposal, order,
transaction, fill, broker access, or real-money authorization was created. The
strategy remains `research`, campaign eligibility remains false, qualification
remains 0/60, and paper-only safety remains mandatory.
