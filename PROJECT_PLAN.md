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
incrementally during scans/rebuilds and lazily on first guide access. The
channel-number card is reserved for genuinely unreadable or missing media.

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

## Next feature: friendly application settings

The next implementation should remove the need to create or edit
`docker/.env` to configure TMDB.

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
