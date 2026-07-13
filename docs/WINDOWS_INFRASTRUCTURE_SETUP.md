# Windows Infrastructure Setup

Run `powershell -ExecutionPolicy Bypass -File scripts/infrastructure_doctor.ps1` from the repository root. The command is read-only: it does not start Docker, change Windows services, enable WSL, open ports, or edit Docker Desktop.

The report checks Docker installation/service/engine, Compose, WSL2, ports 3000/5432/6379/8000, disk, free memory, firmware virtualization, PostgreSQL, and Redis. JSON and Markdown outputs are written under `reports/infrastructure/` and the process exits non-zero whenever a required check fails.

On 2026-07-13 the actual machine result was **BLOCKED / FAIL CLOSED**. Docker CLI and Compose 5.1.0 were installed, WSL2 defaulted to version 2, firmware virtualization was enabled, ports 3000/5432/6379/8000 were free, and 119.43 GB of disk was available. Docker's service/Linux engine was not running, PostgreSQL and Redis were unreachable, and only 3.36 GB of 15.26 GB RAM was free, below the 4 GB exercise threshold. This is not infrastructure verification.

Operator remediation:

1. Close memory-heavy applications until at least 4 GB is free.
2. Open Docker Desktop manually and wait for its Linux/WSL2 engine to report healthy.
3. Rerun the doctor. Do not use `-SkipDoctor` for verification evidence.
4. Configure strong, distinct local passwords and operator/reviewer keys in `.env` from `.env.example`.
5. Run the production-like or distributed verification scripts only after reviewing their explicit service start/restart actions.

Administrative WSL/virtualization changes require separate operator approval and a reboot when Windows requests it.
