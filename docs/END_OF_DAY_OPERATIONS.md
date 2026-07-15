# End-of-Day Operations

Run `scripts\m10-eod.ps1 -CampaignId <id> -MarketDate YYYY-MM-DD`. The script verifies paper-only safety, creates a PostgreSQL binary backup, restores it into an isolated database, removes that isolated database, records backup evidence, and invokes the EOD workflow.

EOD stops new proposals, expires pending session orders, finalizes simulated results, reconciles cash/holdings and order/fill uniqueness, verifies audit, and writes data-quality, strategy, execution, incident, account, reconciliation, audit, backup, and restore evidence. It produces HTML, JSON, and CSV with SHA-256 linkage and queues the day for human review.

Any failed reconciliation, audit, backup, isolated restore, source eligibility, or mandatory evidence check aborts. The day cannot count before an accepted review.
