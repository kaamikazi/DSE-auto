# Low-Memory Distributed Mode

Status: **real serialized sub-stage verification** on 2026-07-15. This mode is for paper-only infrastructure verification; it is not a production-capacity claim or real-market evidence.

`docker-compose.low-memory.yml` keeps PostgreSQL, Redis, API when required, one external scheduler, and one worker per container. It requires an explicitly named isolated PostgreSQL database, limits each Python process to a 2-connection pool plus 1 overflow connection, starts no frontend or `db_test`, and uses production-style processes without reload.

The unchanged pre-start requirement remains 3 GiB available physical memory and 8 GiB commit headroom. Post-start continuation additionally requires at least 600 measured seconds, an adaptive physical reserve of `max(1.5 GiB, 1.25 × measured project footprint)`, at least 8 GiB commit headroom, stable memory trend, healthy pagefile, no sustained paging, no restart/OOM/process loss, healthy PostgreSQL, and a valid canonical audit chain.

| Component | B1 peak | B2 peak | B3 peak |
| --- | ---: | ---: | ---: |
| PostgreSQL | 38.8 MiB | 37.9 MiB | 40.1 MiB |
| Redis | 5.0 MiB | 5.2 MiB | 6.1 MiB |
| API | 83.8 MiB | stopped | 80.9 MiB |
| Scheduler | 57.2 MiB | 58.9 MiB | 53.9 MiB |
| Worker 1 | 63.2 MiB | 58.2 MiB | 61.1 MiB |
| Worker 2 | stopped | 62.8 MiB | stopped |
| Docker/WSL overhead (peak estimate) | 0.726 GiB | 0.750 GiB | 0.692 GiB |
| Total project footprint | 0.967 GiB | 0.962 GiB | 0.927 GiB |

B1 and B3 passed. B2 failed due real host paging and memory trend despite flat worker memory; it must not be treated as a capacity pass.
