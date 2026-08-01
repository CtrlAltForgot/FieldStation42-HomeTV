# myHomeTV realistic scheduler

The realistic scheduler is opt-in per weekly station slot. Existing station
files without a `programming` object retain their original random/sequence
behavior.

## Configure it

Open **Stations**, choose **Scheduler**, select a weekly tagged slot, and choose:

- **New episode slot**: one new-to-this-channel episode per ISO broadcast week.
- **Reruns only**: episodes are eligible only after their premiere airtime has
  passed.
- **New when due, otherwise rerun**: combines those rules.

Episodes are ordered by season, episode, multipart suffix, then normalized
path. Movies are excluded. `slot_id` is durable and should not be changed after
the slot begins airing. The editor also controls the minimum rerun gap,
fallback tag, and end-of-library behavior.

Each configured weekly slot may premiere at most one episode in its ISO
broadcast week. To schedule two premieres per week, configure two weekly slots
with different stable `slot_id` values. `premiere_interval_weeks` supports
every-week, alternate-week, or longer release patterns while
`cadence_anchor_date` keeps the pattern deterministic across rebuilds.

Equivalent station JSON:

```json
{
  "tags": "shows/Example",
  "programming": {
    "mode": "mixed",
    "slot_id": "thursday-18-example",
    "series": "Example",
    "premiere_cadence": "weekly",
    "premiere_interval_weeks": 1,
    "cadence_anchor_date": "2026-08-06",
    "catch_up_policy": "wait_for_missing",
    "minimum_rerun_gap_days": 14,
    "library_end_policy": "restart_after_hiatus",
    "restart_hiatus_weeks": 13,
    "fallback_tag": "shows/reruns"
  }
}
```

At the finale, `reruns` immediately uses the eligible least-recently-aired
pool, `restart_after_hiatus` waits and then restarts as **RERUN** (never fake
**NEW**), and `hold` uses fallback programming. Newly discovered later episodes
resume the weekly premiere sequence.

### Policy reference

| Setting | Meaning |
| --- | --- |
| `mode` | `premiere`, `rerun`, or `mixed` |
| `slot_id` | Stable identity for this weekly appointment; do not casually rename it |
| `premiere_interval_weeks` | Number of ISO weeks between premieres |
| `cadence_anchor_date` | Date used to decide which weeks are on cadence |
| `premiere_start_date` / `premiere_end_date` | Optional inclusive season window |
| `hiatus_ranges` | Optional inclusive `{start, end}` date ranges with no premiere |
| `catch_up_policy` | `advance_available`, or `wait_for_missing` to hold at a numbering gap |
| `minimum_rerun_gap_days` | Minimum time since an episode last aired |
| `rerun_recent_seasons` | Optional number of newest seasons allowed in the rerun pool |
| `library_end_policy` | `reruns`, `restart_after_hiatus`, or `hold` |
| `fallback_tag` | Slot-level content used when no scheduled episode is eligible |

The fallback order is slot `fallback_tag`, station `fallback_tag`, then the
station's configured off-air/error behavior. A rerun slot never promotes an
unaired episode just to fill time.

## History and rebuild behavior

`broadcast_history` in the runtime SQLite database separately records
`reserved` and `aired` programs. Future generation never marks a program
played. A reservation becomes aired after its scheduled end, whether or not a
viewer was connected. Resetting a schedule releases future reservations while
preserving past aired history. Existing past schedules are imported as legacy
airings, and logical season/episode identity survives file renames.

Canonical identity is the English series name plus season, episode, and
multipart suffix. This keeps a renamed or moved file from becoming NEW again.
Season zero specials sort before numbered seasons, multipart episodes sort by
their suffix, duplicate encodes share one logical premiere identity, and files
without trustworthy season/episode numbers are not guessed into the premiere
sequence. With `wait_for_missing`, a numbering gap waits for the missing file;
with `advance_available`, scheduling continues with the next known episode and
the durable history remains the audit trail.

Schedule extension keeps existing reservations. A rebuild first reconciles
past reservations to aired, releases only future reservations being replaced,
and then deterministically reserves the replacement future. Catalog play
counts are not used as premiere history. Adding a later episode therefore
extends the series without rewriting previous airings or already-reserved
weeks.

The guide displays **NEW** and **RERUN** badges. Times remain local wall-clock
times through DST because schedules use the container timezone (`TZ`, normally
`America/Chicago`).

## Operator workflow

1. Open **Stations → Scheduler** for the channel.
2. Choose a tagged weekday/hour slot and select its broadcast behavior.
3. Set cadence, missing-episode, rerun-gap, season-window, fallback, and finale
   policies, then save.
4. Review **Next six broadcasts**. A preview is read-only: it never reserves an
   episode or changes play history.
5. Rebuild or extend the future schedule from the main page. Past history is
   retained, while replaced future reservations are safely released.

The channel-history panel shows aired and scheduled totals, the last actual
airtime, and the next reservation. History begins only after a realistic slot
has been generated or an older past schedule has been migrated.

Select **Edit history** beside a series to inspect its chronological airing
records. The editor can remove one incorrect record, remove future
reservations, set an explicit next season/episode, or remove the series history
and restart at S1E1. Destructive actions require confirmation. After changing
history, use **Reset schedule + one week** on the main page so existing future
schedule blocks cannot restore stale reservations.

Premieres are always selected in numeric `(season, episode, multipart)` order;
filesystem and alphabetical filename order are never used. **Set next episode**
is the only intentional way to skip earlier available episodes.

## Channel promos and bumper folders

Catalog rebuilds create:

```text
catalog/channel_bumpers/<Channel>/
  general/ promos/ spring/ summer/ halloween/
  thanksgiving/ christmas/ new-years/
```

Only the current seasonal folders join a channel's bumper pool. Existing
shared `bump_dir` folders remain supported. For realistic premiere schedules,
myHomeTV renders restrained six-second MP4 promos from cached series artwork,
places them in the channel's `promos` folder, and caps placement by lead time
and minimum spacing. Set `schedule_promos` to `false`, or configure:

```json
{
  "schedule_promos": {
    "enabled": true,
    "intensity": "normal",
    "duration": 6,
    "minimum_spacing_hours": 24,
    "lead_hours": 72
  }
}
```

## Metadata and missing subtitles

Use **Settings** on the main page to store the TMDB v3 key and, optionally, an
OpenSubtitles API key. Keys are saved locally to `confs/main_config.json` with
private permissions; API responses expose only the last four characters. A
new TMDB key is tested against TMDB before it replaces a working saved key, so
a typo or temporary validation failure leaves the previous configuration
intact.

Playback prefers embedded English subtitles, then English sidecars. If neither
exists and OpenSubtitles is configured, myHomeTV accepts only an exact
OpenSubtitles file-hash match, caches it under `catalog/.subtitle_cache`, and
records provider provenance. Changing subtitle mode replaces the browser text
track and does not retune or restart the channel.
