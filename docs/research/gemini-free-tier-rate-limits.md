# Gemini free tier rate limits for the scoring worker

Research against primary sources (ai.google.dev) on 2026-07-12. All cited numbers carry that retrieval date. These limits change often, so re-verify before relying on any number here.

## TL;DR

1. **Free tier RPD, RPM, TPM for gemini-3.5-flash: no longer published.** `gemini-3.5-flash` is a real, stable model ID (GA since 2026-05-19). But Google removed the static per-model rate limit tables from the official rate limits page. The page now says limits "can be viewed in Google AI Studio" per project, and warns "Specified rate limits are not guaranteed and actual capacity may vary." **The PRD's 1,500 RPD assumption is unverifiable against the current primary source.** It must be confirmed by reading the project's live limits at https://aistudio.google.com/rate-limit. What the page does still state: limits are measured as RPM, TPM (input), and RPD, and exceeding any one triggers a rate limit error.
2. **Same limits through the OpenAI-compatible endpoint, per project not per key.** The OpenAI compatibility layer is a thin translation over the same Gemini API (`generativelanguage.googleapis.com/v1beta/openai/`); it documents no separate quota. The rate limits page states "Rate limits are applied per project, not per API key." Multiple keys in one project share quota.
3. **Structured output: yes. Implicit caching: yes but with caveats.** The OpenAI compat page documents schema-based `response_format` (Pydantic and Zod `zodResponseFormat`, which is `json_schema` under the hood) with `gemini-3.5-flash`. `json_object` mode is not documented. Implicit caching is "enabled by default for all Gemini 2.5 and newer models" server-side, so it applies regardless of API surface; minimum 4,096 tokens for gemini-3.5-flash. On the free tier the monetary benefit is nil (tokens are already free), and the docs do not state whether cache hits reduce TPM accounting. Cached token counts are documented in the native SDK response (`usage.total_cached_tokens`); the OpenAI compat docs do not document `prompt_tokens_details.cached_tokens`, so treat cached-token visibility through the compat layer as unverified.
4. **Gotchas.** Free tier content is used to improve Google products (pricing page: "Content used to improve our products: Yes"; paid tier: No). RPD quotas reset at midnight Pacific time, not rolling. Docs do not document a Retry-After header on 429; the troubleshooting page prescribes exponential backoff with jitter and notes official SDKs retry transient errors "up to four times with an initial delay of approximately 1 second and a maximum delay of 60 seconds."

## Detail and citations

### 1. Model ID and free tier limits

- **gemini-3.5-flash exists and is stable.** https://ai.google.dev/gemini-api/docs/models (retrieved 2026-07-12) lists stable flash models `gemini-3.5-flash`, `gemini-3.1-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-flash-lite`, plus alias `gemini-flash-latest`. `gemini-2.0-flash` and `gemini-2.0-flash-lite` are shut down.
- **GA date.** https://ai.google.dev/gemini-api/docs/changelog (retrieved 2026-07-12): "May 19, 2026: Released gemini-3.5-flash, the generally available (GA) version of Gemini 3.5 Flash."
- **Model card.** https://ai.google.dev/gemini-api/docs/models/gemini-3.5-flash (retrieved 2026-07-12): input limit 1,048,576 tokens, output limit 65,536, supports structured outputs and caching, does not list rate limit numbers.
- **No published per-model table.** https://ai.google.dev/gemini-api/docs/rate-limits (retrieved 2026-07-12): "Rate limits depend on a variety of factors (such as your usage tier) and can be viewed in Google AI Studio." The page defines the three dimensions (RPM, TPM input, RPD) and gives an example ("if your RPM limit is 20, making 21 requests within a minute will result in an error"), but contains no free tier RPM, TPM, or RPD values for any text model. The only tier tables remaining are spend-based limits (Free: N/A) and Batch API enqueued token limits.
- **Consequence for the PRD.** The 1,500 RPD figure matches the historically published free tier table for flash models, but that table is gone from the primary source. Third-party sites still repeat 15 RPM, 250,000 TPM, 1,500 RPD for flash on free tier; those are secondary and were not accepted as evidence here. The authoritative number for this project is whatever https://aistudio.google.com/rate-limit shows when signed in to the project.

### 2. OpenAI-compatible endpoint and quota scope

- https://ai.google.dev/gemini-api/docs/openai (retrieved 2026-07-12): base URL `https://generativelanguage.googleapis.com/v1beta/openai/`, endpoints for chat completions, images, videos, embeddings, batches, and model listing. "Support for the OpenAI libraries is still in beta while we extend feature support." Unsupported parameters are silently ignored. The page defines no separate rate limits; it is the same API and the same project quota.
- https://ai.google.dev/gemini-api/docs/rate-limits (retrieved 2026-07-12): "Rate limits are applied per project, not per API key." Rotating keys within one project buys nothing.

### 3. Structured output and caching through the compat layer

- **Structured output.** https://ai.google.dev/gemini-api/docs/openai (retrieved 2026-07-12), "Structured output" section: "Gemini models can output JSON objects in any structure you define." The examples call `client.beta.chat.completions.parse(model="gemini-3.5-flash", ..., response_format=CalendarEvent)` in Python and `response_format: zodResponseFormat(CalendarEvent, "event")` in JavaScript. `zodResponseFormat` emits a `response_format` of type `json_schema`, so schema-constrained output works. A plain `json_object` mode is not shown on the page.
- **Implicit caching.** https://ai.google.dev/gemini-api/docs/caching (retrieved 2026-07-12): "Implicit caching is enabled by default for all Gemini 2.5 and newer models." Minimum prompt size for a hit: 4,096 tokens for gemini-3.5-flash (2,048 for the 2.5 models). Guidance: "Try putting large and common contents at the beginning of your prompt." Winnow's shape (static rubric first, transcript last, ~10k tokens) is exactly the recommended layout and clears the 4,096 minimum, so the rubric prefix should hit the implicit cache across the nightly batch.
- **Free tier benefit.** The caching page frames the benefit as cost: "We automatically pass on cost savings if your request hits caches." On the free tier tokens cost nothing, so there is no monetary benefit. Whether cache hits are discounted against the TPM meter is not documented; assume full input token count for TPM budgeting.
- **Cached token visibility.** Native SDKs surface it as `usage.total_cached_tokens` (caching page, retrieved 2026-07-12). The OpenAI compat page does not document a `prompt_tokens_details.cached_tokens` field in chat completions usage, so do not depend on it; if Winnow wants to observe cache hits, it may need the native `usageMetadata` via the Gemini SDK.
- **Explicit caching via compat.** Available only as `extra_body: {"google": {"cached_content": "cachedContents/..."}}` (OpenAI compat page, retrieved 2026-07-12). Not needed for Winnow given implicit caching.

### 4. Free tier gotchas for a nightly batch

- **Training on your data.** https://ai.google.dev/gemini-api/docs/pricing (retrieved 2026-07-12): free tier rows for flash models state "Content used to improve our products: Yes"; paid tier states No. Video transcripts sent for scoring will be used by Google. Acceptable for public YouTube transcripts, but the rubric prompt is also included.
- **Reset schedule.** https://ai.google.dev/gemini-api/docs/rate-limits (retrieved 2026-07-12): "Requests per day (RPD) quotas reset at midnight Pacific time." Not rolling. A nightly batch that starts after midnight Pacific gets a fresh RPD budget.
- **429 handling.** https://ai.google.dev/gemini-api/docs/troubleshooting (retrieved 2026-07-12): 429 RESOURCE_EXHAUSTED means "You've exceeded one of the API's rate limits (RPM, TPM, RPD, spend, etc.)". Guidance: "Use exponential backoff: Wait a short time before the first retry (for example, 1 second), then increase the delay exponentially (for example, 2s, 4s, 8s)"; "Add jitter"; "Retry on specific errors: Only retry on transient errors (like 429, 408, or 5xx)." No Retry-After header is documented anywhere on the page, so do not rely on one; if the response happens to carry one, honoring it is harmless.
- **Burst behavior.** RPM is evaluated per minute and any single dimension trips the limit, so a burst of 15+ back-to-back calls can 429 even though the daily budget is barely touched.
- **Not guaranteed.** Rate limits page: "Specified rate limits are not guaranteed and actual capacity may vary." Free tier capacity can degrade under load, so the worker must tolerate 429 even when under its own pacing.

## Pacing recommendation for the scoring worker

Assumptions: ~50 calls per night, ~10k input tokens per call, free tier, official numbers unpublished so pacing must be conservative against historical free tier flash limits (roughly 10 to 15 RPM, 250k TPM, 1,500 RPD).

- **Inter-call delay: 10 seconds fixed (6 RPM).** That is well under any historical free flash RPM and yields ~60k input tokens per minute at 10k tokens per call, far under any plausible TPM. Total batch time: about 9 minutes for 50 calls. There is no reason to run hotter for a nightly job.
- **429 backoff:** exponential with full jitter, initial delay 2s, factor 2, cap 60s, max 5 attempts per video. This mirrors the documented SDK behavior (about 1s initial, 60s max, four retries) with one extra attempt. If a Retry-After header is present, use max(header, computed backoff). Also retry 408 and 5xx; do not retry 400 or 403.
- **RPD exhaustion detection:** if 3 consecutive videos exhaust retries on 429, stop the batch and mark remaining videos as deferred. Persistent 429 late in a batch means a daily or capacity limit, and hammering wastes the remaining RPD. Deferred videos rejoin the next nightly run, which starts after the midnight Pacific reset.
- **Daily cap:** self-impose 200 requests per day in the worker (4x the normal batch, still 13 percent of the assumed 1,500 RPD). Even if Google has cut free flash RPD by an order of magnitude since the last published table, 50 to 200 calls per night remains safe. Log daily request counts so a future RPD change shows up as a trend before it breaks the batch.
- **Schedule:** run the batch shortly after 00:05 Pacific so the whole run sits inside one fresh RPD window.
- **Keep the rubric prefix byte-identical** across calls (no timestamps or per-video content before the transcript) to preserve implicit cache hits.

## Contradictions with the PRD

- The PRD cites 1,500 RPD as a documented free tier limit. As of 2026-07-12 Google no longer publishes that number; the rate limits page defers to per-project limits in AI Studio and disclaims guarantees. The PRD should reference the AI Studio rate limit page as the source of truth and treat 1,500 as an unverified historical figure.
