# myHomeTV Project Plan and Handoff

Last updated: 2026-08-01

## Product vision

myHomeTV turns a personal media library into a polished cable-style television
service that is simple enough for the owner's grandmother to use on a TV. The
primary experience should feel like a modern DirecTV/cable guide: channels are
always airing on a shared timeline, browsing is remote-friendly, selection
reveals rich program information and artwork, and live preview begins quickly
without obscuring the artwork while it loads.

The browser experience is being built first. A dedicated television app is a
future client of the same server APIs and shared channel timeline.

## Collaboration and release workflow

- Work continuously through a requested feature set, validate it, commit it,
  and push it before handing it back for feedback.
- The active release branch is `hometv-mvp` on `origin`.
- The Unraid checkout is
  `/mnt/user/appdata/fieldstation42-hometv/app`. This legacy filesystem path is
  intentionally retained so existing configuration and runtime data do not
  move during the myHomeTV rebrand.
- After a pushed update, provide this one-line Unraid command:

  ```bash
  cd /mnt/user/appdata/fieldstation42-hometv/app && git pull --ff-only origin hometv-mvp && ./deploy-unraid.sh
  ```

- Do not ask the user to run development commands on their personal computer.
  Commands intended for them should run in the Unraid terminal.
- Never commit or document the user's TMDB credential. A key pasted into chat
  should be treated as exposed and rotated.

## Completed foundation

### TV-first guide and smoother playback

Commit `b135370` added the TV-oriented full guide, program artwork/live
preview, remote/keyboard navigation, selection details, current-time marker,
and prewarmed dual-player handoffs intended to reduce buffering between
back-to-back ads and scheduled items.

### Guide preview, metadata, anime, and subtitles

Commit `4b8e1cc` added:

- a four-hour guide window so listings continue beyond 2:00 a.m.;
- no picture-in-picture affordance in guide previews;
- artwork that remains visible while preview video starts;
- a small bottom-right `Preview loading` indicator;
- faster preview initiation and cached media stream probes;
- lower-positioned remote-control hints;
- incremental TMDB enrichment for movie, series, and episode descriptions,
  English titles, episode facts, and cached artwork;
- metadata enrichment during catalog rebuilds and a manual **Scan missing
  metadata** action;
- English-localized anime series names;
- automatic English subtitles for non-English audio, including common anime
  tracks whose language exists only in the stream title;
- player subtitle modes: **CC Auto**, **CC English**, and **CC Off**, with `C`
  as the keyboard shortcut.

Movies must never display season or episode information. Episode summaries use
the compact form `S1E1: Episode title` rather than verbose season/episode text.

Guide artwork must not depend exclusively on TMDB. Every scheduled video needs
a persistent cached still extracted from the media as a fallback, created
incrementally during scans/rebuilds and lazily on first guide access. Episodic
airings must never display channel-number cards or generated title cards.

### myHomeTV rebrand

Commit `bcc8ae6` changed the user-facing product name from FieldStation42 to
**myHomeTV** across the management UI, guide, watch page, remote, diagnostics,
bump screens, API title, documentation, Docker image/project, and Raspberry Pi
kiosk label. Internal `fs42` module names and the existing Unraid data path are
retained for compatibility and to avoid a risky data migration.

The deployment script migrates the legacy Docker container name
`fieldstation42-hometv` to `myhometv`. It keeps the previous container under a
temporary rollback name until the new container passes its health check.

The rebrand release passed 69 automated tests. The preceding guide/metadata
release passed 67 tests.

## Completed feature: friendly application settings

The management page now removes the need to create or edit `docker/.env` to
configure TMDB or OpenSubtitles.

### User experience

- Add a clearly labeled **Settings** button to the main navigation or main
  management page.
- Open a focused settings panel/dialog that works with mouse, keyboard, and a
  television remote.
- Include a **TMDB metadata** section with:
  - an API-key password field that does not reveal an existing key;
  - status such as **Configured**, **Not configured**, or a useful validation
    error;
  - **Save and test** and **Remove key** actions;
  - a short link/instruction for obtaining a TMDB v3 API key;
  - a **Scan missing metadata now** action after successful validation.
- Explain that TMDB supplies English movie/show/episode information and guide
  artwork, while local NFO metadata remains preferred for episode-specific
  facts.
- Make failure messages plain and actionable. The user should not need to know
  what an environment variable is.

### Server and security behavior

- Store UI-managed secrets in a persistent, server-owned settings file under
  the existing mounted configuration/runtime area, not in source control and
  not in browser local storage.
- Restrict settings endpoints to the trusted-LAN application model already
  documented for myHomeTV. Never return the raw key to the browser after save;
  return only configured state and a masked suffix if useful.
- Accept `TMDB_API_KEY` from the environment as a deployment override, but let
  the persisted UI setting be used when the environment value is absent or is
  still the literal placeholder `PASTE_YOUR_KEY_HERE`.
- Validate the key against TMDB before replacing a known-good stored key. Use
  atomic file replacement and permissions appropriate for the unprivileged
  container user.
- Refresh the process-level TMDB helper after a successful save so metadata
  scanning works immediately without redeployment.
- Add API, persistence, redaction, validation-failure, and UI-presence tests.

## Implemented goal pending release: broadcast-realistic scheduler

The scheduler should behave like a programmed television network rather than a
random playlist. A viewer must be able to return at the same local time every
week for the next episode of a show, while other slots use believable reruns
drawn only from episodes that have already premiered on that channel.

### Scheduling model

- Keep the existing weekday/hour grid as the backward-compatible foundation.
- Add opt-in `programming` settings to a series slot. A slot may be a
  `premiere`, `rerun`, or `mixed` slot and has a stable `slot_id` so its state
  survives harmless configuration edits.
- A weekly premiere slot advances exactly once per configured broadcast week,
  in canonical `(season, episode, part, path)` order. Multiple future weeks
  generated at once reserve different consecutive episodes.
- A rerun slot may select only an episode whose premiere time is in the past.
  It should prefer the least recently aired eligible episode while respecting
  minimum repeat spacing.
- A mixed slot premieres when its cadence is due and otherwise behaves as a
  rerun slot.
- Default cadence is one new-to-channel episode per week. Supported cadence
  should include a configurable number of premieres per week and optional
  seasonal hiatus/date windows.
- The guide marks scheduled programs as `NEW`, `RERUN`, or `SPECIAL` without
  putting these words into the canonical media title.

### Durable state and correctness

- Introduce immutable schedule-airing identity and a durable broadcast-history
  table keyed by station, slot, series, media identity, and scheduled time.
- Separate `scheduled/reserved` from `aired`. Generating a month of future
  schedules must not increment play counts or claim those episodes aired.
- Reconcile history only when a scheduled program reaches its end time. This
  works even with no active viewer because the network timeline itself aired.
- Preserve reservations and episode order across restart, schedule extension,
  catalog refresh, and incremental discovery of newly added episodes.
- A destructive schedule reset releases only future reservations. Past aired
  history remains unless the user explicitly chooses **Reset viewing history**.
- Catalog rebuilds remap history through stable media identity (series, season,
  episode and normalized path fingerprint), not volatile catalog row IDs.
- Handle specials (`S0`), multipart episodes, missing episode numbers,
  duplicate encodes, and gaps without silently moving the premiere cursor
  backward.
- Use the configured local timezone and test DST spring-forward and fall-back
  weeks. Weekly slots remain anchored to wall-clock time.

### Policies and fallbacks

- `premiere_cadence`: default `weekly`.
- `minimum_rerun_gap_days`: prevents the same episode repeating too soon.
- `rerun_pool`: `aired_only` by default, with optional recent-season limits.
- `catch_up_policy`: choose whether a missed/removed premiere waits for the
  next weekly slot or advances while preserving an audit record.
- `library_end_policy`: `reruns`, `restart_after_hiatus`, or `hold` rather than
  silently wrapping to episode one as a new premiere.
- If no rerun is eligible, use a configured slot fallback tag, then the station
  fallback tag, and finally an explicit off-air/error block. Never premiere an
  unaired episode in a rerun slot merely to fill time.

### Configuration experience

- Add a TV-friendly scheduler editor in station management with plain labels:
  **New episode**, **Rerun**, **New when due, otherwise rerun**.
- Let the user choose series/tag, weekday, local start time, cadence, rerun
  spacing, hiatus dates, fallback, and end-of-library behavior.
- Preview the next several weeks before saving, including episode, NEW/RERUN
  status, conflicts, gaps, and fallbacks.
- Provide history controls per series: view last aired/next premiere, correct
  the next episode, mark an episode aired/unaired, and reset history only behind
  an explicit confirmation.
- Existing station JSON without `programming` fields continues using current
  selection/sequence behavior unchanged.

### Schedule-aware channel promos

- Generate occasional short promo bumpers from the actual premiere calendar,
  cached show artwork, and each channel's visual identity.
- Supported copy includes **New episodes Thursdays at 6 Central**, **New season
  Wednesday**, **Season premiere tonight**, and **Season finale**, chosen only
  when supported by scheduled episode/season state.
- Promos are channel-specific, expire automatically after their advertised
  event, and regenerate when the schedule changes.
- Use restrained broadcast-style motion (artwork pan/zoom, clean typography,
  brief channel sting), broadcast-safe text margins, and readable TV sizing.
- Place promos through the bumper/reel system with per-show and per-channel
  frequency caps, a minimum spacing window, and a hard prohibition on
  back-to-back promos. They should add texture, never become invasive.
- Provide an enable switch, intensity (`rare`, `normal`, `frequent`), maximum
  duration, and preview/regenerate controls in scheduler settings.

### Guide, playback, and media support included in this goal

- Anchor the normal guide viewport at the current minute on its far-left edge;
  past programs appear only after an intentional backward browse. Keep the
  current-time indicator at that boundary and preserve future guide coverage.
- Precompute and cache one spoiler-safe series image for every episodic series.
  Current programs may transition to a live preview, but future episodes always
  use the generic series image and never reveal an episode-specific frame.
  Prewarm visible artwork; no channel-number or generated title-card fallback
  is permitted for episodic airings.
- Prevent preview headings and metadata from clipping at the bottom at TV zoom
  levels, keep controls near the lower edge, and disable picture-in-picture in
  the guide preview.
- Reduce tune time by keeping metadata/subtitle probing out of the first-frame
  path and measuring manifest and first-segment latency.
- Change subtitles without retuning the channel. Prefer embedded and sidecar
  English subtitles, then optionally fetch a high-confidence match, cache it
  with provenance, and never silently accept a low-confidence result.
- Scaffold `catalog/channel_bumpers/<Channel>/` with `general`, `promos`,
  `spring`, `summer`, `halloween`, `thanksgiving`, `christmas`, and
  `new-years` folders. Only date-eligible seasonal folders join that channel's
  bumper pool; existing shared bumper directories remain compatible.

### Acceptance scenarios

1. A show assigned Tuesday at 8:00 p.m. premieres S1E1 this week and S1E2 the
   following Tuesday even if four weeks are generated in one operation.
2. A Thursday rerun slot before the first Tuesday premiere uses its fallback;
   after Tuesday it may air S1E1 but never S1E2 before S1E2's premiere.
3. Restarting, extending, or rebuilding the catalog does not skip or duplicate
   a weekly premiere.
4. Adding S1E9 after S1E1-S1E8 were already known appends it without changing
   past history or existing future reservations.
5. Rebuilding a future schedule releases replaced reservations but retains all
   history whose scheduled end is already in the past.
6. The same rerun is not selected inside its configured repeat-gap window.
7. Weekly wall-clock slots remain at the intended local time across both DST
   transitions.
8. Old station configurations and schedules continue to work without opting
   into the new scheduler.

### Release verification

The implementation is covered by scheduler regression tests for consecutive
multiweek reservation, aired-only reruns, repeat spacing, end-of-library
policies, logical identity after rename, future-reset preservation, missing
episode gaps, newly discovered episodes, specials, multipart episodes,
duplicate encodes, cadence anchors, and both Central-time DST transitions.
The broader HomeTV suite also covers guide labels/artwork, subtitle behavior,
provider-setting redaction and validation, bumper season windows, promo
placement, legacy schemas, and Unraid deployment contracts.

Operator behavior and configuration are documented in
`docs/HOMETV_SCHEDULER.md`.

## Immediate verification after the next deployment

The 2026-08-01 screenshot still displayed `FIELDSTATION42` in the header and
footer even after the rebrand code was pushed. Determine whether this is only
browser caching or whether the deployed container still serves the old image.

Verify in this order:

1. Confirm the running container is named `myhometv` and its image was rebuilt
   from commit `bcc8ae6` or later.
2. Hard-refresh the management page or open it in a private browser window.
3. Confirm the header, footer, guide, watch page, remote, About page, document
   titles, and bump defaults all display `myHomeTV`.
4. If stale assets persist, add cache-busting/versioned static asset URLs or
   appropriate cache headers rather than asking users to clear caches after
   every release.
5. Replace any literal placeholder TMDB key currently present in `docker/.env`;
   do not record the real key in this repository.

## Quality bar for future work

- Design for large viewing distances, simple remote navigation, strong focus
  indication, and minimal steps.
- Preserve the shared live-channel timeline across all clients.
- Keep artwork available during network or transcoder delays.
- Metadata scans must be incremental and must not reset schedules.
- New anime should use an English series display name when TMDB provides one
  and automatically select full-dialogue English subtitles for foreign audio.
- Preserve user data and provide rollback behavior for deployment migrations.
- Run the full HomeTV and Unraid deployment test suites before every release.

## Active always-ready artwork goal (2026-08-01)

- At server startup, enumerate every catalog once and ensure one persistent,
  canonical image exists for each episodic series. Repeat the same incremental
  backfill automatically after catalog rebuilds.
- Before the guide reveals its listings, group the full visible window by
  canonical program identity, fetch each unique image with bounded concurrency,
  and wait for the browser to decode it. Selection then swaps to an already
  decoded in-memory image with no normal-state loading card or spinner.
- Retain HTTP caching across guide visits and an in-memory decoded cache for the
  current guide session. Incremental future guide coverage must be warmed before
  it becomes browsable.
- Expose server preload progress/readiness for diagnostics. A clearly labeled
  unavailable state is allowed only when the program's source media and every
  approved metadata/local-art source are genuinely unusable.

## Active live-news release (2026-08-01)

- Add an idempotent one-click main-page installer for official, freely
  available ABC News Live, CBS News 24/7, NBC News NOW, and LiveNOW from FOX
  streams; require neither a subscription nor a paid API key.
- Assign collision-safe channel numbers and validate curated publisher
  identities. Resolve each publisher's current concrete YouTube broadcast ID
  rather than embedding the unreliable channel-level player. When no current
  YouTube broadcast exists, discover only an allowlisted HLS URL exposed by
  the publisher's own official page; never proxy or restream it.
- Treat live news as first-class programming in station summaries, the guide,
  artwork preview, channel navigation, and browser playback, with immediate
  branded art and clear standby/error behavior when publishers are offline.
- Preserve local scheduled/HLS behavior. Prebuffer a real fragment before
  every boundary—especially back-to-back ads—and keep the outgoing picture
  visible until the replacement is playable.
- Ensure every episodic guide entry resolves one stable series-specific cached
  image through a persistent canonical series index. Prefer local library art,
  then TMDB/TVmaze art, then a representative frame from that same series.
  While an image is being produced, show only the small loading indicator—never
  a channel-number card, generated title card, or unrelated episode image.
- Suppress harmless FFmpeg decoder chatter such as `Late SEI is not
  implemented` during scanning/playback while retaining process failure and
  recovery reporting.
- Cover catalog/source validation, collision handling, repeat installation,
  synthetic guide/now data, embed sessions, schema compatibility, static UI,
  ad prebuffer behavior, and the full regression/container deployment suite.
- Keep the guide open continuously: remove the red current-time marker and
  micro-scroll the grid with wall time instead of rebuilding/closing it every
  minute. Refresh data offscreen/in place only when future coverage runs low.
- Treat every movie and every item lasting at least 60 minutes as completely
  commercial-free. Keep 50-minute episodes eligible for restrained breaks,
  but cap the total filler added to any shorter program so schedule rounding
  cannot create an excessive ad pod.
- Treat official YouTube news embeds as non-interactive video surfaces beneath
  myHomeTV: disable native controls and keyboard input, block pointer capture,
  automatically resume an unexpected pause, and request the highest available
  quality. myHomeTV remains the only interactive overlay while preserving any
  publisher branding required by the official player.
