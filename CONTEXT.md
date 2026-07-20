# Winnow

Single-user local tool that builds an alternative YouTube feed ranked by content quality. Videos from the user's sources are scored against a rubric by an LLM, and only videos above a threshold appear in the feed.

## Language

### Sources

**Channel**:
A YouTube channel tracked as a video source. Its source is either `subscription` (mirrored from the user's YouTube subscriptions) or `manual` (added by hand).

**Active**:
Whether a channel is still supplied by its source. Subscription channels the user unsubscribes from are deactivated on sync, never deleted.
_Avoid_: deleted, removed

**Excluded**:
A user mute. An excluded channel stays synced but its videos are not ingested.
_Avoid_: muted, blocked, disabled

**Topic**:
A search query used as a supplemental video source beyond channels.
_Avoid_: search, keyword

### Ingest

**Ingest run**:
One pass that syncs subscriptions and then pulls new video metadata from all active, non-excluded channels and active topics.

**Backfill**:
The bounded pull of a channel's recent upload history when that channel first appears, whether via initial sync or manual add. Backfill is an ingest concept; the scoring of backfilled videos is just backlog.
_Avoid_: history import, catch-up

**Transcript status**:
Where a video stands on transcript fetching. `no_transcript` means no transcript exists in any language (permanent); `fetch_failed` means retries were exhausted on an unclassified error (may be retried by hand).

### Scoring

**Rubric**:
The six quality dimensions and their definitions as given to the scoring model. The rubric does not include weights.
_Avoid_: criteria, checklist

**Dimension**:
One of the six 0 to 10 quality axes: information density, originality, clickbait gap, padding, depth, production integrity.

**Weights**:
User-configurable multipliers that combine dimension scores into the effective score. Weights are a setting applied at read time, not part of the rubric or the scoring call.

**Scoring model**:
The LLM configured to score videos. Every score row records which scoring model wrote it.
_Avoid_: provider, engine

**Effective score**:
A video's overall quality, recomputed live from its stored dimension scores using current weights. The feed ranks by it and calibration compares it to the threshold.
_Avoid_: overall (ambiguous), score (alone)

**Stored overall**:
The overall value the scoring model returned at scoring time, kept for the record. Nothing ranks or filters by it.

**Threshold**:
The minimum effective score for a video to appear in the feed. User-configurable.
_Avoid_: cutoff, bar

**Hard flag**:
A disqualifying condition that suppresses a video from the feed entirely, regardless of effective score or threshold. Dimension scores are still produced and remain visible in detail and audit views.
_Avoid_: auto-fail, blacklist

**Low-transcript exemption**:
A per-channel opt-out of the low-transcript hard flag only. Exempt channels are still scored on all six dimensions.

**Backlog**:
The set of unscored videos awaiting the scoring worker, whatever their origin. Drained newest-first by publish date, fresh uploads before backfill.
_Avoid_: queue, backfill (that is the ingest step)

**Score finality**:
A score row is final once written. Nothing is ever re-scored, not after a model switch and not after a prompt change.
_Avoid_: re-scoring, refresh

**Prompt version**:
An integer identifying the rubric prompt revision that produced a score, bumped on every prompt change.

**Daily self-cap**:
The self-imposed budget of scoring calls per Pacific day, set below the provider's measured limit.
_Avoid_: quota (that is the provider's limit)

**Segment**:
One of the head, middle, or tail spans sampled from a transcript too long to score whole.
_Avoid_: slice, chunk

### Calibration

**Verdict**:
The user's post-watch judgment of a video: `great` or `slop`. Verdicts accumulate organically from daily use only.
_Avoid_: rating, label, feedback (alone)

**Agreement**:
The share of verdicts the scores concur with: greats whose effective score is above threshold, slop below. Always evaluated against current weights and threshold, scoped to the current scoring model and prompt version.

**Provisional**:
The state of an agreement figure before the sample floor of 20 great and 20 slop verdicts is reached. A provisional figure cannot pass or fail the acceptance bar.

**Disagreement list**:
The videos whose verdict and effective score conflict, sorted by distance from the threshold. The input for rubric prompt iteration.

### Runtime

**Due-check loop**:
The recurring in-process evaluation of state-derived due-conditions that decides whether ingest or scoring runs. There are no wall-clock schedules.
_Avoid_: cron, scheduler (as a component name), job

**Tick**:
One evaluation of the due-check loop.
