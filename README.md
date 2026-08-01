# myHomeTV

Turn a personal media library into the cable service you wish still existed.

myHomeTV is a self-hosted television server. It organizes local movies, shows,
commercials, bumpers, and optional free live-news sources into numbered
channels with a continuous schedule. Open the guide, choose a channel, and join
whatever is airing—no episode picker required.

![A cable box next to a television](docs/cable_cover_3.png)

## Why myHomeTV?

Media servers are excellent at helping you choose something. myHomeTV is for
the times when you do not want to choose. It recreates the comfortable parts of
cable TV while keeping the media and server in your home.

- A full-screen, time-proportional cable guide that starts at **now**
- Numbered channels with current and upcoming programming
- Chronological episode scheduling, durable play history, weekly premieres,
  reruns, marathons, and schedule regeneration
- Server-cached, show-specific artwork and metadata in the guide
- Commercial breaks, channel bumpers, seasonal bumpers, and generated promos
- Movie-aware scheduling and ad-free treatment for programs at least one hour
- Optional English metadata and artwork from TMDB/TVmaze
- Optional English subtitle discovery through OpenSubtitles
- Free, official live-news sources when their publishers expose a playable feed
- Independent HLS sessions, so several televisions can watch different channels
- Browser, Roku SceneGraph, and LG webOS clients designed for a TV remote

myHomeTV began as a fork of
[FieldStation42](https://github.com/shane-mason/FieldStation42). The original
local MPV player remains available; this fork adds the headless server, modern
guide, metadata pipeline, scheduler, and living-room clients.

## How it works

```text
Your media → catalog + metadata → channel scheduler → shared server timeline
                                                        ├─ Web browser
                                                        ├─ Roku TV
                                                        └─ LG webOS TV
```

The server scans media into station catalogs, builds future schedules, and
resolves the exact point currently airing on each channel. FFmpeg produces HLS
streams on demand. Clients share the channel timeline but receive independent
viewing sessions, so changing a channel on one TV does not affect another.

Artwork is indexed and cached on the server. The guide uses one series image
for upcoming television episodes and reserves a live episode preview for the
currently selected airing. Clients do not need to fetch or analyze the whole
library themselves.

## Quick start with Docker

### 1. Clone and configure

```bash
git clone https://github.com/CtrlAltForgot/myHomeTV.git
cd FieldStation42-HomeTV
cp docker/hometv.env.example docker/.env
```

Edit `docker/.env` and point these values at persistent directories on the
host:

```dotenv
FS42_MEDIA_PATH=/path/to/media
FS42_CONFS_PATH=/path/to/myhometv/confs
FS42_CATALOG_PATH=/path/to/myhometv/catalog
FS42_RUNTIME_PATH=/path/to/myhometv/runtime
FS42_LOGS_PATH=/path/to/myhometv/logs
TZ=America/Chicago
```

`FS42_MEDIA_PATH` is mounted read-only at `/media` inside the container. Use
container paths such as `/media/TV Shows` when creating station configurations.

TMDB and OpenSubtitles keys are optional. A TMDB key can also be saved from the
**Settings** button on the myHomeTV home page.

### 2. Start the server

```bash
docker compose --env-file docker/.env \
  -f docker/docker-compose.hometv.yml up -d --build
```

Open:

- Admin: `http://SERVER-IP:4242/`
- Watch: `http://SERVER-IP:4242/watch`
- Guide: `http://SERVER-IP:4242/static/guide.html`

Change `FS42_PORT` in `docker/.env` if port 4242 is already in use.

### 3. Create channels and build the guide

Use **Stations** in the admin interface to create a station. Available templates
include Standard, Loop, Web Page, Guide, Streaming, and Blank. Every visible
station is represented in the shared guide; station types without a generated
media schedule receive a channel-specific listing rather than disappearing.

For a normal media channel:

1. Set its content directory and schedule rules.
2. Choose **Rebuild catalog + generate one week** on the home page.
3. Leave the server running—the guide follows the schedule continuously.

Catalog rebuilding is incremental. Unchanged files reuse cached chapter,
metadata, subtitle, and artwork results. **Scan missing metadata** fills only
missing identities without resetting the schedule.

## Unraid

The repository includes an Unraid staging deployment script with validation,
health checks, persistent data mounts, and a recoverable backup before each
deployment. The established layout is:

```text
/mnt/user/appdata/fieldstation42-hometv/
├── app
├── backups
├── catalog
├── confs
└── runtime
```

From the cloned `app` directory, deploy with:

```bash
git pull --ff-only origin hometv-mvp && ./deploy-unraid.sh
```

This deployment publishes myHomeTV on port **4243**. See
[Docker and Unraid setup](docker/README.md) for configuration and migration
details.

## TV clients

### Roku

The Roku app is controlled entirely with the Roku remote. It includes the same
proportional guide model as the web client, overlays the guide above live video,
uses Rewind/Fast-forward for adjacent channels, and returns to the guide with
Back.

1. Enable Developer Mode on the Roku.
2. Open `http://SERVER-IP:PORT/api/tv/apps/roku` on a computer and save the ZIP.
3. Open the Roku developer installer at `http://ROKU-IP`.
4. Upload the ZIP and choose **Install**.

The server address is entered once in the app and stored on the Roku.

### LG webOS

The packaged LG client is available at:

```text
http://SERVER-IP:PORT/api/tv/apps/webos
```

LG sideloading requires Developer Mode and the webOS command-line tools. See
[TV client setup and controls](clients/README.md) for the complete Roku and LG
instructions.

## Scheduling that feels like television

myHomeTV stores a durable per-series broadcast history. Scheduler policies can
release the next chronological episode in a fixed weekly slot, fill other slots
with eligible reruns, restart a completed series naturally, and avoid jumping
randomly between distant seasons. The **Edit History** interface can inspect,
advance, remove, or reset a series cursor before regenerating future programming.

The schedule is data, not a per-device playlist. Everyone sees the same program
airing at the same time, while each client remains free to tune elsewhere.

More detail is available in [the scheduler guide](docs/HOMETV_SCHEDULER.md).

## Commercials, bumpers, and promos

Short-form media can be inserted around eligible programs. Long programs and
movies are protected from normal ad insertion. Ad media is analyzed and
prefetched so consecutive spots can play without a client-side reload between
them.

Channel bumpers live under the persistent catalog directory:

```text
catalog/channel_bumpers/
└── Channel Name/
    ├── general/
    ├── promos/
    ├── christmas/
    ├── new-years/
    └── other seasonal folders/
```

Generated promos can announce a premiere, new episode, new season, or finale in
the style of that channel without interrupting every break.

## Metadata, artwork, and subtitles

- File and directory names are parsed first, including common `S01E02` and
  `01x02` forms.
- TVmaze and TMDB provide English series, episode, and movie information when
  available.
- Canonical artwork is content-addressed and served with long-lived cache
  headers.
- Movies do not display fake season or episode labels.
- Television uses compact labels such as `S1E2: Episode title`.
- Anime titles are normalized to their English series identity when metadata is
  available.
- Embedded subtitles are preferred; OpenSubtitles can fill missing English
  tracks when configured.

API keys remain on the server and are never sent to a TV client.

## Live news

myHomeTV can install a curated set of free official news channels. It prefers a
publisher-provided HLS feed and falls back only where a client supports the
official live player. Availability is controlled by each publisher and can
change without notice; myHomeTV does not bypass subscriptions, DRM, regional
restrictions, or authentication.

See [live-news behavior and troubleshooting](docs/HOMETV_LIVE_NEWS.md).

## Requirements

For the headless server:

- Docker with the Compose plugin
- A Linux host such as Unraid, Debian, Ubuntu, or Raspberry Pi OS
- Local media readable by the container
- Enough CPU for the number of simultaneous FFmpeg transcodes you permit

TMDB and OpenSubtitles accounts are optional. Local metadata and embedded
artwork continue to work without them.

For a traditional directly connected television, the original Python/MPV mode
is still supported. See [Raspberry Pi kiosk setup](docs/RASPBERRY_PI_KIOSK.md).

## Development

Run the automated suite:

```bash
pytest -q
```

Build the Roku client:

```bash
npm exec --yes --package=brighterscript -- \
  bsc --project clients/roku/bsconfig.json
```

Useful technical references:

- [myHomeTV architecture](docs/HOMETV_ARCHITECTURE.md)
- [Station configuration](docs/STATION_CONFIG_README.md)
- [Main server configuration](docs/MAIN_CONFIG_README.md)
- [REST API](docs/STATION_API_README.md)

## Contributing and license

Bug reports and pull requests are welcome. Please describe large proposed
changes in an issue first so scheduler, API, and client behavior stay aligned.

myHomeTV retains FieldStation42's Mozilla Public License 2.0 licensing and
attribution. See [LICENSE](LICENSE).
