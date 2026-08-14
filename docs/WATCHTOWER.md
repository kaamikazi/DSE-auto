# DSE Watchtower v0.1

Watchtower is a local-only, paper-research scanner. It answers which securities deserve
human investigation and why. It does not produce orders, fills, transactions, portfolio
effects, expected returns, profit probabilities, or recommendations.

## Architecture

`app.services.watchtower` reads immutable operator-owned DSE Day End HTML/CSV files, an
optional manual instrument master, and optional manual event evidence. It computes only
deterministic price/activity anomalies, applies a transparent attention score, and writes
one JSON, one CSV, and one Markdown report. `app.watchtower_cli` is the only entrypoint.

The service reuses the existing forward-ingest HTML table extractor. It does not construct
the forward runner, open a database session, import a broker, or change forward evidence.
The operational SQLite file is only hashed before and after a run.

## Run

From `backend`:

```powershell
.\.venv\Scripts\python.exe -m app.watchtower_cli
```

Defaults:

- Day End inputs: `End of day`
- Instrument master: `config/watchtower_instrument_master.csv`
- Events: `config/watchtower_events.json`
- Reports: `reports/watchtower/YYYY-MM-DD`

Existing report files are reused only when byte-identical. Watchtower refuses to overwrite
a different report for the same date.

## Instrument-master contract

The CSV header is:

```text
trading_code,company_name,sector,instrument_type,market_category,listing_status,observed_at,source_reference,verification_status
```

`verification_status` must be one of `VERIFIED_EQUITY`, `UNVERIFIED_INSTRUMENT`, or
`NON_EQUITY`. A verified equity requires an equity instrument type, company, sector,
category, listing status, timezone-aware observation time, and official source reference.
Unknown values remain empty; the scanner never infers them.

For the next metadata import the operator must manually save:

1. `https://www.dsebd.org/company_listing.php`
2. `https://www.dsebd.org/by_industrylisting.php`
3. Relevant official DSE company profile pages used to verify instrument type, category,
   sector, and listing status

Watchtower does not fetch these pages.

## Manual event-evidence contract

`config/watchtower_events.json` is a JSON array. Each object contains:

```json
{
  "trading_code": "EXAMPLE",
  "event_type": "earnings",
  "event_time": "2026-08-13T10:00:00+06:00",
  "publication_time": "2026-08-13T14:30:00+06:00",
  "observed_at": "2026-08-13T15:00:00+06:00",
  "source_tier": "A",
  "source_reference": "local file or official URL reference",
  "short_factual_summary": "Factual operator-entered summary.",
  "contradiction_flag": false
}
```

Events never change the attention score or report label and never prove causality. Tier E
evidence is displayed as rumour-only investigation context.

## Feature and scoring policy

Daily return, opening gap, and intraday range use the current official row and YCP when the
row is usable. Trailing multiples, robust z-scores, volatility expansion, and breakouts use
up to 60 prior usable sessions and require 40. Baselines use medians and MAD where relevant.
Zero baselines return an unavailable feature instead of dividing by zero.

The attention score is a sum of visible, predeclared anomaly-severity points. It is not
alpha, expected return, profit probability, or AI confidence. Unverified instruments can
have observable raw anomalies but can only receive `DATA_ISSUE`, never `WATCH` or
`HIGH_ATTENTION`.

## Safety boundary

Watchtower is fixed to the conceptual safety state:

```text
TRADING_MODE=paper
LIVE_TRADING_ENABLED=false
BROKER_ADAPTER=disabled
```

It has no network client, database writes, migrations, broker imports, credential access,
order path, fill path, transaction path, workers, queues, or autonomous browsing.
