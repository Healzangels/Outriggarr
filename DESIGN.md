# Outriggarr — design draft v0.3

YouTube → Sonarr/Radarr bridge. Name: an outrigger is the float rigged alongside a hull to keep it stable — this app rides alongside the *arr stack and never replaces it.

**Changes from v0.1:** adds a web GUI, Radarr as a second target, manual grabs and playlist mapping, and moves configuration from YAML into a database because the GUI edits it. The core pipeline (stage → *arr manual import) is unchanged.

**Changes from v0.2 (M0 implementation):** name settled; the job dedupe key is a stored `target_key` column rather than a constraint over the raw target ids; runtime paths/port come from `OUTRIGGARR_*` env vars.

## Goal

Sonarr and Radarr stay the management platforms: they decide what is wanted (monitoring), acceptable quality, naming, folder layout, and notifications. This app is a companion that fills their *wanted* lists from YouTube (or any yt-dlp site) and hands finished files back through their own import APIs.

"Monitored in this app" means *a Sonarr series has a source attached here*. Which episodes are actually wanted still comes from Sonarr's monitoring. The app never has its own notion of a monitored episode, and it never writes into a Sonarr/Radarr root folder — only into a staging directory both containers can see.

## Scope

**v1**

- Web GUI: connections, series subscriptions, manual grab (single URL or playlist), activity/queue.
- Sonarr: automatic (subscriptions) and manual.
- Radarr: manual only — paste a URL, pick the movie. Channels do not map to movie lists in any reliable way.
- One instance each of Sonarr and Radarr.

**Later (explicitly not v1)**

- Radarr auto-matching (decided 2026-09-02 to stay here: films Radarr wants are rarely on YouTube legitimately, a search-per-movie returns trailers, reviews and fan cuts, and a wrong import makes Radarr stop looking for the real film; Grab covers the real case. If it ever returns it is the channel-based shape — a channel's uploads against the whole wanted list, exact title only, a tight runtime check against TMDB, held for approval by default — never a YouTube search); multiple *arr instances; built-in auth; a public API key for automation; Torznab/download-client emulation so grabs show in Sonarr's queue; quality upgrades (cutoff-unmet).

**Pulled forward (2026-09-02, owner's call): notifications via Apprise** — only for Outriggarr's own events, which nothing else can report: a job failing for good (retries exhausted, import rejected, internal error), a subscription scan error (announced once per new error text, not every interval), and optionally a job import (off by default because the *arr announces it). URLs and toggles live in Settings → Notifications with a *Send test* button; delivery never affects a job.

## Prior art

- `ryakel/stream-harvestarr` (maintained fork of `whatdaybob/sonarr_youtubedl`; GPL-3.0): config.yml only, no GUI, downloads straight into the Sonarr root and rescans, requires the TVDB title to match the upload title. Features worth matching: per-series format, air-date offset for pre-release series, cookies.txt, rate-limit backoff, subtitles, regex. Reading it for ideas is fine; copying code would put this project under GPL.
- `MegaR/Tuberr`, `derekantrican/subarr`, Tube Archivist, TubeSync (already running on CMacServer): YouTube-first tools that either replace Sonarr or feed Plex directly. Sonarr is not in the loop.
- Sonarr issue #6445: TVDB carries YouTube-native shows and Sonarr will add them with "YouTube" as the network, but has no way to download them. That gap is this project.

Differentiators: files go through the *arr import pipeline (rename, quality, notifications); imports carry explicit episode/movie IDs so no filename parsing is load-bearing; a GUI with match preview so bad matches are caught before download.

## Architecture

One container, one process:

```
Browser ──► FastAPI (HTML pages + JSON API) ──► SQLite (/config/app.db)
                                                    ▲
                     Worker (background task) ──────┘
                       ├─ Scheduler: scan subscriptions → create jobs
                       └─ Runner: job → yt-dlp → /staging → ArrClient.manual_import()
```

The KISS insight: **everything is a Job**. A subscription scan, a pasted URL, and a mapped playlist all produce the same `Job(target, video)` row, and one runner processes them. Subscriptions are just job factories.

### Why not the alternatives

| | Chosen: pull/reconcile + GUI | Torznab indexer + SABnzbd emulation |
|---|---|---|
| Who initiates | App polls wanted lists; user queues manual jobs | Sonarr searches and grabs |
| *arr-side config | API key + shared staging mount | Add an indexer and a download client per instance |
| Benefit | Small surface; GUI gives queue visibility without emulating anything | Grabs show in Sonarr's own queue |
| Risk | App owns scheduling/retries | Two protocol emulations to keep compatible; on-demand searches need pre-indexed channels |

Deferred, not rejected. Source, matcher, and runner carry over unchanged if it is added.

## Domain model

| Table | Fields (essentials) |
|---|---|
| `connection` | id, kind (`sonarr`/`radarr`), name, url, api_key, staging_path_remote (how that *arr sees `/staging`), enabled |
| `subscription` | id, connection_id, series_id (Sonarr's), tvdb_id, title (snapshot for display), sources (JSON list of channel/playlist/collection URLs), format (nullable → global default), video_limit (nullable → the global scan limit), audio_language (nullable → source-declared, then global), auto_download (`future`/`all`/`none`), created_at, strategies, date_tolerance_days, date_offset_days, title_regex, title_require, enabled, last_scan_at, last_scan_result |
| `override` | subscription_id, video_id, season, episode, video_url, video_title (the last two set when the override was pasted as a URL, so a scan can use a video outside the source's newest-N listing) |
| `job` | id, connection_id, target_kind (`episode`/`movie`), series_id, episode_ids (JSON), movie_id, target_key, target_label (display only, supplied by the creator), video_id, video_url, video_title, status, progress_pct, staged_path, error, attempts, next_retry_at, created_at, finished_at |
| `setting` | key, value — scan interval, concurrency, default format, merge container, yt-dlp extra opts (JSON), cookies path |

Job status: `queued → downloading → importing → done | failed | cancelled`. Dedupe is a partial unique index on `(connection_id, target_key, video_id)` **over jobs that are not `done`** (migration 0004), where `target_key` is derived from the target ids (`episode:<series_id>:<sorted episode ids>` or `movie:<movie_id>`). A done job is history: if Sonarr later loses the file, the same video can be queued again by a scan or a grab. Failed and cancelled jobs still block a duplicate — Retry them instead.

## Job pipeline

1. **Download**: yt-dlp (library, not subprocess) into `/staging/<job-id>/<parseable name>.<ext>`, video+audio merged by ffmpeg. Progress hook writes `progress_pct` to the job row. Caption tracks in `subtitles_langs` (uploader captions; auto-generated only if `subtitles_auto`) are fetched and converted to `.srt` sidecars named after the staged video, which the *arr imports as extra files (the connection Test warns when Import Extra Files is off). Then one ffmpeg stream-copy remux (first video stream, every audio stream, subtitles if any; never `-map 0`, which archive.org's hint and cover tracks break) stamps a language on the audio streams — YouTube tracks are untagged and Plex showed them as *Unknown* (found after M4 on the first Hot Ones imports). Which language, in order: the subscription's own `audio_language` (the operator's word), else the language the source declares for the chosen audio track (YouTube sets one per track and prefers the original on dubbed videos; `ja` → `jpn` via a BCP-47 → ISO 639-2 map, "undetermined" counts as none), else the global `audio_language` setting (default `eng`); blank everywhere leaves the file untagged. So anime stays Japanese without configuration, and the tag applied is logged per job. A failure there is noted on the job but does not stop the import.
2. **Import** via `ArrClient` (one interface, two implementations):
   - `GET /api/v3/manualimport?folder=<staging_path_remote>/<job-id>&filterExistingFiles=true`
   - Take the returned entry. Its rejections were computed without our ids, so if there are any, `POST /api/v3/manualimport` (the reprocess call the *arr UI uses) re-evaluates the entry with `seriesId` + `episodeIds` (+ `seasonNumber`) or `movieId`; only rejections that survive that block the import ("Unknown Series" alone never does). Then set `seriesId` + `episodeIds` (Sonarr) or `movieId` (Radarr), `quality`, `languages`.
   - `POST /api/v3/command {"name": "ManualImport", "files": [...], "importMode": "move"}`
   - Poll `GET /api/v3/command/{id}` until completed; surface rejection reasons from the GET step verbatim in the job's `error`.
3. The *arr moves and renames the file into the library and fires its own notifications.
4. Remove the job's staging folder.

**M2 decisions.** The runner asks the *arr for the target first (`target_info`: titles for the staging name, and `hasFile`), then downloads, renames to the parseable name, and asks again right before import: a target that already has a file ends the job as `done` with the note *target already had a file; nothing imported* and the staged file is discarded (`done`, not `cancelled`: a non-done job keeps covering its episode, and this case is usually our own earlier import that the *arr moved, so the episode must stay re-acquirable). The `GET /manualimport` call passes only `folder` and `filterExistingFiles`: adding `seriesId` (Sonarr) or `movieId` (Radarr) switches the *arr into listing that series'/movie's own library folder and ignoring `folder` (proven live 2026-09-01, first M2 run). The parseable staging name is what lets the *arr pre-resolve the episode; the POST still carries explicit ids. Languages come from the candidate, or English when the *arr could only say Unknown. After the command completes the target is read once more; "completed" without `hasFile` is a failure that keeps the staged file. A job whose staged file still exists skips the download on its next run (crash recovery and *arr-side retries). A shutdown while polling the import command keeps the staged file and re-queues the job without consuming an attempt; a command still running at the poll timeout (10 min) is a retryable failure, not a terminal one; a multi-episode target with some of its files already present is refused (terminal) rather than half-imported. Captions are fetched in a second, best-effort yt-dlp pass (`skip_download` + `ignoreerrors`) after the video is staged: a caption that 404s or 429s must not fail, or misclassify, a video that downloaded fine. `staged_path` is committed right after the rename, before the audio-tag remux, so a hard stop in that (long, uninterruptible) window resumes instead of re-downloading. Every worker write that a Cancel could race is one conditional UPDATE (`_set_unless_cancelled`, `_enter_importing`), and the API's cancel is one too (`WHERE status IN cancellable`, 409 on zero rows). The stall guard counts bytes as well as the percentage, so a stream with no known size is not "stuck at 0%".

`DownloadedEpisodesScan` / `DownloadedMoviesScan` (single call, path + importMode) still exist on `/api/v3/command` but depend on the filename parser — keep as a fallback switch only. The un-versioned `/api/command` path is gone in Sonarr v4; everything is `/api/v3/`.

Runner: one download at a time by default (YouTube throttling), configurable. yt-dlp is blocking, so it runs in a thread pool under the asyncio worker. The worker never claims a job a running task still owns (a Cancel followed by Retry mid-run re-queues the row; it is claimed only once the first run has ended), a Cancel outranks any failure that lands after it, the `downloading → importing` step is an atomic conditional update, and a target that already has a file is detected before the download, not after. Only jobs on enabled connections are claimed. One worker per database: the worker takes an exclusive lock file in the config dir at start; a second instance on the same database logs that and stays idle.

## Scheduler (subscriptions)

Every `scan_interval` for each enabled subscription on an enabled connection:

1. The series itself (`GET /api/v3/series/{id}`; a deleted series is a visible, non-retryable scan error, and the title is refreshed), then its episodes from Sonarr (`GET /api/v3/episode?seriesId=`), reduced to monitored, no file, aired (Sonarr's local `airDate` where it gives one, else the UTC date). One call per series beats paging the whole `wanted/missing` list (28 pages on the current library). Episodes that already have a live or done job are skipped before matching.
2. Flat-list every source of the subscription (one or more channels/playlists, up to 10; a series whose episodes are split across channels needs one subscription) and match against the union, a video counted once. One source failing fails the scan, since a partial pool could turn an ambiguous pair into a confident wrong match. Sources are anything yt-dlp can list — YouTube channels and playlists, Vimeo channels and showcases — plus archive.org *collections*, which yt-dlp cannot list: those go through archive.org's search API (title, date = the original air date, `movies` items only, the collection's own name stripped from item titles so they match exactly), and each item is then an ordinary yt-dlp download. Per source: a channel's newest N uploads (N = the subscription's *videos to list* if set, else the `scan_video_limit` setting, default 50, at most 5000 — a channel whose episodes sit behind hundreds of newer uploads needs a deeper listing than the rest, and a full 1200-entry listing costs about 13 s; a bare channel URL is rewritten to its `/videos` tab), or a playlist in full — playlists are in whatever order their owner chose, so truncating them would hide newest-last entries. Entries that are not (yet) a downloadable episode are left out of the listing and counted in the log: scheduled premieres (the title is there, the video is not; a job for one would fail and retry for a day, and under `future` a premiere further out than the retry ladder would fail for good), live streams in progress, and Shorts. Members-only entries stay listed: the cookies file may well be a member's, and cookies are used on demand. Listings also ask YouTube for its *approximate date* (`youtubetab:approximate_date`), the "3 years ago" the channel page shows; it is kept as an age (`approx_age`), displayed with a `~` in the listed-videos panel, and never matched on — the date strategy needs the exact date from a per-video fetch, and a guess to the year would pair the wrong week.
3. Match (below). On a scheduled scan, create a job per match the subscription's **auto-download policy** allows: `future` (the default for a new subscription — only episodes airing from the subscription's `created_at` on; subscribing to a 700-episode show must not dump its backlog on the queue), `all` (every wanted episode; what subscriptions created before the policy existed keep), or `none` (scans only refresh the preview). Matches the policy leaves out are reported as *not automatic*. The Download buttons in the preview are always manual and ignore the policy: *Download all N* or *Download selected* with a checkbox per matched episode, so a backlog can be taken in pieces whatever Sonarr monitors (`POST /api/subscriptions/{id}/download {episode_ids}`). A duplicate (same episode and video, job not done) is reported, not created. The scan summary lands in `subscription.last_scan_result`.

**Re-acquisition.** Whether an episode needs a file is Sonarr's `hasFile`, never our job history: only jobs that are not `done` count as covering an episode. Delete the file in Sonarr and the next scan queues it again (the same video if it still matches). Failed and cancelled jobs keep covering their episode until the user retries or the job is deleted, so a deterministic failure does not pile up a new job every scan. A pinned video stays in the matcher's pool even while a non-done job (say a cancelled wrong pairing) still holds it: the pin is the user's word that the old pairing was wrong, so it must be able to take effect before that job is deleted.

## Matching

Applied in a fixed order — override, regex, title, date — regardless of how the subscription lists them; the first strategy yielding exactly one candidate wins. Zero or several → fall through; if all fall through, the episode is skipped and shown as *unmatched* in the GUI, with what each strategy saw, so the user can add an override. A video is assigned at most once per scan. Each strategy runs to a fixed point: when an exact claim takes a video, an episode whose two containment candidates included it is re-evaluated in the same scan. Dead or private entries (the listing carries only the id as the title) are dropped from the pool before any strategy — digits in an id must never satisfy a regex — and are never fetched for dates.

1. **Override** — `video_id → SxxExx`, set from the GUI by picking a listed video or pasting a URL (resolved once, on save; a pick is stored with its URL and title, so it keeps working after the video leaves the listing window). Always wins. A URL override is added to the candidate pool even when the listing does not contain it.
2. **Title** — normalise both sides (lowercase, strip punctuation, collapse whitespace, strip `Ep. 5` / `#5` / `Episode 5` prefixes); equality, then containment. A show's own count in the titles ("#751 - JOE ROGAN" in Sonarr, "KT #751 - …" on the channel) is a guard, not a strategy: when both titles carry a `#N` at the head of the title the numbers must agree (a `#2024` hashtag in a tail is not a show number), and containment with agreeing numbers is tier *numbered*: it settles a claim like an exact title, but it is still containment ("KT #751 - … (clip)" contains "#751 - …"), so the length check still applies to it — a guest who appears in five episodes no longer makes all five candidates, and a highlights clip is held rather than filed as the episode. An episode whose title is nothing but a prefix (TVDB's "Episode 5" placeholder) normalises to nothing and can only pair by regex, date or a pin. Containment needs a normalised episode title of at least 6 characters and at least two words, so "TBA", "Pilot" or "Finale" never match half a channel. An ellipsis run inside the episode title is TVDB's wildcard for words it does not know ("Caleb Williams .... While Eating Spicy Wings" for the upload "Caleb Williams Goes “Iceman” Mode While Eating Spicy Wings"): such a title is matched fragment by fragment, every fragment required, in order, each on word boundaries (`wildcard_fragments` / `_contains_in_order`), still tier *contains* so the length check applies, and a placeholder fragment (`TBA`, `TBD`) never matches anything. A trailing ellipsis leaves one fragment and is plain containment.
3. **Air date** — upload date within `date_tolerance_days` of `airDateUtc + date_offset_days`. Flat listings usually lack upload dates, so this fetches per-video info only for still-unassigned, undated videos, only if the strategy is enabled and something is still unmatched, and at most 20 per scan — newest first, so a 1500-video channel takes a day to date and a 2016 upload sits hours away; the subscription page's *Fetch upload dates* fetches every undated listed video once as a background task with progress (four in flight, a commit every ten), after which the next scan (or *Refresh preview*) matches by date immediately. Fetched dates are cached per video (`video_meta`; an unknown date is retried after a week) and written in one short transaction after the fetch loop — never while awaiting the network, since SQLite has a single write lock and the worker's progress writes share it.

Optional `title_regex` with named groups `season`/`episode` for channels that number their own uploads (`episode` required, `season` optional). It runs as the *regex* strategy, second in the order.

**Title scope.** A subscription may require a phrase in every candidate's title (`title_require`, *Title must contain* on the form; case- and punctuation-insensitive substring). It is the general form of the show-number guard, for a channel that carries several shows: a guest's name turns up in every show they appear in, and "Max Schaaf" on the Vice channel is one episode of *Epicly Later'd* and one of *Let It Kill You*. Out-of-scope videos are invisible to every automatic strategy, title and date alike; a pin is the user's word and is exempt. The preview chip says how many listed videos are in scope.

**Length check.** After the strategies, every match that is not a pin or an exact title is checked against the episode's runtime (Sonarr's `runtime`, minutes, from TVDB) using the video's duration from the flat listing: when they differ by more than five minutes *and* by more than a factor of two, the match is *held* — reported in the preview with the reason ("video runs 3:52, Sonarr says the episode runs 24 min"), never queued, and the episode stays open for the later strategies. Pins and exact titles are exempt because a wrong runtime on TVDB is far commoner than a same-titled wrong video (calibrated on 282 real matches: all 84 date-tier matches passed, and the only outliers were exact-title Scam School episodes whose flat "10 min" on TVDB was the wrong number). An unknown runtime or duration is no evidence, not a veto. **Split uploads** are held the same way: a match (any strategy but a pin) whose video title names a part — `(Part 1/5)`, `(Part 2)`, `(1/5)`, `Part 1 of 2`, `Pt. 3/17`, `1 of 4`, `Part 2` — while the episode's title does not is held with the reason *video is one part of a split upload; the episode is whole in Sonarr*: importing it would file a fragment as the episode, flip `hasFile`, and the other parts would never be fetched (prior art learned this the hard way). Markers are read on the raw titles, counts are two digits at most and need 1 ≤ N ≤ M, an episode that names a part itself takes a part, and two parts listed at once were already safe (two candidates, nobody claims). The release valve is the pin button on the held row. Every job records how it was matched (`matched_by`: override/regex/exact/contains/date) and the length evidence it had, for the Matches page.

**Cookies on demand.** The signed-in cookies file is used only when YouTube asks for a sign-in (age gate, bot check, private or members-only): every listing, info fetch and download runs without it first and is retried with it on that error. Using the session everywhere got the account put on YouTube's "SABR-only" experiment, after which its web clients offered nothing above 360p (seen live on Kill Tony: 1080p without cookies, 360p with), and it also kept the session busy enough to rotate away. **PO tokens.** YouTube serves the best formats of an age-gated video (a signed-in cookies session) only to clients that present a proof-of-origin token, so those imports topped out at 480p. The image ships yt-dlp's bgutil provider plugin and the provider's script checkout (built in a Node stage; the plugin runs it with Node or Deno, both in the image) and the app points the plugin at it through `extractor_args` whenever the script and a runtime are present, merging the operator's own extractor args per extractor. The token is minted per video on demand (outbound to Google's BotGuard endpoint only; no new port), cached by the plugin under the config dir. `/health` and the footer show *PO tokens: on/off*; off never degrades health because regular videos are unaffected. Proven live 2026-09-02: an age-gated Scam School video went from 360p to its full 720p. Current yt-dlp no longer errors on an age-gated video without cookies: it quietly serves the embedded client's poorer formats and only notes it. So a cookie-less download whose result carries `age_limit >= 18`, with a cookies file and the PO-token plugin both available, is discarded and done again with the session (`YtDlpSource.download`).

**Match preview** (GUI, dry run) shows the wanted episodes and what each strategy would pick, before anything is queued. This is the main defence against the title-drift problem prior art suffers from.

## Staging filename and quality

`{Series Title} - S{ss}E{ee} - {Episode Title} [WEBDL-{res}p].{ext}` for episodes, `{Movie Title} ({Year}) [WEBDL-{res}p].{ext}` for movies. Parseable as a safety net; not load-bearing.

Quality from downloaded height: ≥2160 → WEBDL-2160p, ≥1080 → WEBDL-1080p, ≥720 → WEBDL-720p, else WEBDL-480p. The target's quality profile must allow it or the import is rejected (and the GUI says so).

**Format presets.** The free-text yt-dlp selector stays the source of truth (global default in Settings, optional override per subscription); beside it a picker offers `FORMAT_PRESETS` (settings.py): up to 1080p H.264 + AAC (the default; every client direct-plays it and YouTube has no H.264 above 1080p), up to 1080p any codec, up to 4K any codec, up to 720p and 480p H.264 + AAC, and best available. Choosing one writes its selector into the field; a field that matches no preset shows as *Custom*, so an operator's own selector is never overwritten and the picker never hides what will actually run. Every preset is proved a valid selector by the test suite (yt-dlp parses it), and the default is asserted to be one of them.

Default yt-dlp format: `bestvideo*[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/bestvideo*[height<=1080]+bestaudio/best[height<=1080]`. YouTube serves many uploads at 2160p AV1; the cap matches the profiles in use and H.264/AAC avoids player transcodes. Raise it in Settings → Downloads (or per subscription) for a 4K profile.

## GUI screens (v1)

| Screen | What it does |
|---|---|
| **Settings → Connections** | Add/edit Sonarr and Radarr: URL, API key, remote staging path. *Test* button (hits `/api/v3/system/status`, checks the reported `appName` matches the connection kind, and checks the staging path is visible via `/api/v3/filesystem`). `/filesystem` returns an empty listing for a missing directory, identical to an empty one, so the check lists the parent and looks for the staging directory in it. Adding a connection tests it on the page that comes back (`?test=<id>` arms an `hx-trigger="load"` on that card's result slot); a rejected form keeps what was typed and shows the error inside the card that failed, never echoing the API key. |
| **Settings → Downloads** | Scan interval, concurrency, videos per scan, default yt-dlp format, container, cookies file, extra yt-dlp options (JSON passthrough — one escape hatch instead of a setting per feature; merged LAST so it always wins), subtitle languages, audio language tag, optional Sonarr tag label. |
| **Settings → Notifications** | Apprise URLs (one per line) and the event toggles; *Send test*. |
| **Matches** | Every match the scheduler made, one row per target (a re-download of the same episode supersedes the older job's row); the rows that need a look come first (a contradicting length before a missing one, riskier tiers first), everything vouched for or confirmed follows newest first (`_risk`). A match needs a look while nothing vouches for it: not an exact title or a pin, not a video length that agrees with the runtime, not the operator's *Looks right*; a length that contradicts the runtime stays on the list until confirmed. *Recheck lengths* fetches the missing evidence for older jobs (each video's duration via yt-dlp, four in flight; the runtime via one `episodes()` call per series; one DB write at the end) so most rows clear themselves; *Looks right* per row or for all listed sets `job.reviewed_at`. Jobs from before `matched_by` existed get their tier inferred from the titles, the subscription's strategies and its pins, shown as "likely …". The tier and strategy names are internal; the page says *pinned*, *exact title*, *show number*, *title contains*, *by date*, *regex* (`TIER_LABELS`). *Wrong video?* is honest about the flow: a matched episode has a file, so Sonarr no longer wants it; delete the file in Sonarr, then pin the right video on the subscription page, where the episode reappears under Unmatched. |
| **Series** | Search box over Sonarr's series (live, cached), with a *subscribed* indicator. Subscribe → form: source URLs (one per line), automatic downloads (future only / everything / nothing), format override, strategies, tolerance/offset, regex, videos to list. Detail view: match preview (a dry-run scan loaded by HTMX), unmatched list with a *Pin a video* field that filters the listed videos as you type or takes a pasted URL, an *Episodes in Sonarr* panel per season (file / missing / unaired / unmonitored, with the covering job; a missing episode whose job is history — done, cancelled or terminally failed, i.e. the file was deleted in Sonarr after the import — gets a red ✕ that deletes that job right there), *Refresh preview* (a dry-run scan; queues nothing) and *Download N matched* (queues the matches), settings, recent jobs. When no listed video resembles any wanted episode the preview says so (wrong playlist / web series vs TV seasons); dead or private entries show as *unavailable* and are never matched or offered as pins. The series search shows Sonarr's file counts. Forms are plain HTML posts (python-multipart). The subscribe form shows Source URLs, strategies and auto-download; date tolerance, regex, title scope, listing depth, audio and quality fold under *More options* (open when one is set). Series and subscription pages say when the next scan is due (`next_scan_text`). After a download the subscription page's Episodes and Recent jobs cards refresh themselves (`HX-Trigger: jobs-changed`), and saving the settings panel says *Subscription saved.* Opening a subscription page shows the LAST scan's report from `subscription.last_report` (written by every successful scan, dry run or not), so a page open costs no listing: the card says "Listed 21 min ago" and *Refresh preview* is what lists again. A failed scan leaves the last good report in place; changing a setting that decides what matches (sources, strategies, dates, regex, title scope, listing depth, auto-download) or moving the subscription clears it. |
| **Grab** | Paste a video or playlist URL → flat-resolve → table of videos. For each: pick a target (Sonarr series → season/episode picker, or Radarr movie search). Playlist helper: "start at S01E01 and number sequentially" bulk-fill, editable per row. *Queue* creates jobs. Implemented as one Alpine.js component talking to the JSON API (`/api/resolve`, the library lookups, `POST /api/jobs`); a row is queueable only when its S/E resolves to a real Sonarr episode id (or a movie is picked); rows whose target already has a file are flagged. YouTube season playlists usually list newest first, so "fill sequentially" has a newest-first toggle (fill from the bottom) beside the per-row correction. Sonarr's full series listing is ~22 MB / ~5 s on a 5 600-series library, hence the 60 s cache and a slow first search. |
| **Activity** | One table, views all/active/failed/done with counts, newest 200, refreshed by HTMX every 3 s; error text verbatim in a collapsible that survives the refresh; *Retry* / *Cancel* / *Delete* (finished jobs only, removes the staging folder) post to the web routes, which call the same functions as the JSON API and return the refreshed table with a notice on any refusal. A fresh install's empty state points at Settings. The notice slot is a permanent live region that notices swap into (a faded notice never removes the slot), and the poll waits while keyboard focus is inside the table. |

Trouble that stops downloads (ffmpeg missing, staging not writable) and a rate-limit pause are said in a bar above every page, not only in the footer; footer items carry their explanation as a Pico tooltip that opens on hover, focus or tap.

Subscribed series carry a tag in Sonarr when the `sonarr_tag` setting names one (off by default): applied on subscribe, moved when a subscription changes series, removed on unsubscribe, never fatal.

## JSON API

The pages are thin; every action is a JSON endpoint so the UI can be replaced or scripted later without touching the worker.

```
GET/POST/PUT/DELETE  /api/connections            POST /api/connections/{id}/test
GET                  /api/connections/{id}/series (live from Sonarr)   /movies (live from Radarr)
GET/POST/PUT/DELETE  /api/subscriptions          POST /api/subscriptions/{id}/scan
GET                  /api/subscriptions/{id}/preview
PUT/DELETE           /api/subscriptions/{id}/overrides/{video_id}
POST                 /api/resolve  {url}  → list of videos (flat)
POST                 /api/jobs     [{connection_id, target:{kind, series_id, episode_ids | movie_id}, video:{url, id, title}}]  (all-or-nothing; 409 lists duplicates)
GET                  /api/jobs?status=     GET /api/jobs/{id}
POST                 /api/jobs/{id}/retry   (failed|cancelled → queued)   /cancel  (queued|downloading|failed → cancelled; a running download aborts within ~2 s and the worker removes its staging folder; importing cannot be cancelled)
GET                  /api/connections/{id}/series?q=&limit=   /series/{sid}/episodes   /movies?q=&limit=   (live; series/movies listings cached 60 s per connection)
GET/PUT              /api/settings
```

## Components

| Module | Responsibility |
|---|---|
| `db/` | SQLAlchemy 2.x models + Alembic migrations. SQLite file in `/config`. |
| `arr/base.py` | `ArrClient` protocol: `status()`, `wanted(series_id=None)`, `quality_definitions()`, `path_visible(path)` (M1); `manual_import_candidates(folder)`, `manual_import(files)`, `command(id)` (M2). Errors raise `ArrError` whose message carries the request and the verbatim response body. |
| `arr/sonarr.py`, `arr/radarr.py` | Implementations over a shared `arr/common.py` HTTP base. Differences are confined here (`episodeIds` vs `movieId`, series vs movie lookups). The scheduler reads a series' episodes with one `episodes()` call rather than paging `wanted/missing`. |
| `notify.py` | `Notifier` protocol + `AppriseNotifier`; the only module importing apprise. |
| `source.py` | `VideoSource` protocol: `download(url, dest_dir, fmt, merge_container, progress, should_abort, subtitle_langs, auto_subtitles)`, `list_recent(url, limit)`, `resolve(url)` (video or playlist → videos), `fetch_info(url)` (one video, flat: a collection URL is a clear error), `tag_audio_language(path, language)`. `YtDlpSource` implements it; yt-dlp writes `<video id>.<ext>` and the runner renames to the staging name once the height is known. |
| `matcher.py` | Pure functions over episodes + videos + overrides. No I/O. |
| `naming.py` | Staging filename + quality mapping. |
| `worker/scheduler.py` | Subscription scans → jobs. |
| `worker/runner.py` | Job state machine: download → import → cleanup; retry/backoff. `claim_next_jobs` marks due jobs `downloading` in one transaction; `process_job` is the whole pipeline for one job. Progress writes are throttled to one every 2 s. |
| `api/` | FastAPI routers for the JSON API. |
| `web/` | Page routes + templates. |
| `main.py` | App factory; starts the worker as a background task; SIGTERM-clean. |

Dependency direction: `web → api → db/arr/source/matcher`; `worker → db/arr/source/matcher`. `web` never touches the worker directly — it writes rows, the worker picks them up.

## Frontend choice

| | Jinja2 + HTMX (+ Alpine.js for the picker) | React/Vite SPA |
|---|---|---|
| Build step | None — one Python container | Node build stage in the Dockerfile |
| Fit for these screens | Tables, forms, polling progress: HTMX's sweet spot | Better for the playlist-mapping grid if it grows complex |
| Cost | Server renders HTML; the JSON API still exists for scripting | Two codebases, API contract to keep in sync |

Recommendation: HTMX for v1 with a small CSS framework (Pico or similar, dark theme). The JSON API is the boundary, so swapping the front end later is a contained change. Revisit if the Grab screen's mapping grid becomes painful.

## Auth and exposure

No built-in auth in v1. Deploy behind NPM + Authentik like the other *arr companions on the stack.

Benefit/risk of the GUI vs v0.1's headless daemon: it adds one HTTP port on the Docker bridge. Exposure beyond that is the same as any *arr app — Sonarr/Radarr API keys sit in the SQLite file under `/config`, readable by anyone with that path. Keys can alternatively be supplied as env vars and the DB stores only a reference, if that matters. Nothing else listens; outbound is *arr APIs + YouTube.

## Failure handling

- **yt-dlp errors** (throttle, age gate, geo, extractor breakage): job → `failed` with the message, backoff 1h → 6h → 24h, capped attempts (4), other jobs continue. A retryable failure is `failed` with `next_retry_at` set; the worker re-claims it when due. *arr transport errors and 5xx retry the same way; a job whose target lookup fails before download also retries. A redirect (a wrong URL or a proxy login page) or a non-JSON body is terminal — retrying it for 31 hours would only hide a configuration mistake. yt-dlp stop conditions reached through pass-through options (download archive, max downloads) are errors, not our abort, and those keys are reserved. Extractor breakage is usually a yt-dlp release away: opt-in `pip install -U yt-dlp` on container start.
- **Import rejected** (quality not in profile, path not visible, no candidate listed, command failed): terminal `failed` with `next_retry_at` null; staged file kept; rejection reasons shown on the job verbatim; *Retry* after fixing config.
- **Shutdown mid-download**: the yt-dlp progress hook aborts, the partial folder is removed, and the job returns to `queued` without consuming an attempt.
- **Stalled download**: the same hook trips a stall guard when the download has not advanced for 30 minutes, or has run for 6 hours in total (a hang held the single worker until a restart before; prior art's issue history is dominated by these). It is a retryable failure with the reason ("no download progress for 30 min (stuck at 41%); abandoned, will retry"), and it spends an attempt. yt-dlp's own 20 s socket timeout keeps the hook cycling through a dead connection, so the abort is delivered; progress needs a known total, as the percentage does. The audio-language remux has its own hour-long timeout for the same reason.
- **Permanent download failures**: an answer time will not change — the video is gone (removed, account terminated, 404), walled off (private, members-only, age-gated after the cookies retry, geo-blocked), or the request is wrong (format not available, unsupported URL) — fails at once with `next_retry_at` null and the verbatim text, instead of four attempts over 31 hours; *Retry* by hand is there once something has changed (a cookies file, the format string, the owner's mind). YouTube's bot check and its rate-limit answer are deliberately not on that list: the first is the address being busy and passes by itself, the second is handled below. The Activity page adds a *Likely cause* line under a failed job's verbatim error (`causes.py`, a pure table over the text plus the cookies state from the footer, so a sign-in error says whether to export cookies, re-export them, or pin another upload).
- **Rate-limited by the source**: YouTube's answer ("The current session has been rate-limited by YouTube for up to an hour", or HTTP 429 from any host) is one wall for everything, so it is handled once, process-wide: the job goes back to `queued` without spending an attempt, due at the end of a shared cool-off, and the worker starts no other job, the scheduler runs no scan, and the background date fetches and rechecks skip the rest of their run (nothing is remembered as unknown) until it lifts. The pause is 15 minutes, doubling per pause that proved too short, capped at an hour; answers landing while a pause is in force are the same strike, not more; any successful download resets the ladder. `/health` carries `youtube_cooloff` and the footer shows *rate-limited: paused N min* while it holds; it is not a degradation. Only YouTube's own wall counts (`is_rate_limited` ignores a 429 that names another host, such as archive.org's search or a caption CDN: that request's ordinary retry ladder applies). A download that STARTED before the wall went up does not clear the pause when it finishes (`CoolOff.clear(since=…)`), so the ladder still doubles. One video that keeps answering rate-limited while other downloads go through is that video's own problem: after `RATE_LIMIT_MAX_REQUEUES` (3) re-queues its next such answer is an ordinary retryable failure (`job.rate_limit_hits`). The scheduler's own date fetches honour the wall too (stop, pause, remember nothing); a transient fetch error remembers nothing, only a permanent answer is remembered as "no date" for the week.
- **Unmatched**: visible per subscription in the GUI; rescanned each interval (flat listing is cheap).
- **Idempotency**: job dedupe on `(connection, target, video)`; re-check the wanted list right before import so a file Sonarr got elsewhere is not double-imported.

## Deployment

- `python:3.12-slim` + ffmpeg + deno (yt-dlp's JavaScript runtime for YouTube) + the `yt-dlp-ejs` package (the JS challenge solver, bundled at build time so yt-dlp never fetches remote components at runtime). `PUID`/`PGID`/`UMASK` handled by `entrypoint.sh` (chowns `/config`, drops privileges with `setpriv`) so staged files are importable by the *arr user. `OUTRIGGARR_YTDLP_UPDATE=1` upgrades yt-dlp on start.
- Stop grace: give the container 60 s (`stop_grace_period` / `--stop-timeout`) so an in-flight download can abort and re-queue; Docker's default 10 s kills it and the job is recovered on the next start instead.
- Volumes: `/config` (DB, cookies) and the staging directory. Two layouts work: the whole data share mounted as `/data` (as Sonarr/Radarr do) with `OUTRIGGARR_STAGING_DIR=/data/outriggarr` — chosen on the reference stack for consistency — or only the staging folder mounted as `/staging` (least access; the default). Either way the *arr must see the same host folder; the move into the library is an atomic rename whenever staging and library share a filesystem on the *arr side. `OUTRIGGARR_CONFIG_DIR` / `OUTRIGGARR_DATABASE_URL` likewise.
- One published port for the GUI (`OUTRIGGARR_PORT`, default 8080 inside the container). Same bridge as Sonarr/Radarr; reaches them by container name.
- Migrations run in-process at startup; the `alembic` CLI reads the same env vars. The instance lock is taken BEFORE they run: a second instance on the same database serves the pages only and does not touch the schema (and reports `instance_lock` under /health problems, 503). When the schema is behind head, the SQLite file is copied to `app.db.bak-<revision>` first (the online backup API) so an image rolled back to older code has something to return to. /health also asks for the write lock for two seconds (`BEGIN IMMEDIATE`; a reader never blocks under WAL, so `SELECT 1` cannot see a wedged writer) and runs the PO-token plugin's own check once at startup (`pot_provider_probe`), reporting its text; the pool is sized for eight downloads plus the scheduler plus pages (10 + 20 overflow).
- Run as the same uid/gid as Sonarr/Radarr so the *arr can move staged files: the entrypoint takes `PUID`/`PGID`/`UMASK`, owns `/config`, probes the staging folder and drops privileges. The image ships Deno (yt-dlp's YouTube extractor needs a JavaScript runtime) and Node (for the PO-token script), and declares a `HEALTHCHECK` on `/health`.
- Shipping source from macOS: `COPYFILE_DISABLE=1 tar --no-xattrs ...`, or AppleDouble `._*.py` files land in `migrations/versions/` and Alembic fails at startup with "source code string cannot contain null bytes".

## Stack

Python 3.12, FastAPI, SQLAlchemy 2.x + Alembic, `yt-dlp` (library), `httpx`, Jinja2 + HTMX. `pytest` with fake `ArrClient` and `VideoSource`; matcher and naming tested as pure functions.

## Open questions

1. ~~Name.~~ Outriggarr.
2. ~~Actual host path for staging, and how it is mounted into Sonarr and Radarr.~~ Answered: the data share is mounted as `/data` in Outriggarr exactly as in Sonarr/Radarr, `OUTRIGGARR_STAGING_DIR=/data/outriggarr` is the staging folder, and each connection's `staging_path_remote` names the same path.
3. Are the target series already on TVDB with full episode lists? If not, that is the first blocker, not code.
4. ~~HTMX vs React — accept the recommendation, or is there a preference?~~ HTMX (decided before M3). Pico CSS, htmx and Alpine.js are vendored under `web/static/` (see NOTICE there); no CDN, no build step.
5. ~~Sonarr tag on subscribed series — wanted in v1 or later?~~ Implemented in M5 as the `sonarr_tag` setting, off by default; applied on subscribe, removed on unsubscribe, never fatal.
6. SponsorBlock segment removal as a default, given iSponsorBlockTV already runs on the playback side? Left off. It is not reachable through the yt-dlp passthrough either (`sponsorblock_remove` is a CLI-only option and `postprocessors` is a reserved key); it would need a dedicated setting.

## Build order

Each milestone is independently useful and ends with a confirmation step. The integration risk is in M2, so it comes before any UI.

| # | Milestone | Done when |
|---|---|---|
| M0 | Skeleton | Repo layout per CLAUDE.md; FastAPI boots; DB + first Alembic migration (`connection`, `setting`, `job`); `/health`; Dockerfile builds and runs. |
| M1 | Connections | `ArrClient` protocol; Sonarr and Radarr `status()`, `quality_definitions()`, `wanted()`; connections CRUD + *Test* over the JSON API against the real instances. |
| M2 | Job pipeline, headless | Runner: download → manual import → cleanup, with retry/backoff. Proven by posting one job for a real wanted Sonarr episode via the JSON API and seeing the file land in the library, renamed by Sonarr, with the staging folder empty. Same for one Radarr movie. **Proven 2026-09-01 on the real stack**: Sonarr (Hot Ones S30E09) and Radarr (Big Buck Bunny, 2008) both moved + renamed into their library folders by ManualImport, `hasFile` true, staging folder empty. |
| M3 | Grab + Activity screens | Paste URL/playlist → resolve → target picker (series/season/episode, movie search) → bulk-fill → queue. Activity with progress, retry, cancel. **Proven 2026-09-01 on the deployed container**: two Hot Ones episodes queued from the Grab page (playlist resolve → series search → season 30 fill → per-row correction → Queue) imported and were renamed by Sonarr; Cancel from Activity stopped a 2 h Kill Tony download at 24 % with the staging folder removed; Retry from Activity re-ran it. |
| M4 | Subscriptions | `matcher.py` (pure, tested); scheduler; overrides; Series screen with match preview and unmatched list; `Refresh preview` (then called Scan now). **Proven 2026-09-01 on the deployed container**: Hot Ones subscribed from the Series screen with the Season 30 playlist as source; the preview matched the one remaining wanted episode (S30E05) by title; the scheduler's first tick queued it before *Scan now* was even pressed (which then correctly reported "already has a job"); the job imported and Sonarr renamed it into Season 30. |
| M5 | Operational polish | PUID/PGID; cookies file; yt-dlp extra opts; opt-in yt-dlp self-update; optional Sonarr tag; README; plus the Settings screen, deno in the image, and the newest-first playlist toggle. **Proven 2026-09-01 on the deployed container**: PID 1 runs as uid 99 gid 100 umask 002 via the entrypoint; `/health` reports deno and ffmpeg; a fresh YouTube extract shows no JS-challenge warnings with deno + `yt-dlp-ejs`; a Monstrum subscription on the Storied channel matched three wanted episodes (one by title, two by upload date) that imported as 664 files owned by 99:100 with `audioLanguages=eng` in Sonarr's media info. |

Answer the open questions above before M1 (real staging mount) and before M3 (HTMX vs React). Nothing in "Later" gets built without a design change first.
