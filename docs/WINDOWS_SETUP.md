# Windows Setup

Install Python 3.12+, Node 22+, Git and optionally Docker Desktop. From PowerShell in the repository run `Copy-Item .env.example .env`, set a unique 32+ character `API_SECRET_KEY`, then run `.\scripts\setup.ps1`.

Execution policy may block `.ps1`; use `Set-ExecutionPolicy -Scope Process Bypass` only in a trusted shell if your organization permits it. SQLite needs no service. For PostgreSQL/Redis use `docker compose up --build` after configuring secrets.

