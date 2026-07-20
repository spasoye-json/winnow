# Organic-only calibration with a sample floor, and scores final once written

Score quality is validated only against verdicts accumulated from daily use, with no seeded golden set. The 80% acceptance bar (greats above threshold, slop below) is valid only once 20 great and 20 slop verdicts exist; below that floor, agreement is provisional. Agreement is computed live: each video's effective score is recomputed from stored dimension scores with current weights and compared to the current threshold, so tuning never requires re-scoring. Score rows are final once written and are never re-scored, not after a scoring model switch and not after a rubric prompt change; each score row records its model and prompt version, and the audit is scoped to the current pair.

## Considered options

- Seeded golden set: rejected, an up-front labeling pass measures effort spent labeling, not the tool in real use.
- Re-scoring after prompt or model changes: rejected (also in issue #8), it burns quota, muddies before-and-after comparison, and the model column plus prompt version make old rows queryable instead.
- Prompt iteration at will: rejected, prompt changes are gated on a failed bar with a valid sample, one change at a time, informed by the disagreement list.

## Consequences

- Each rubric prompt iteration cycle takes roughly two weeks, since a prompt change starts a fresh accumulation toward the 20-per-class floor. This is the accepted price of interpretable agreement numbers.
- Switching scoring models mid-stream resets the calibration sample, since the audit only covers rows from the current model.
- Weights and threshold can be tuned freely at any time with instant effect and no sample reset.

Source: issues #12 and #8, PRD sections 12 and 14.
