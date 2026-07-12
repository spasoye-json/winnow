# Truncation strategy for very long transcripts in the scoring worker

Research against primary sources (ai.google.dev, arXiv, Oxford Applied Linguistics) on 2026-07-12. Resolves PRD open question (docs/prd-youtube-curator.md section 13): head+tail sampling vs chunk-and-summarize, under the ~15k-token transcript budget from section 6.

## TL;DR

1. **Recommendation: head+middle+tail sampling in a single scoring call.** Budget split 6,000 / 5,000 / 4,000 tokens (head, middle, tail), middle segment centered on 50% of video duration, explicit omission markers between segments, triggered when the estimated transcript token count exceeds 15,000. No chunk-and-summarize.
2. **Truncation triggers at roughly 60 to 90 minutes of video.** 15k tokens is about 9,000 to 12,000 English words (Gemini: 100 tokens is about 60 to 80 words), and produced speech runs about 125 to 160 wpm. Videos under an hour essentially never truncate; a typical subscription day of ~50 videos likely truncates 0 to 10 of them, concentrated in podcasts and lectures.
3. **Chunk-and-summarize costs 4 to 5 requests per long video instead of 1.** Worst case (50 long videos) consumes the entire 200 requests-per-day self-cap with zero retry headroom; sampling always costs exactly 50. Map calls also use a different prompt, so they get no benefit from the cached rubric prefix and add roughly 2.4x uncached input tokens on a long-video day.
4. **Summarization destroys the signal the rubric scores.** Padding, repetition, filler, and AI-narration phrasing are exactly what a summary removes by design. Head+middle+tail keeps raw transcript text for every dimension: head for clickbait gap, middle for padding ratio, distributed samples for depth. Published evidence agrees: head+tail truncation beat head-only and tail-only for long-document classification (Sun et al. 2019), and LLMs attend best to the beginning and end of long inputs (Liu et al., "Lost in the Middle").

## 1. When does a transcript exceed 15k tokens

- **Tokens per word.** https://ai.google.dev/gemini-api/docs/tokens (retrieved 2026-07-12): "For Gemini models, a token is equivalent to about 4 characters. 100 tokens is equal to about 60-80 English words." So 15,000 tokens is roughly 9,000 to 12,000 English words, and 1 word is roughly 1.25 to 1.67 tokens.
- **Speech rate.** Tauroza and Allison, "Speech Rates in British English", Applied Linguistics 11(1):90-105 (1990), https://academic.oup.com/applij/article/11/1/90/255991: across conversations, lectures, interviews, and radio monologues, their guideline classifies 125 to 160 wpm as average speech, 160 to 185 as moderately fast. Produced YouTube content (scripted narration, edited podcasts) sits in the 130 to 160 band.
- **Threshold math.** 9,000 words at 155 wpm is 58 minutes; 12,000 words at 130 wpm is 92 minutes. So a transcript crosses 15k tokens somewhere between roughly 60 and 90 minutes of video depending on speech rate and tokenization; call the center estimate 70 minutes. Under 60 minutes truncation essentially never fires; over 90 minutes it always does.
- **How often it fires.** No trustworthy current primary source exists for the duration distribution of a curated subscription feed. The best published dataset numbers are old and general-population: a 2007 YouTube crawl found mean duration 441s, median 181s, only 15% of videos over 10 minutes (https://arxiv.org/abs/0707.3670). That is two decades stale and Winnow's mix is deliberately long-form-leaning, so reason from the math instead: only videos over ~1 hour truncate. A mainstream mix of 50 videos/day probably truncates 1 to 5; a podcast-heavy and lecture-heavy mix maybe 10. Either way it is a minority path, which argues for the simplest mechanism that handles it well rather than a second pipeline.

## 2. Quota cost of chunk-and-summarize

Assume map chunks of ~12k tokens (leaving room for the summarization instructions) plus one reduce-and-score call.

- 2-hour video: 120 min x 150 wpm = 18,000 words, about 25k tokens (Gemini band 1.25 to 1.67 tokens per word). 3 map calls + 1 reduce = **4 requests instead of 1**.
- 3-hour podcast: about 37k tokens. 4 map + 1 reduce = **5 requests**.
- Daily worst case against the self-cap from docs/research/gemini-free-tier-rate-limits.md (200 requests/day, 10s pacing): 50 two-hour videos = 200 requests, which is **100% of the cap with zero headroom for 429 retries**, and about 34 minutes of wall time at 10s pacing. A merely bad day (10 long videos among 50) is 40 + 40 = 80 requests, survivable but 60% more than sampling.
- Sampling by contrast is always exactly 50 requests regardless of durations: 25% of the cap, deterministic, retry headroom intact.

## 3. Rubric dimensions vs candidate strategies

The rubric (PRD section 7) needs specific regions of the video: **clickbait gap** needs the head measured against the title, **padding ratio** needs the middle (mid-roll sponsor reads, repetition, stalling), **depth and rigor** needs wherever the substance is, **production integrity** needs raw phrasing anywhere.

| Strategy | Clickbait gap | Padding ratio | Depth | Production integrity |
|---|---|---|---|---|
| Head-only | OK (promise visible, delivery often not) | Bad: middle invisible | Bad: substance often mid or late | OK |
| Head+tail | Good: promise and payoff both visible | Weak: mid-roll padding invisible; only intro stalling caught | OK | OK |
| Head+middle+tail | Good | Good: middle sample is direct padding evidence | Good: three probes across the runtime | Good |
| Chunk-and-summarize | Weak: summary paraphrases the promise-delivery relationship | Very bad: summaries compress out repetition and filler, erasing the measurement target | OK for content depth, loses style-level rigor cues | Very bad: AI-narration phrasing rewritten away |

The core failure of chunk-and-summarize: the rubric scores **form**, not just content. A summary is by definition the transcript with the padding removed, so a padded 2-hour video and a dense one can produce near-identical summaries.

Published evidence:

- **Head+tail beats head-only and tail-only** for long-document classification: Sun et al., "How to Fine-Tune BERT for Text Classification?" (https://arxiv.org/abs/1905.05583) tested head-only (first 510 tokens), tail-only (last 510), and head+tail (first 128 + last 382): "The truncation method of head+tail achieves the best performance on IMDb and Sogou datasets" (error 5.42% vs 5.63% head-only on IMDb). Their tasks put evidence at document ends; Winnow's padding dimension puts evidence in the middle, which motivates adding the middle segment.
- **Models privilege the beginning and end of long context**: Liu et al., "Lost in the Middle: How Language Models Use Long Contexts" (https://arxiv.org/abs/2307.03172, TACL 2024): "performance is often highest when relevant information occurs at the beginning or end of the input context, and significantly degrades when models must access relevant information in the middle of long contexts", "even for explicitly long-context models". Two implications for Winnow: (a) stuffing a full 40k-token transcript into the prompt would not reliably beat a 15k sample anyway, since mid-context content is under-attended; (b) at ~20k total prompt tokens the effect is modest, and the middle segment is still needed because no other strategy sees that region at all.
- No published head-to-head of truncation vs summarization for LLM rubric-judgment tasks was found; the closest evidence is the classification result above plus the definitional argument that summarization removes style-level features.

## 4. Prefix-caching compatibility

Constraint from PRD section 6: static rubric first, byte-identical prefix across calls, per-video content last. Implicit caching is default-on for Gemini 2.5+ with a 4,096-token minimum prompt for gemini-3.5-flash and Google's guidance to put common content first (https://ai.google.dev/gemini-api/docs/caching, retrieved 2026-07-12; details in docs/research/gemini-free-tier-rate-limits.md).

- **Any single-call sampling strategy is fully compatible.** The rubric prefix stays byte-identical; truncation markers and the sampled transcript live in the per-video suffix. One fixed paragraph in the static rubric explains the marker format, which is itself byte-identical and cached.
- **Chunk-and-summarize is not.** Map calls use a summarization prompt, a different prefix entirely. The shared portion across map calls is only the short summarization instructions (a few hundred tokens, below the 4,096 minimum), so map calls get effectively zero cache benefit while each carries ~12k unique transcript tokens. Rough quantification for a 10-long-video day (2h average): 30 map calls x 12k = **~360k uncached input tokens**, vs ~150k transcript tokens behind a cached rubric prefix under sampling, about 2.4x the uncached input plus 30 extra requests. On the free tier the cost is $0 either way, but it burns TPM budget and, on the DeepSeek fallback path where prefix caching is a real 50x price difference on the cached portion (PRD section 11), it dilutes the discount.

## Recommendation

**Head+middle+tail sampling, single scoring call per video.**

- **Trigger:** estimate tokens as `ceil(len(transcript_chars) / 4)` (Gemini's documented 4 chars per token). If the estimate is <= 15,000, send the full transcript untouched. Otherwise sample.
- **Budget split (15,000 transcript tokens total):**
  - Head: 6,000 tokens (~24,000 chars) from the start. Carries the title-promise check, intro padding, and thesis.
  - Middle: 5,000 tokens (~20,000 chars) centered on 50% of **video duration** using transcript snippet timestamps (not 50% of character count), since padding assessment is temporal.
  - Tail: 4,000 tokens (~16,000 chars) from the end. Carries payoff and conclusions.
- **Cut points:** cut only at transcript snippet boundaries (youtube-transcript-api returns timed snippets) so segments start and end on natural utterances.
- **Markers:** between segments insert exactly one line:
  `[TRANSCRIPT OMITTED: ~{words} words, {start_min} min to {end_min} min of the video]`
  Before the transcript, inside the per-video block, one stats line:
  `Transcript: sampled (head, middle, tail) from ~{total_words} words over {duration_min} minutes ({wpm} wpm).`
  The stats line preserves the information-density and hard-flag signal that truncation would otherwise hide.
- **Static rubric addition (cache-safe, byte-identical):** one fixed paragraph stating that long transcripts arrive sampled with `[TRANSCRIPT OMITTED: ...]` markers, that omitted spans must not be scored as missing content, and that the stats line gives full-video word counts.
- **Hard flag ordering:** compute the "transcript under 20% of what duration implies" hard flag (PRD section 7) from the **full** transcript word count before sampling, never from the sampled text.
- **Fallbacks:** if snippets lack timestamps, center the middle segment at 50% of character count. Non-English text: the chars/4 estimate undercounts tokens for non-Latin scripts, so also cap by word count (trigger if words > 10,000) as a belt-and-suspenders check. No other fallback needed; sampling is deterministic and cannot fail.

## Why not chunk-and-summarize

1. **It erases the measurement target.** Padding ratio, repetition, filler, and AI-script phrasing are style-level features that summarization removes by design. The rubric would be scoring the summarizer's prose, not the video's.
2. **Quota:** 4 to 5 requests per long video vs 1. The worst case consumes the entire 200 requests-per-day self-cap; sampling holds a constant 50.
3. **Cache:** map calls use a different prompt, fall below the 4,096-token implicit-cache minimum on their shared portion, and add roughly 2.4x uncached input tokens on a heavy day, plus they dilute the paid-tier prefix discount on the DeepSeek fallback.
4. **Complexity and failure surface:** per-video fan-out state, partial map failures, and multi-call latency, all to serve a minority path (a handful of videos per day). Reconsider only if audit feedback (PRD section 14) shows long videos scoring systematically wrong under sampling.

## Unverified claims

- The truncation-frequency estimate (0 to 10 of 50 videos/day) is inference from speech-rate math plus an assumed subscription mix; the only published duration distribution found is a 2007 crawl (https://arxiv.org/abs/0707.3670) and it was not accepted as evidence for a 2026 curated feed. Log actual transcript token counts in production to calibrate.
- No paper directly comparing truncation vs summarization for LLM-as-judge rubric scoring was found; the Sun et al. result is for encoder classification and the "summaries erase padding" argument is definitional, not measured.
- The 6k/5k/4k split is a judgment call anchored to Sun et al.'s finding that the head deserves the largest share alongside a nontrivial tail; only the feedback audit loop can validate the exact ratios.
- Whether implicit cache hits reduce TPM accounting is undocumented (see docs/research/gemini-free-tier-rate-limits.md section 3), so the token arithmetic in section 4 assumes full input counts.
