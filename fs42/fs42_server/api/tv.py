"""Stable, device-neutral API used by Roku and webOS television clients."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from fs42.fs42_server.api.schedules import _schedule_payload
from fs42.station_manager import StationManager


router = APIRouter(prefix="/api/tv", tags=["tv-clients"])

ROKU_GUIDE_WINDOW_SECONDS = 10_800
ROKU_GUIDE_TRACK_WIDTH = 1_490

STATION_TYPE_LABELS = {
    "standard": "Scheduled programming",
    "loop": "Continuous programming",
    "web": "Web channel",
    "guide": "Program guide",
    "streaming": "Streaming channel",
    "live_news": "Live news coverage",
}


def _iso(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _field(program, name: str, default=None):
    return program.get(name, default) if isinstance(program, dict) else getattr(
        program, name, default
    )


def _compact_tv_program(
    program, guide_start: dt.datetime, current_offset_second: int = 0,
):
    """Attach all couch-guide data while dropping the bulky metadata object."""
    starts = _field(program, "start_time")
    ends = _field(program, "end_time")
    if isinstance(starts, str):
        starts = dt.datetime.fromisoformat(starts)
    if isinstance(ends, str):
        ends = dt.datetime.fromisoformat(ends)
    start_label = starts.strftime("%I:%M%p").lstrip("0").lower()
    end_label = ends.strftime("%I:%M%p").lstrip("0").lower()
    metadata = _field(program, "meta", {}) or {}
    description = (
        metadata.get("plot") or metadata.get("description")
        or metadata.get("outline")
        or _field(program, "program_details", "")
        or ""
    )
    start_second = int((starts - guide_start).total_seconds())
    end_second = int((ends - guide_start).total_seconds())
    return {
        "title": _field(program, "title", ""),
        "display_title": _field(program, "display_title", "")
        or _field(program, "title", ""),
        "program_details": _field(program, "program_details", ""),
        "program_description": description,
        "year": metadata.get("year") or metadata.get("release_year") or "",
        "rating": metadata.get("rating") or metadata.get("content_rating") or "",
        "genre": metadata.get("genre") or metadata.get("genres") or "",
        "start_time": _iso(starts),
        "end_time": _iso(ends),
        "guide_start_minute": (starts - guide_start).total_seconds() / 60,
        "guide_end_minute": (ends - guide_start).total_seconds() / 60,
        "guide_start_second": start_second,
        "guide_end_second": end_second,
        # Roku receives final, integer-only positions relative to NOW. Roku's
        # runtime has produced inconsistent float layout on real televisions.
        "roku_start_pixel": int(
            (start_second - current_offset_second)
            * ROKU_GUIDE_TRACK_WIDTH / ROKU_GUIDE_WINDOW_SECONDS
        ),
        "roku_end_pixel": int(
            (end_second - current_offset_second)
            * ROKU_GUIDE_TRACK_WIDTH / ROKU_GUIDE_WINDOW_SECONDS
        ),
        "guide_time": start_label,
        "guide_time_range": f"{start_label}–{end_label}",
        "artwork_url": _field(program, "artwork_url", ""),
        "airing_kind": _field(program, "airing_kind", ""),
    }


def guide_card_rect(
    starts: int, ends: int, window_start: int,
    window_seconds: int = 10_800, track_width: int = 1_490,
) -> tuple[int, int] | None:
    """Reference geometry shared by tests and the integer-only Roku model."""
    visible_start = max(starts, window_start)
    visible_end = min(ends, window_start + window_seconds)
    if visible_end <= visible_start:
        return None
    x = int((visible_start - window_start) * track_width / window_seconds)
    width = max(
        72,
        int((visible_end - visible_start) * track_width / window_seconds) - 6,
    )
    return x, min(width, track_width - x)


def roku_card_rect(
    start_pixel: int, end_pixel: int, viewport_pixel: int = 0,
    track_width: int = ROKU_GUIDE_TRACK_WIDTH,
) -> tuple[int, int] | None:
    """Exact clipping performed by Roku after server-side pixel conversion."""
    visible_start = max(start_pixel - viewport_pixel, 0)
    visible_end = min(end_pixel - viewport_pixel, track_width)
    if visible_end <= visible_start:
        return None
    return visible_start, max(72, visible_end - visible_start - 6)


def _station_placeholder(station: dict, start: dt.datetime, end: dt.datetime) -> dict:
    """Give schedule-less stations a real, channel-specific guide card."""
    station_type = station.get("network_type", "standard")
    title = station.get("network_long_name") or station["network_name"]
    detail = STATION_TYPE_LABELS.get(station_type, "Channel programming")
    return {
        "title": title,
        "display_title": title,
        "program_details": detail,
        "meta": {"description": detail},
        "start_time": start,
        "end_time": end,
        "artwork_url": (
            f"/api/watch/channels/{station['channel_number']}/artwork"
        ),
        "airing_kind": station_type,
    }


def tunable_neighbor(rows: list[dict], current: int, delta: int) -> int:
    """Return the next playable row, wrapping exactly once in either direction."""
    if not rows or current < 0 or current >= len(rows) or delta == 0:
        return current
    step = 1 if delta > 0 else -1
    candidate = current
    for _ in range(len(rows)):
        candidate = (candidate + step) % len(rows)
        if rows[candidate].get("is_tunable"):
            return candidate
    return current


@router.get("/config")
async def config():
    return {
        "name": "myHomeTV",
        "api_version": 1,
        "server_time": _iso(dt.datetime.now()),
        "capabilities": {
            "guide": True,
            "hls": True,
            "live_news": True,
            "subtitles": True,
            "independent_sessions": True,
        },
    }


@router.get("/apps/{platform}")
async def television_app(platform: str):
    releases = Path("clients/releases")
    choices = {
        "roku": (releases / "myhometv-roku.zip", "application/zip"),
        "webos": (
            releases / "myhometv-webos.ipk",
            "application/vnd.webos.ipk",
        ),
    }
    selected = choices.get(platform.casefold())
    if not selected or not selected[0].is_file():
        raise HTTPException(404, "Television client package not found")
    return FileResponse(
        selected[0],
        media_type=selected[1],
        filename=selected[0].name,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/guide")
async def guide(hours: int = Query(6, ge=1, le=24)):
    """Return the complete couch-client guide in one request.

    Station names are data, never URL path components. This is important for
    names such as ``CBS News 24/7`` and for non-English channel names.
    """
    now = dt.datetime.now()
    start = now.replace(second=0, microsecond=0)
    end = start + dt.timedelta(hours=hours)
    current_offset_second = int((now - start).total_seconds())
    rows = []
    for station in StationManager().stations:
        if station.get("hidden"):
            continue
        has_schedule = bool(station.get("_has_schedule"))
        station_type = station.get("network_type", "standard")
        if has_schedule or station_type == "live_news":
            payload = _schedule_payload(
                station["network_name"], _iso(start), _iso(end), True, True
            )
            programs = payload.get("schedule_blocks", [])
        else:
            payload = {"error": None}
            programs = []
        has_programs = bool(programs)
        if station_type == "live_news":
            for program in programs:
                program["artwork_url"] = (
                    f"/api/watch/channels/{station['channel_number']}/artwork"
                )
        elif programs:
            missing = [
                getattr(program, "display_title", None)
                or getattr(program, "title", "Unknown program")
                for program in programs
                if not getattr(program, "artwork_url", None)
            ]
            if missing:
                raise HTTPException(
                    503,
                    "Canonical artwork index is incomplete for: "
                    + ", ".join(dict.fromkeys(missing[:5])),
                )
        if not programs:
            programs = [_station_placeholder(station, start, end)]
        programs = [
            _compact_tv_program(program, start, current_offset_second)
            for program in programs
        ]
        rows.append({
            "channel_number": str(station["channel_number"]),
            "channel_name": station.get("network_long_name")
            or station["network_name"],
            "network_name": station["network_name"],
            "network_type": station_type,
            "is_live_source": station_type == "live_news",
            "is_tunable": bool(
                station_type == "live_news"
                or (has_schedule and has_programs and not payload.get("error"))
            ),
            "artwork_url": (
                f"/api/watch/channels/{station['channel_number']}/artwork"
            ),
            "programs": programs,
            "error": payload.get("error"),
        })
    rows.sort(
        key=lambda row: (
            0, int(row["channel_number"])
        ) if row["channel_number"].isdigit() else (1, row["channel_number"])
    )
    for index, row in enumerate(rows):
        row["previous_tunable_index"] = tunable_neighbor(rows, index, -1)
        row["next_tunable_index"] = tunable_neighbor(rows, index, 1)
    return {
        "start": _iso(start),
        "end": _iso(end),
        "server_time": _iso(now),
        "current_offset_minute": (now - start).total_seconds() / 60,
        "current_offset_second": current_offset_second,
        "roku_track_width": ROKU_GUIDE_TRACK_WIDTH,
        "channels": rows,
        "artwork_ready": True,
        "timeline": [
            {
                "minute": minute,
                "roku_pixel": int(
                    (minute * 60 - current_offset_second)
                    * ROKU_GUIDE_TRACK_WIDTH / ROKU_GUIDE_WINDOW_SECONDS
                ),
                "label": (start + dt.timedelta(minutes=minute)).strftime(
                    "%I:%M%p"
                ).lstrip("0").lower(),
            }
            for minute in range(0, hours * 60 + 1, 30)
        ],
    }
