# Expanded DSE research-universe candidate

The governed candidate is **inactive**. Every per-symbol activation decision defaults
to `rejected_not_granted`; no blanket approval is requested. The candidate was built
at Git HEAD `d97e1e190ee8701d34c182962dff0e5ddd12410f` from the retained canonical
candidate database using data quality and sector diversity only. No strategy return,
trade, P&L, or other performance field participated in selection.

## Proposed symbols

The 25-symbol candidate is:

- GP, ACI, BRACBANK
- HEIDELBCEM, PREMIERCEM
- WALTONHIL, RSRMSTEEL, BSRMLTD
- LANKABAFIN, IDLC
- AMCL(PRAN), BATBC, OLYMPIC
- POWERGRID, SUMITPOWER, TITASGAS
- GREENDELT, RELIANCINS
- UNILEVERCL, MARICO, BERGERPBL
- SQURPHARMA, RENATA
- SQUARETEXT, PARAMOUNT

These cover 11 provisional sectors with no more than three symbols per sector. GP,
ACI, and BRACBANK are continuity anchors from the already-approved research dataset;
this is a governance-continuity rule, not a performance rule. At least one passing
candidate is retained from each provisional sector before remaining places are filled
by quality score.

## Data-quality and survivorship boundary

All 25 symbols are `review_required`, not ready for activation. Their first and last
valid observations are provisional research bounds only. Official listing, delisting,
and suspension evidence is not available in the retained registry, so no current-only
universe is projected backward and no interval is approved. The implementation can
enforce verified listing/delisting dates and split eligibility around suspension
periods once evidence is supplied.

ROBI and LHBL require cleaning because duplicate burden exceeds 5%; LHBL also lacks
both adjusted and unadjusted views. CITYBANK, EBL, DUTCHBANGL, JAMUNABANK, DBH,
BEXIMCO, GPHISPAT, MJLBD, and ENVOYTEX passed basic quality gates but were excluded
by continuity anchors, sector minimums, sector caps, and the fixed 25-symbol limit.

## Separate DSEX track

DSEX remains rejected and separate from equity activation. The preserved evidence
contains 6,586 `00DSEX` rows (680 invalid), 2,209 literal `DSEX` rows (2 invalid),
and 240 duplicate rows. The price-series identity is plausible but the alias is not
officially verified, continuity has not passed, and non-comparable volume semantics
remain excluded. Malformed rows remain preserved.

## Prepared study, not executed

The expanded `ma_crossover@1.0.0` plan freezes the universe before execution and
includes per-symbol, equal-weight, sector-balanced, leave-one-symbol-out,
leave-one-sector-out, rolling walk-forward, untouched holdout, buy-and-hold, cash,
drawdown, return/drawdown, cost, slippage, source-tier, corporate-action, and
parameter-stability checks. Mandatory ablations remove BRACBANK, the future
best-performing symbol, every sector in turn, weaker-quality symbols, and apply
stricter costs.

Private evidence is retained under
`reports/expanded_research_universe/review_986762d211a099e5d0bb2392/`; manifest SHA-256
is `cf537b23079ec1a9405bbea21727513a16e547b820bed76aeeeb2b9c6a4f77b7`.
JSON, CSV, Markdown, canonical artifact input, audit linkage, and hashes are present.
Portable HTML is not claimed: after bounded artifact-contract corrections, the
builder required embedded executable SQL provenance for the chart source.

No dataset, strategy, campaign, session, signal, proposal, order, transaction, fill,
or broker connection was activated or executed. Strategy lifecycle remains
`research`, promotion remains blocked, campaign eligibility remains false, and
qualification remains 0/60.
