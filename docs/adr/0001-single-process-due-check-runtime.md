# Single process with a state-driven due-check loop, no wall-clock scheduling

The host is a laptop that suspends often, so wall-clock schedules (cron, systemd timers, APScheduler) would routinely misfire and need missed-run bookkeeping. Instead, one long-lived FastAPI process runs a plain asyncio loop that ticks every 5 minutes and evaluates due-conditions from SQLite state: ingest runs when the last successful ingest is older than 6 hours, scoring runs whenever a backlog exists under the daily self-cap. Catch-up after sleep is inherent, because the first awake tick runs whatever is overdue.

## Considered options

- Cron or systemd timers: rejected, wall-clock schedules misfire on a suspending host and need sleep hooks.
- APScheduler or separate worker processes: rejected, adds moving parts for a single-user tool that a 5-minute poll covers.
- Manual start: rejected, work only happens while a terminal stays open.

## Consequences

- The process is started by a systemd user unit enabled at login (`WantedBy=default.target`, `Restart=on-failure`); the repo ships the unit file.
- All periodic behavior must be expressed as a due-condition over stored state, not as a schedule. New background work joins the tick, it does not get its own timer.
- Scoring is continuous through the day rather than a single batch; quota accounting stays per Pacific day.

Source: issue #11, PRD sections 6 and 9.
