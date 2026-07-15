# Distributed Campaign Results

## Corrected-B2 campaign — 2026-07-15

Campaign `ec0a77b3-dc35-4772-bda0-41b6a768d461` passed its three-day checkpoint, so the harness conditionally extended it to ten accelerated days. All ten externally queued `simulation_day` tasks succeeded once. GP, ACI, and BRACBANK exercised `ma_crossover` and `momentum_dsex`; one review was deliberately rejected and nine were accepted. Worker-restart and stale-data incidents were resolved, risk rejection and partial-fill evidence were present, reconciliation was healthy, cash matched derived cash, duplicate orders were false, and the canonical audit chain was valid.

The three-day report SHA-256 is `9BF5D1FE2FF7BF2CA6CFF744A16C3A7E7719F92EBBAC034B7A825716312128CB`; the ten-day report SHA-256 is `5AA18D9B35D817F830DB51A9762835A01C283233D65F963C3D13692BFF540F5D`.

The checkpoint backup files created during the campaign used Windows PowerShell 5.1 binary redirection and were later proven non-restorable (UTF-16-expanded `PGDMP` bytes). They remain preserved as invalid evidence and are not counted as backup passes. After fixing binary transport, a fresh 103,704-byte custom-format backup (`6FB756BC73A09864BAAF1A299F5139F11EFBA02214FA99D1D35B8339084FF5B3`) restored 34 tables and a valid audit chain.

Classification remains **accelerated distributed infrastructure validation**. It contributes zero real-market qualification days and makes no profitability or live-readiness claim.

Status: **blocked / not run**.

The shortened 3-day accelerated distributed infrastructure campaign was not started because B2 failed its measured two-worker safety gate. Therefore the 10-day extension was also not run. No accelerated run is classified as real-market evidence, and no campaign success, strategy profitability, or all-process simultaneous stability claim is made.
