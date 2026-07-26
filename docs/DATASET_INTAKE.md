# Dataset intake

The `/api/v1/research-data` workflow registers a raw CSV, ZIP-of-CSV, XLSX, JSON, or Parquet file, then requires an explicit normalized-column mapping and preview. ZIP members are limited in count, expanded size, compression ratio, and depth; traversal, links, nested paths, non-CSV members, executables, active content, duplicates, and retention collisions fail closed.

Clean previews may be activated only as `active_for_research`. Activation writes immutable normalized JSON and database lineage. Rollback removes normalized rows without deleting raw evidence. It never qualifies a campaign, creates a signal, or creates an order.
