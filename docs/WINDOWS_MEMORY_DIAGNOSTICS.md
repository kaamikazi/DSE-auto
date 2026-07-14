# Windows Memory Diagnostics

Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/memory_doctor.ps1`. The script is read-only except for JSON and Markdown reports under `reports/memory/`; it never terminates processes, clears standby memory, edits the pagefile, or changes Docker/WSL settings.

The 2026-07-14 measured snapshot reported 15.26 GiB physical memory, 3.41 GiB available, 20.61/38.26 GiB committed/limit, 17.65 GiB commit headroom, and a 23 GiB pagefile with 0.27 GiB used. Available memory later fell to 2.38 GiB after the Stage B application containers started.

Largest measured working sets were `vmmemWSL` (1.06–1.77 GiB), Chrome (1.08–1.30 GiB aggregate), Defender (0.69–0.76 GiB), memory compression (0.59–0.69 GiB), ChatGPT processes, and unrelated Oracle/MySQL/SQL Server services. Project PostgreSQL plus Redis used about 38 MiB idle; `db_test` added roughly 72–80 MiB.

## 2026-07-15 low-memory continuation

The read-only unrelated-service diagnostic found running Oracle XE/listener, MySQL80, MySQL84, SQL Server Express/telemetry/writer, about 0.94 GiB Chrome working set, and about 0.52 GiB across LM Studio/Teams/OneDrive. Nothing was stopped. `scripts/unrelated_service_doctor.ps1` records exact optional operator commands while protecting Docker Desktop, WSL2, project containers, Windows core services, and Defender.

B2 showed why host-level measurements remain mandatory: both workers stayed flat near 58–63 MiB, but Windows recorded a 2,178 hard-pages/s burst, pagefile growth from 0.556 to 0.661 GiB, and available-memory decline. The stage failed closed.

Safe operator actions are limited to closing approved applications, stopping unrelated database services through their normal controls, rebooting when appropriate, and reviewing Docker/WSL limits. Cleanup commands from `docker system df` are advisory only; never delete images, volumes, caches, or databases automatically.
