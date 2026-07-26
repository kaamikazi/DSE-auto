# real portfolio onboarding

The user-requested workflow is implemented internally with neutral `PortfolioStatementDraft` records so portfolio-owner identity is not stored in repository history. It imports symbol, quantity, average cost, optional acquisition/realized activity, cash, dividends, bonus/rights events, redacted broker/account label, statement date, and source hash.

Every statement is previewed, duplicate-checked, reversible, reconciled, and separated from paper holdings. The UI banner is **REAL PORTFOLIO — READ ONLY**. This is not a broker connection and cannot submit orders.
