# gemini-3.1-flash-lite as the sole scoring model, no tiering, no automatic fallback

The obvious default, gemini-3.5-flash, turned out to have a measured free-tier limit of 20 requests per day (issue #17), which cannot cover even fresh uploads. The pipeline therefore scores everything with gemini-3.1-flash-lite (measured 500 RPD, 15 RPM, 250k input TPM), a single-model pipeline with no premium rescoring pass and no automatic fallback model. Switching models is a manual `.env` edit, with deepseek-v4-flash (non-thinking) documented as the alternate.

## Considered options

- gemini-3.5-flash as default: rejected, 20 RPD means a backfill backlog would take weeks to drain.
- gemma-3-27b: rejected, 14,400 RPD but only 15k input TPM, which collides with the ~15k-token truncation threshold; near-threshold calls risk outright rejection.
- Paid tier: rejected, it solves a problem a free model already solves.
- Tiered scoring (flash-lite everywhere, 3.5-flash rescoring borderline videos): deferred, not built. It needs a borderline definition and disagreement reconciliation, all bought before any evidence that flash-lite calibration is bad. The M4 audit produces that evidence.
- Automatic fallback on primary failure (issue #8): rejected, backoff, the circuit breaker, and defer-to-next-run already cover the failure modes, and the feed tolerates a day of delay.

## Consequences

- Because score rows are final and calibration is scoped per model (ADR 0002), a model switch resets the calibration sample. The choice is stickier than a config edit suggests.
- Pacing is sized to flash-lite: 10s inter-call delay and a 200-call daily self-cap, which is 40% of the measured 500 RPD.
- Free-tier limits are per-project and unpublished; the project's AI Studio rate-limit page is the source of truth, and the measured figures here can drift.

Source: issues #17, #18, and #8, research doc `docs/research/gemini-free-tier-rate-limits.md`, PRD sections 6, 9, and 11.
