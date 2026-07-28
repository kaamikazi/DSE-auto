# ma_crossover operational research registration

Registration ID: `4faf2623-f458-4d96-93d0-e70e8af8f7f6`  
Strategy: `ma_crossover@1.0.0`  
Lifecycle: `research`  
Readiness: `ready_for_research_execution_authorization`

This is an operational identity registration only. It does not authorize strategy execution, promotion, campaign creation, paper activity, broker access, production use, live trading, or real-money use. Qualification remains `0/60`.

## Bound identity

- Implementation: `backend/app/backtesting/engine.py`
- Parameter source: `backend/app/services/research_governance.py::PARAMETERS`
- Parameters: `{"fast":20,"slow":50}`
- Code SHA-256: `b3b8e3bbce398d084b1b971332876861745e40f11600d83e9435e4c5e4ecb3b3`
- Parameter SHA-256: `51d34977e7e67cb3045ec624e7e0f6474fb24390f6427fa1d0f307e4ee7df13e`
- Dataset ID: `ba5f2d99-6c66-4e37-ae31-d48c8ee47b15`
- Dataset version: `gp-aci-bracbank-research-f24a48cb729e8a65`
- Dataset SHA-256: `ddfeee6bbf5324f9f64fd604e9d4bfc7bd2a45ce2896e7b968037af58de04791`
- Symbols: GP, ACI, BRACBANK; DSEX remains prohibited.

The registration is deterministic, operator-registered, non-independently reviewed, promotion-blocked, campaign-ineligible, and eligible only for a separately authorized research execution.

## Legacy identity review

No reusable historical registration ID was found. The canonical audit chain contains an execution-prohibition decision but no earlier registration-creation event. The governance template used `UNASSIGNED`; the promotion approval pack contains no registration identity. Four recovery-state SQLite snapshots and two disaster-recovery SQLite copies contained no `ma_crossover@1.0.0` registration. Custom PostgreSQL dumps were not restored blindly and remain classified `unverifiable_without_isolated_restore`. A new UUID was therefore created for the current operational identity.

## Audit boundary

Five separate canonical events record artifact-hash verification, registration authorization, registration creation, promotion prohibition, and the continued requirement for separate execution authorization. The audit chain verified immediately afterward.

The private authorization, legacy review, provenance, protected-state counts, and hashed manifest are retained under `reports/research_strategy_registration/4faf2623-f458-4d96-93d0-e70e8af8f7f6/`.
