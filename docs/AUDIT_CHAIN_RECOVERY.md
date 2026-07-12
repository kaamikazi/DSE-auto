# Audit Chain Recovery

The 398-event legacy audit set contained one preserved two-writer branch. Migration `0004` adds chain generations and monotonic sequence numbers. Recovery exports every original record to a JSON archive, hashes the archive, and requires a descriptive operator acknowledgement before creating a new canonical generation. Original database rows are never rewritten.

The completed archive hash is `8925b8d4e8d69386ca4716b8503b0ba238a9280c99e4deb879a93d54540f1000`. Canonical chain `c7aa6ed0-1288-417f-acbb-6ad4bfdd967c` initialized successfully and verifies after the imported-data session and backup/restore.

Commands:

```powershell
python scripts/operator.py audit-status
python scripts/operator.py verify-audit
python scripts/operator.py audit-recover --acknowledgement "..." --dry-run
```

Canonical audit records use an independently durable serialized journal. The journal commits before the calling workflow proceeds, preventing concurrent writers or a later caller crash from creating a branch.
