"""Curated, zero-subscription live news channels for the browser TV client."""

from __future__ import annotations

import datetime as dt
import html
import re
import json
import logging
import os
import threading
import time
import urllib.parse
from pathlib import Path

import requests


CHANNEL_ID_RE = re.compile(r"^UC[A-Za-z0-9_-]{20,30}$")
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
DISCOVERY_LOCK = threading.Lock()
DISCOVERY_CACHE: dict[str, tuple[float, str]] = {}
LOG = logging.getLogger("LiveNews")

# These are official publisher-operated YouTube channels.  We intentionally
# embed the publisher's live player instead of discovering or restreaming its
# media URL, so availability and advertising remain under publisher control.
SOURCES = (
    {"id": "abc-news-live", "name": "ABC News Live", "channel": 20,
     "youtube_channel_id": "UCBi2mrWuNuyYy4gbM6fU18Q", "color": "#2457d6",
     "official_url": "https://abcnews.go.com/Live"},
    {"id": "cbs-news-247", "name": "CBS News 24/7", "channel": 21,
     "youtube_channel_id": "UC8p1vwvWtl6T73JiExfWs1g", "color": "#1769c2",
     "official_url": "https://www.cbsnews.com/streaming/",
     "hls_discovery_url": "https://www.cbsnews.com/live/"},
    {"id": "nbc-news-now", "name": "NBC News NOW", "channel": 22,
     "youtube_channel_id": "UCeY0bbntWzzVIaj2z3QigXg", "color": "#6046d7",
     "official_url": "https://www.nbcnews.com/now"},
    {"id": "livenow-fox", "name": "LiveNOW from FOX", "channel": 23,
     "youtube_channel_id": "UCJg9wBPyKMNA5sRDnvzmkdg", "color": "#c72835",
     "official_url": "https://www.livenowfox.com/live"},
)


def source(source_id: str) -> dict | None:
    return next((dict(item) for item in SOURCES if item["id"] == source_id), None)


def station_source(station: dict) -> dict | None:
    if station.get("network_type") != "live_news":
        return None
    configured = station.get("live_source") or {}
    item = source(str(configured.get("source_id", "")))
    channel_id = str(configured.get("youtube_channel_id", ""))
    if item and CHANNEL_ID_RE.fullmatch(channel_id):
        item.update(configured)
        return item
    return None


def _live_cache_path() -> Path:
    return Path(os.environ.get("FS42_LIVE_NEWS_CACHE", "runtime/live-news-sources.json"))


def _persist_video(source_id: str, video_id: str) -> None:
    path = _live_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    data[source_id] = {"video_id": video_id, "checked": time.time()}
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        LOG.info("Could not persist live source cache: %s", exc)


def discover_live_video(station: dict, force: bool = False) -> str | None:
    """Resolve the publisher's /live page to its current concrete video ID."""
    item = station_source(station)
    if not item:
        return None
    source_id = item["id"]
    with DISCOVERY_LOCK:
        cached = DISCOVERY_CACHE.get(source_id)
        if cached and not force and time.monotonic() - cached[0] < 120:
            return cached[1]
        try:
            response = requests.get(
                f"https://www.youtube.com/channel/{item['youtube_channel_id']}/live",
                headers={"Accept-Language": "en-US,en;q=0.8", "User-Agent": "Mozilla/5.0 myHomeTV/1.0"},
                timeout=10,
            )
            response.raise_for_status()
            # The page's initial navigation command identifies the actual live
            # broadcast. Other videoId values later in the response are merely
            # recommendations and must never be selected.
            command = re.search(
                r"window\[['\"]ytCommand['\"]\]\s*=\s*(\{.*?\});",
                response.text,
            )
            match = re.search(
                r'"watchEndpoint"\s*:\s*\{\s*"videoId"\s*:\s*"([A-Za-z0-9_-]{11})"',
                command.group(1) if command else "",
            )
            if not match:
                LOG.warning("No current YouTube live broadcast found for %s", source_id)
                return None
            video_id = match.group(1)
            DISCOVERY_CACHE[source_id] = (time.monotonic(), video_id)
            try:
                _persist_video(source_id, video_id)
            except OSError as exc:
                # Persistence is an optimization; a read-only development
                # runtime must never discard a successfully discovered feed.
                LOG.info("Could not persist live source cache: %s", exc)
            return video_id
        except Exception as exc:
            LOG.warning("Live broadcast discovery failed for %s: %s", source_id, exc)
            return None


def discover_official_hls(station: dict) -> str | None:
    """Find a CORS-enabled HLS URL published in an approved official page."""
    item = station_source(station)
    page = str((item or {}).get("hls_discovery_url", ""))
    if not page:
        return None
    try:
        response = requests.get(
            page, headers={"User-Agent": "Mozilla/5.0 myHomeTV/1.0"}, timeout=10
        )
        response.raise_for_status()
        candidates = re.findall(r'https://[^"\s\\]+?\.m3u8(?:\?[^"\s\\]*)?', response.text)
        approved_suffixes = (".cbsivideo.com", ".google.com")
        for candidate in candidates:
            url = html.unescape(candidate).replace("\\/", "/")
            host = (urllib.parse.urlparse(url).hostname or "").casefold()
            if any(host.endswith(suffix) for suffix in approved_suffixes):
                return url
    except Exception as exc:
        LOG.warning("Official HLS discovery failed for %s: %s", item.get("id"), exc)
    return None


def station_config(item: dict, channel: int) -> dict:
    return {"station_conf": {
        "network_name": item["name"],
        "network_long_name": item["name"],
        "channel_number": channel,
        "network_type": "live_news",
        "hidden": False,
        "live_source": {
            "source_id": item["id"],
            "provider": "youtube",
            "youtube_channel_id": item["youtube_channel_id"],
            "official_url": item["official_url"],
            "color": item["color"],
            "managed": True,
        },
    }}


def now_payload(station: dict, now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + dt.timedelta(days=1)
    elapsed = (now - start).total_seconds()
    return {
        "channel_number": str(station["channel_number"]),
        "channel_name": station["network_name"],
        "program_title": station["network_name"],
        "episode_title": "Live news coverage",
        "program_details": "Live news coverage",
        "start": start.isoformat(), "end": end.isoformat(),
        "item_end": end.isoformat(), "item_remaining": (end - now).total_seconds(),
        "duration": 86400, "elapsed": elapsed, "progress": elapsed / 86400,
        "playback_offset": 0, "server_time": now.isoformat(), "is_live": True,
    }


def schedule_blocks(station: dict, start: dt.datetime, end: dt.datetime) -> list[dict]:
    return [{
        "title": station["network_name"], "raw_title": station["network_name"],
        "display_title": station["network_name"],
        "program_details": "Live news coverage", "airing_kind": "live",
        "start_time": start.isoformat(), "end_time": end.isoformat(), "plan": [],
    }]


def artwork_svg(station: dict) -> str:
    item = station_source(station) or {}
    name = html.escape(station.get("network_name", "Live News"))
    color = item.get("color", "#2463a8")
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720">
<defs><linearGradient id="g" x2="1" y2="1"><stop stop-color="{color}"/><stop offset="1" stop-color="#07111d"/></linearGradient></defs>
<rect width="1280" height="720" fill="url(#g)"/><circle cx="1100" cy="100" r="360" fill="#fff" opacity=".06"/>
<text x="88" y="305" fill="#7ee8ff" font-family="sans-serif" font-size="34" font-weight="700" letter-spacing="8">LIVE</text>
<text x="88" y="405" fill="white" font-family="sans-serif" font-size="72" font-weight="700">{name}</text>
<text x="88" y="475" fill="#d8e5ef" font-family="sans-serif" font-size="30">Official live news stream</text></svg>'''
