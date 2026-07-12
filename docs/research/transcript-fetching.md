# Transcript fetching: viability and failure handling

Research against primary sources (GitHub repos, source code, official docs) on 2026-07-12. All cited claims carry that retrieval date. YouTube's anti-bot behavior changes often, so re-verify before relying on any specific behavior here.

## TL;DR

1. **youtube-transcript-api works from a residential IP today.** The library is actively maintained (v1.2.4 released 2026-01-29, repo pushed 2026-05-19). Its documented blocking problem is cloud provider IPs: the README and the `RequestBlocked` error text say YouTube blocks "most IPs that are known to belong to cloud providers (like AWS, Google Cloud Platform, Azure, etc.)". A July 2026 issue thread confirms the split in practice: "Works locally, Blocked on AWS". Residential IPs can still get blocked by volume ("You have done too many requests"), but Winnow's scale (tens of fetches per night) is far below any reported trigger.
2. **The library has a precise exception taxonomy** rooted at `CouldNotRetrieveTranscript`, which maps cleanly onto Winnow's permanent versus transient split (full mapping in section 4). Built-in proxy support exists (`WebshareProxyConfig` rotating residential, `GenericProxyConfig` for HTTP or HTTPS) but is unnecessary for a local residential deployment.
3. **One live edge case: `PoTokenRequired`.** Some videos' caption URLs carry an `exp=xpe` parameter and the timedtext endpoint returns an empty body without a Proof of Origin token. Open issue #592; the maintainer says it "really shouldn't happen with the most current version" but at least one user still reports it on v1.2.4. Treat as unknown, not permanent.
4. **Best fallback is yt-dlp**, which is extremely actively maintained (release 2026.07.04) and has a PO token plugin ecosystem. Its known YouTube subtitle 429 problem affects only auto-translated subtitles; per maintainer bashonly (2026-01-06): "Manual subtitles and original language automatic captions are not affected by this HTTP Error 429 issue." Winnow only needs original-language captions.
5. **The official Data API `captions.download` is a non-option** for third-party videos: it requires OAuth (`youtube.force-ssl` or `youtubepartner` scope), requires "permission to edit the video", returns 403 otherwise, and costs 200 quota units per call.
6. **Recommendation:** youtube-transcript-api direct, no proxy, as primary. Classify exceptions per section 4. Add yt-dlp as fallback only if `PoTokenRequired` or `YouTubeDataUnparsable` shows up in practice; do not build it preemptively.

## 1. youtube-transcript-api (jdepoix/youtube-transcript-api)

### Maintenance status

- Latest release v1.2.4, published 2026-01-29. Repo last pushed 2026-05-19, 7,869 stars, 31 open issues. https://github.com/jdepoix/youtube-transcript-api/releases (retrieved 2026-07-12 via GitHub API)
- Recent releases are small fixes (v1.2.4: Webshare `-rotate` suffix fix; v1.2.3: Python 3.14 support; v1.2.1: `filter_ip_locations` for Webshare). The core fetch path has been stable since the v1.2.0 breaking cleanup of deprecated static methods (2025-07-21). https://github.com/jdepoix/youtube-transcript-api/releases

### Residential IP viability

- README: "YouTube has started blocking most IPs that are known to belong to cloud providers (like AWS, Google Cloud Platform, Azure, etc.)", causing `RequestBlocked` or `IpBlocked`. https://github.com/jdepoix/youtube-transcript-api?tab=readme-ov-file#working-around-ip-bans-requestblocked-or-ipblocked-exception (retrieved 2026-07-12)
- Issue #593 (opened 2026, "Cloud IP blocking in 2026"): "Direct usage (no proxy): Works locally, Blocked on AWS". The entire IP-ban issue corpus (#485 AWS Lambda, #511, #549, #552) is about cloud deployments or high-volume proxy use, not residential desktops. https://github.com/jdepoix/youtube-transcript-api/issues/593 (retrieved 2026-07-12)
- The `RequestBlocked` message names two causes: cloud IP, or "You have done too many requests and your IP has been blocked by YouTube". No numeric threshold is published anywhere in the repo. Winnow's nightly batch of roughly 50 fetches with pacing is orders of magnitude below the bulk-scraping volumes in the issue reports. https://github.com/jdepoix/youtube-transcript-api/blob/master/youtube_transcript_api/_errors.py (retrieved 2026-07-12)

### Proxy support (available, not needed here)

- `WebshareProxyConfig`: rotating residential proxies, with `filter_ip_locations` since v1.2.1. The error text warns Webshare free tier and "Proxy Server" or "Static Residential" plans do not work. https://github.com/jdepoix/youtube-transcript-api?tab=readme-ov-file#using-webshare (retrieved 2026-07-12)
- `GenericProxyConfig`: any HTTP or HTTPS proxy. SOCKS is currently broken (open issue #603, "Missing dependencies for SOCKS support"; feature request #602). https://github.com/jdepoix/youtube-transcript-api/issues/603 (retrieved 2026-07-12)
- Cookie authentication is temporarily unsupported: the `AgeRestricted` error text says "Cookie Authentication is temporarily unsupported in youtube-transcript-api, as recent changes in YouTube's API broke the previous implementation." So age-restricted videos are currently unfetchable by this library, full stop. https://github.com/jdepoix/youtube-transcript-api/blob/master/youtube_transcript_api/_errors.py (retrieved 2026-07-12)

### Exception taxonomy (from `_errors.py` at master, retrieved 2026-07-12)

Source: https://github.com/jdepoix/youtube-transcript-api/blob/master/youtube_transcript_api/_errors.py

All fetch failures subclass `CouldNotRetrieveTranscript`, which subclasses `YouTubeTranscriptApiException`.

| Exception | Meaning |
|---|---|
| `TranscriptsDisabled` | "Subtitles are disabled for this video" |
| `NoTranscriptFound` | No transcript in any requested language code (carries the available `TranscriptList`) |
| `VideoUnavailable` | "The video is no longer available" (deleted, private) |
| `VideoUnplayable` | Unplayable with a YouTube-supplied reason and sub-reasons (members-only, region lock, etc.) |
| `AgeRestricted` | Age-restricted; needs auth, which the library currently cannot do |
| `InvalidVideoId` | Caller passed a URL instead of an ID |
| `RequestBlocked` | "YouTube is blocking requests from your IP" (cloud IP or too many requests) |
| `IpBlocked` | Subclass of `RequestBlocked`, same meaning; `except RequestBlocked` catches both |
| `YouTubeRequestFailed` | Wraps a `requests.HTTPError`; carries `.reason` with the HTTP status text |
| `YouTubeDataUnparsable` | Watch-page data not parsable, "This should not happen, please open an issue" (YouTube changed markup or library bug) |
| `PoTokenRequired` | "The requested video cannot be retrieved without a PO Token. If this happens, please open a GitHub issue!" |
| `NotTranslatable`, `TranslationLanguageNotAvailable` | Translation-specific; only reachable when requesting translated transcripts |
| `FailedToCreateConsentCookie` | EU consent-cookie automation failed |
| `CookieError` (`CookiePathInvalid`, `CookieInvalid`) | Cookie file problems; dead code path while cookie auth is disabled |

Note: plain network failures (DNS, connection reset, timeout) are not wrapped; they surface as `requests.exceptions.RequestException` subclasses and must be caught separately.

### Live issues to know about

- **#592 (open): `PoTokenRequired` with `exp=xpe`.** For some videos the caption track `baseUrl` contains `exp=xpe` and the timedtext endpoint returns an empty HTTP 200 body for any programmatic request; cookies do not help because the PO token is generated at runtime by the player JS. Maintainer jdepoix (2026-06-15): "This really shouldn't happen with the most current version of the module. If this happens to anyone else, please let me know!" One user confirmed it still happens (2026-06-24). Scope appears narrow but real. https://github.com/jdepoix/youtube-transcript-api/issues/592 (retrieved 2026-07-12)
- **#612 (open): retry on 429 never rotates IP for InnerTube POST** because urllib3's default `Retry` excludes POST. Only matters when using rotating proxies. https://github.com/jdepoix/youtube-transcript-api/issues/612 (retrieved 2026-07-12)

## 2. Alternatives

### yt-dlp subtitle extraction

- **Maintenance:** releases 2026.07.04, 2026.06.09, 2026.03.17. The most actively maintained YouTube tool in existence. https://github.com/yt-dlp/yt-dlp/releases (retrieved 2026-07-12)
- **Mechanics:** `--skip-download --write-subs --write-auto-subs --sub-langs "en.*" --sub-format json3`. Supported YouTube subtitle formats are `('json3', 'srv1', 'srv2', 'srv3', 'ttml', 'srt', 'vtt')` per `_SUBTITLE_FORMATS` in the extractor. `json3` gives timed segments trivially parseable in Python. https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_video.py and https://github.com/yt-dlp/yt-dlp#subtitle-options (retrieved 2026-07-12)
- **Rate limits:** the long-running "HTTP Error 429 on subtitles" issue #13831 was scoped down by maintainer bashonly (2026-01-06): "Manual subtitles and original language automatic captions are not affected by this HTTP Error 429 issue. Only subtitles/captions that have been automatically translated into another language are affected." Workarounds for translated subs: fresh browser cookies, or `--sleep-subtitles 60`. For batches, maintainers recommend the `-t sleep` preset (`--sleep-subtitles 5 --sleep-requests 0.75 --sleep-interval 10 --max-sleep-interval 20`). https://github.com/yt-dlp/yt-dlp/issues/13831#issuecomment-3712613129 (retrieved 2026-07-12)
- **PO tokens:** YouTube requires Proof of Origin tokens for some request types; for subtitles specifically the `web` and `web_safari` clients require them, and missing tokens "may return HTTP Error 403, or result in your account or IP address being blocked". yt-dlp's default clients are `('android_vr', 'web_safari')` and it routes around PO token requirements per client; the `bgutil-ytdlp-pot-provider` plugin generates tokens automatically when needed. This is the key advantage over youtube-transcript-api, which has no PO token story at all (issue #592). https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide and https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/extractor/youtube/_video.py (retrieved 2026-07-12)
- **Integration complexity for Winnow:** medium. Either shell out or embed `yt_dlp.YoutubeDL` in-process. Costs: a large dependency that must be kept current (YouTube breaks old versions), errors surfaced as `yt_dlp.utils.DownloadError` with message strings rather than typed exceptions (classification requires string matching), and you parse json3 yourself. Reliability from residential IP for original-language subs: good, per the maintainer statement above.

### Official YouTube Data API `captions.download`

- Requires OAuth with `https://www.googleapis.com/auth/youtube.force-ssl` or `https://www.googleapis.com/auth/youtubepartner`; no API-key access. "This method requires the user to have permission to edit the video"; otherwise 403 "The permissions associated with the request are not sufficient to download the caption track." Quota cost 200 units per call. https://developers.google.com/youtube/v3/docs/captions/download (retrieved 2026-07-12)
- Conclusion: only works for videos on channels you own or manage. Useless for Winnow's subscriptions. Not a fallback, not even a partial one.

### pytubefix

- Actively maintained (v10.10.1 released 2026-06-24, repo pushed same day, 1,531 stars) and has caption support. https://github.com/JuanBindez/pytubefix/releases (retrieved 2026-07-12)
- No documented proxy or IP-block handling comparable to the other two, and a much smaller contributor base than yt-dlp. Not recommended over yt-dlp as fallback; listed for completeness.

### Managed transcript APIs (Supadata and similar)

- Issue #593 reports Supadata "Works reliably, No blocking issues, But introduces external dependency + cost". These exist to solve the cloud-IP problem, which Winnow does not have. Skip. https://github.com/jdepoix/youtube-transcript-api/issues/593 (retrieved 2026-07-12)

## 3. Recommendation

**Primary: youtube-transcript-api, direct connection, no proxy.** It matches the PRD's assumption (PRD line 73), is a small typed-exception dependency, and residential IPs are not its failure mode at Winnow's volume. Pace fetches (a few seconds between videos) as cheap insurance against the "too many requests" ban cause.

**Fallback: yt-dlp, deferred.** Wire the fetcher so a second strategy can slot in, but only add yt-dlp if `PoTokenRequired` or `YouTubeDataUnparsable` occurs at meaningful rates in practice. When added: original-language subs only (`--write-subs --write-auto-subs`, never translated), `json3` format, `-t sleep` preset. yt-dlp is the correct fallback because it is the only option with a working PO token path.

**Do not use** the Data API captions endpoint (OAuth, own-videos-only) or a proxy service (solves a problem Winnow does not have).

## 4. Failure classification for the fetcher

Ordering matters: catch `IpBlocked` or `RequestBlocked` before generic `CouldNotRetrieveTranscript`, since everything subclasses the latter.

### (a) Permanent, no transcript. Set `transcript_status = no_transcript`, score metadata-only, never retry

| Signal | Why permanent |
|---|---|
| `TranscriptsDisabled` | Uploader disabled captions |
| `NoTranscriptFound` | No caption track in requested languages; if Winnow accepts any language (PRD open question, line 181), request broadly so this truly means none exist |
| `VideoUnavailable` | Deleted or private |
| `VideoUnplayable` | Members-only, region-locked, etc.; not retryable without auth |
| `AgeRestricted` | Library cannot authenticate at all right now |
| `InvalidVideoId` | Caller bug; log loudly, but retrying is pointless |
| `NotTranslatable`, `TranslationLanguageNotAvailable` | Only if requesting translations, which Winnow should not |

### (b) Transient. Retry with backoff, or defer to next nightly run

| Signal | Handling |
|---|---|
| `RequestBlocked`, `IpBlocked` | IP-level, not video-level. Do not retry per video: trip a circuit breaker, abort the remaining batch, defer all unfetched videos to the next run. Repeated occurrence across runs means escalation (pacing, or the proxy path) |
| `YouTubeRequestFailed` | Inspect `.reason` for the status: 429 and 5xx retry with exponential backoff and jitter (2s initial, cap 60s, 3 attempts) then defer; 4xx other than 429 goes to unknown |
| `requests.exceptions.ConnectionError`, `Timeout`, other `RequestException` | Plain network failure, not wrapped by the library; retry then defer |

### (c) Unknown. Bounded retries across runs, then downgrade

| Signal | Handling |
|---|---|
| `PoTokenRequired` | Not IP-related, retrying from the same client will not fix it (issue #592). If the yt-dlp fallback exists, invoke it; otherwise mark `transcript_status = fetch_failed`, re-attempt on the next 2 nightly runs, then downgrade to metadata-only scoring with a distinct status so it is distinguishable from true `no_transcript` |
| `YouTubeDataUnparsable` | Usually means YouTube changed markup and the library needs an update; same bounded-retry-then-downgrade, plus surface it so the operator checks for a library upgrade |
| `FailedToCreateConsentCookie` | EU consent flow hiccup; treat as transient once, then unknown |
| Any other `CouldNotRetrieveTranscript` or unexpected exception | Same bounded-retry-then-downgrade; log the class name for future mapping |

Schema note: the PRD's `transcript_status` (line 136) should distinguish at least `ok`, `no_transcript` (permanent, confident), and `fetch_failed` (unknown or exhausted retries) so the UI's "unscored" section (PRD line 172) can tell "this video has no captions" from "we could not get them".

### yt-dlp fallback signal mapping (if and when added)

yt-dlp raises `yt_dlp.utils.DownloadError` with message text rather than typed exceptions, so classification is string-based: absence of any requested subtitle track in the extracted info dict (empty `subtitles` and `automatic_captions` for requested langs) means permanent no_transcript; "HTTP Error 429" means transient; "Video unavailable", "Private video", "This video is available to this channel's members" mean permanent; anything else is unknown. https://github.com/yt-dlp/yt-dlp/blob/master/yt_dlp/utils/_utils.py (DownloadError definition; retrieved 2026-07-12)

## Unverified claims

- **No published request-volume threshold** for when a single residential IP gets blocked. The claim that Winnow's ~50 fetches per night is safe is inference from the absence of low-volume residential complaints in the issue tracker, not a documented guarantee.
- **Prevalence of the `exp=xpe` PoTokenRequired case** is unknown; the maintainer believes v1.2.4 should not hit it, one user reports otherwise. Only production telemetry will settle it.
- **PO token enforcement for subtitles is "rolling out"** per the yt-dlp wiki; whether residential IPs will eventually need PO tokens for plain caption fetches is not knowable from current sources.
