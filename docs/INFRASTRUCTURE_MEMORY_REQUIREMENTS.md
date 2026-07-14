# Infrastructure Memory Requirements

The former fixed 4 GiB rule was a conservative full-workload assumption, not a measured universal minimum. Milestone 9 replaces it with two mandatory margins: available physical memory and Windows commit headroom.

| Tier | Minimum available | Minimum commit headroom | Classification |
| --- | ---: | ---: | --- |
| `database_only` | 1.5 GiB | 4 GiB | Conservative estimate; measured containers use far less idle |
| `integration_tests` | 2 GiB | 6 GiB | Verified with real PostgreSQL/Redis integration tests |
| `distributed_runtime` | 3 GiB | 8 GiB | Pre-start passed; post-start failed at 2.38 GiB |
| `distributed_campaign` | 4 GiB | 10 GiB | Retained; blocked and not run |

These remain conservative engineering budgets until repeated peak measurements support changes. Pagefile headroom prevents commit exhaustion but does not replace the physical-memory margin. A tier fails closed if either margin fails.
