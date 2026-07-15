# Real-Market Research Data

The research-only workflow covers GP, ACI, BRACBANK, and DSEX daily OHLCV, timestamps, corporate actions, suspensions, holidays, index values, and optional intraday quotes. Raw bytes and normalized rows have separate SHA-256 hashes. Quality checks cover duplicates, missing weekdays, outliers, OHLC consistency, symbol scope, source timestamps, and declared corporate-action factors.

Research activation requires a quality pass, reviewed provenance, and explicit operator action. It is not campaign activation and contributes zero days to the 60-day tracker. No real dataset is currently supplied; deterministic fixtures are test-only.
