# Canonical Research Operator Runbook

Minimal V1 is the current operator interface. This runbook covers read-only research discovery
and reproduction of the immutable archived compatibility target; it does not start paper
sessions or campaigns.

From `backend`:

1. Run `python -m app.minimal_v1_cli status` and require paper mode, live disabled, broker
   disabled, the expected database role, and a valid audit chain.
2. Run `python -m app.minimal_v1_cli datasets` and review dataset hashes, coverage, adjustment
   grain, activation status, and lineage.
3. Run `python -m app.minimal_v1_cli strategies` and confirm execution and promotion permissions
   remain false for the archived strategy.
4. Run `python -m app.minimal_v1_cli runs` to inspect the canonical historical result.
5. Run `python -m app.minimal_v1_cli reproduce` only when compatibility verification is needed.
   Require all metric differences to remain within the embedded tolerances and the verdict to
   remain `reject_strategy / archived_rejected_benchmark`.

The legacy session, campaign, qualification, and milestone operator instructions are historical
evidence, not current getting-started guidance. Their files remain unchanged unless explicitly
listed in the [Legacy Surface Inventory](LEGACY_SURFACE_INVENTORY.md); no archival or deletion is
authorized by this runbook.
