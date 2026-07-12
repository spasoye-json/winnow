# PRD: Winnow — Personal YouTube Quality Curator

**Version:** 0.1 (MVP)
**Owner:** [You]
**Status:** Draft for implementation
**Target:** Single-user personal tool, local-first

---

## 1. Problem

YouTube's recommendation algorithm optimizes for watch time and engagement, not substance. The result: a feed dominated by clickbait, recycled takes, AI-narrated compilations, and padded videos where 10 minutes of content is stretched to 40. Finding genuinely high-quality videos requires manually wading through garbage.

## 2. Solution Overview

A personal web app that builds an alternative YouTube feed ranked by **content quality** instead of engagement. It ingests videos from user-defined sources, scores each one using an LLM analyzing the transcript and metadata, and presents only videos above a quality threshold.

**Core thesis:** Slop is detectable from transcripts. Padding, recycled content, and clickbait gaps (title promises vs. actual content) are all visible in text, even when thumbnails hide them.

## 3. Goals

- Replace the YouTube homepage as the user's primary discovery surface
- Score every ingested video on a defined quality rubric with an LLM
- Surface only videos above a configurable threshold, ranked by score
- Run cheaply on free/low-cost API tiers (single user)

## 4. Non-Goals (MVP)

- Multi-user support, auth, or accounts
- Mobile app (responsive web is enough)
- Platforms other than YouTube
- Comments, social features, or sharing
- Watching videos in-app (link out to YouTube)
- Training a custom ML model (LLM prompting only)

## 5. User Stories

1. As a user, I connect my Google account once, and my existing YouTube subscriptions (already self-curated) become the source list automatically — kept in sync on each ingest run.
1a. As a user, I can optionally add extra channels or topic queries beyond my subscriptions, and exclude specific subscribed channels from the feed.
2. As a user, I open the app and see a feed of recent videos ranked by quality score, not engagement.
3. As a user, I click any video to see its score breakdown (why it scored high/low) before deciding to watch.
4. As a user, I can adjust my quality threshold and rubric weights to tune what "good" means for me.
5. As a user, I can mark a video as "great" or "slop" after watching, so I can later audit whether scores match my taste.

## 6. System Architecture

```
[Google OAuth] → [Subscription Sync] ─┐
                                      ▼
[Scheduler (cron)] → [Ingest Worker] → [SQLite DB] ← [Scoring Worker] → [Anthropic API]
                          ↓                                ↓
                   [YouTube Data API]              [Transcript fetcher]

[Web UI (feed + detail + settings)] ← reads from SQLite
```

### Components

**Google OAuth + subscription sync**
- One-time "Connect Google" flow using OAuth 2.0 with the `https://www.googleapis.com/auth/youtube.readonly` scope
- On connect and at the start of each ingest run: call `subscriptions.list(mine=true)` (paginated, 1 quota unit/page) and upsert channels into the DB with `source='subscription'`
- Channels removed from subscriptions are deactivated (not deleted) on next sync
- Store refresh token locally (encrypted at rest or OS keychain; `.env`-adjacent file acceptable for a personal local tool)
- Manual channels (`source='manual'`) and topic queries remain supported as optional supplements; per-channel `excluded` flag lets the user mute a subscription without unsubscribing on YouTube

**Ingest worker** (runs on schedule, e.g. every 6 hours)
- Runs subscription sync first, then pulls latest videos from the upload playlists of all active, non-excluded channels, plus optional topic search queries
- Uses YouTube Data API v3 (free tier: 10,000 quota units/day — budget queries accordingly; `search.list` costs 100 units, `playlistItems.list` costs 1 unit, so prefer channel upload playlists over search)
- Stores video metadata: id, title, channel, description, duration, publish date, view count, thumbnail URL
- Dedupes against existing DB rows

**Transcript fetcher**
- Fetches transcript per video (e.g. `youtube-transcript-api` Python package or equivalent)
- Handles missing transcripts gracefully: flag video as `no_transcript`, score on metadata only with a confidence penalty

**Scoring worker**
- For each unscored video: send transcript (truncated to ~15k tokens if longer) + metadata to the configured LLM with the scoring rubric prompt
- **Provider-agnostic design:** use a single OpenAI-compatible chat-completions client where `base_url`, `model`, and `api_key` come from settings/env. Supported out of the box:
  - `gemini-3.1-flash-lite` via Google AI Studio's OpenAI-compatible endpoint — **default** (measured free-tier limits for this project: 500 requests/day, 15 RPM, 250k input TPM — comfortably above expected ~50/day volume → $0; source of truth is the project's AI Studio rate-limit page, limits are per-project and not guaranteed). `gemini-3.5-flash` is limited to 20 requests/day on this project's free tier and is not wired into the pipeline
  - `deepseek-v4-flash` via `https://api.deepseek.com` — near-free fallback (~$0.14/M input; automatic prefix caching cuts the repeated rubric prompt to ~$0.0028/M). **Important:** disable thinking mode (non-thinking variant) — reasoning tokens bill as output and add nothing to a rubric-scoring task
  - Any Claude model via the Anthropic-compatible path if the user later prefers it
- Prompt structure must put the static rubric/system prompt first and the per-video transcript last, byte-identical prefix across calls, to maximize provider-side prompt caching
- Requests structured JSON output: per-dimension scores, overall score, one-paragraph rationale, 2-3 sentence video summary
- Stores results in DB with the `model` name used (already in schema) so scores from different models can be compared later; marks failures for retry with backoff
- Cost/quota control: score only videos from the last N days; cap daily scoring volume; on Gemini free-tier rate limits (requests/minute), queue with backoff rather than fail

**Web UI**
- Feed view: cards with thumbnail, title, channel, quality score badge, LLM-written summary; sorted by score, filterable by topic/channel/date
- Detail view: full score breakdown per rubric dimension + rationale; link to YouTube
- Settings view: manage channels, topics, threshold, rubric weights
- Feedback: thumbs up/down per video stored in DB

## 7. Quality Scoring Rubric

Each dimension scored 0–10 by the LLM. Overall score = weighted average (weights user-configurable, defaults below).

| Dimension | Weight | What it measures |
|---|---|---|
| Information density | 25% | Substance per minute; is there a clear thesis and payload, or filler? |
| Originality | 20% | Original research/analysis/experience vs. recycled takes, reaction content, compilation |
| Clickbait gap | 20% | Does the content deliver what the title/thumbnail promises? (10 = fully delivers) |
| Padding ratio | 15% | Intros, sponsor reads, repetition, "before we start" — how much is stalling? |
| Depth & rigor | 15% | Sources cited, nuance, acknowledges counterpoints vs. surface-level hot takes |
| Production integrity | 5% | Signs of AI-generated narration/script farms, mass-produced templates |

**Hard flags** (auto-fail below threshold regardless of score): detected AI voice content farm, transcript is <20% of what duration implies (pure visual filler is fine for some genres — make this flag genre-aware or user-toggleable).

**Scoring prompt requirements:**
- System prompt defines each dimension with 2–3 concrete examples of high and low scores
- Must return valid JSON only (schema below)
- Include video duration and view count as context but instruct the model to ignore popularity as a quality signal

```json
{
  "scores": {
    "info_density": 0, "originality": 0, "clickbait_gap": 0,
    "padding": 0, "depth": 0, "production": 0
  },
  "overall": 0.0,
  "hard_flags": [],
  "summary": "2-3 sentence neutral summary of the video content",
  "rationale": "1 paragraph explaining the scores"
}
```

## 8. Data Model (SQLite)

```sql
oauth_credentials(id, provider TEXT DEFAULT 'google', refresh_token TEXT,
                  access_token TEXT, expires_at, scopes TEXT, connected_at)
channels(id, yt_channel_id UNIQUE, name, source TEXT CHECK(source IN ('subscription','manual')),
         excluded INTEGER DEFAULT 0, active INTEGER DEFAULT 1, added_at, last_synced_at)
topics(id, query, added_at, active)
videos(id, yt_video_id UNIQUE, channel_id, title, description,
       duration_sec, published_at, view_count, thumbnail_url,
       transcript_status, ingested_at)
scores(video_id FK, overall REAL, info_density, originality,
       clickbait_gap, padding, depth, production,
       hard_flags TEXT, summary TEXT, rationale TEXT,
       model TEXT, scored_at)
feedback(video_id FK, verdict TEXT CHECK(verdict IN ('great','slop')), created_at)
settings(key, value)  -- threshold, weights JSON, schedule
```

## 9. Tech Stack (suggested — implementer may adjust)

- **Backend:** Python (FastAPI) — best library support for `youtube-transcript-api` and `google-api-python-client`
- **DB:** SQLite (single user, zero ops)
- **Frontend:** Server-rendered templates (Jinja2) or a minimal React/Vite app — keep it simple
- **Scoring:** OpenAI-compatible client (`openai` Python package with custom `base_url`) — default model `gemini-3.1-flash-lite` on Google AI Studio free tier; `deepseek-v4-flash` (non-thinking) as alternate; provider swappable via config
- **Auth:** `google-auth-oauthlib` + `google-api-python-client` for the OAuth flow and YouTube API calls; localhost redirect URI for the desktop-style flow
- **Scheduler:** APScheduler in-process, or system cron hitting a CLI command
- **Secrets:** `.env` file — `YOUTUBE_API_KEY`, `SCORING_BASE_URL`, `SCORING_MODEL`, `SCORING_API_KEY`

## 10. MVP Milestones

1. **M1 — Connect & Ingest:** DB schema, Google OAuth flow, subscription sync, ingest worker pulling metadata into SQLite. *Done when: connecting a Google account populates the user's subscriptions and their recent videos.*
2. **M2 — Scoring:** Transcript fetch + LLM scoring pipeline with structured output and retry handling. *Done when: every ingested video gets a score row and rationale.*
3. **M3 — Feed UI:** Ranked feed, detail view with breakdown, threshold filter. *Done when: user can browse the curated feed end to end.*
4. **M4 — Tuning:** Settings UI for weights/threshold, feedback buttons, and a simple audit view comparing user verdicts vs. scores.

## 11. Cost & Quota Estimates

- YouTube API: ~50 channels checked 4×/day via upload playlists ≈ 200 units/day (well under 10k free quota). Avoid `search.list` where possible.
- LLM scoring, default path (Gemini 3.5 Flash free tier): ~50 videos/day ≪ 1,500 requests/day quota → **$0/month**. Watch requests-per-minute limits on the free tier; the worker should pace calls.
- LLM scoring, fallback path (DeepSeek V4 Flash, non-thinking): ~50 videos/day × ~10k input tokens ≈ 500k tokens/day ≈ $0.07/day worst case, and materially less with prefix cache hits on the rubric prompt → roughly **$1–2/month**.
- Note: free-tier Gemini requests may be used by Google for product improvement; acceptable here since transcripts are public content. If that ever becomes a concern, switch provider via config.

## 12. Risks & Mitigations

- **Google OAuth app verification:** `youtube.readonly` is a sensitive scope. A personal OAuth client in "Testing" mode expires refresh tokens every 7 days (forcing weekly re-login). Mitigation: set the OAuth consent screen's publishing status to "In production" without submitting for verification — Google shows an "unverified app" warning at consent (fine for personal use, cap of 100 users) but refresh tokens then persist. Document this in setup instructions.
- **Transcript unavailable** (disabled, auto-caption missing, non-English): score metadata-only with confidence penalty; surface as "unscored" section rather than hiding.
- **LLM rubric drift / miscalibration:** the feedback loop (user verdicts vs. scores) exists specifically to audit this; iterate on the rubric prompt, not the architecture.
- **Genre bias:** transcript-based scoring penalizes visual-first content (art, gameplay). MVP accepts this; note it in UI. Later: per-genre rubrics.
- **YouTube API/ToS constraints:** metadata via official API only; transcripts via public caption endpoints; personal use.

## 13. Open Questions (implementer may decide)

- Truncation strategy for very long transcripts: head+tail sampling vs. chunk-and-summarize
- Whether to backfill a channel's history on add, or only score new uploads going forward
- English-only for MVP, or pass non-English transcripts through as-is (modern LLMs handle them fine — cost is the only concern)
- Whether to add a `SCORING_FALLBACK_MODEL` that automatically retries on a second provider when the primary hits rate limits or outages

## 14. Success Criteria

After 2 weeks of daily use: the user opens this feed instead of YouTube's homepage, and ≥80% of videos they mark "great" scored above threshold while ≥80% marked "slop" scored below it.
