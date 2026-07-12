# Paper Session Operations

Sessions persist account, cash, universe, strategies, risk profile and fill model. Valid states are configured, warming_up, running, paused, degraded, reconciliation_required, stopped, completed and failed. Only one active session may use the paper account. Stale sessions recover to reconciliation_required. Use the authenticated API or `scripts\paper-operator.ps1`.
