"""Stable, device-neutral API used by Roku and webOS television clients."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from fs42.fs42_server.api.schedules import _schedule_payload
from fs42.station_manager import StationManager


router = APIRouter(prefix="/api/tv", tags=["tv-clients"])


def _iso(value: dt.datetime) -> str:
    return value.replace(microsecond=0).isoformat()


def _field(program, name: str, default=None):
    return program.get(name, default) if isinstance(program, dict) else getattr(
        program, name, default
    )


def _compact_tv_program(program, guide_start: dt.datetime):
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
        or metadata.get("outline") or ""
    )
    return {
        "title": _field(program, "title", ""),
        "display_title": _field(program, "display_title", "")
        or _field(program, "title", ""),
        "program_details": _field(program, "program_details", ""),
        "program_description": description,
        "start_time": _iso(starts),
        "end_time": _iso(ends),
        "guide_start_minute": (starts - guide_start).total_seconds() / 60,
        "guide_end_minute": (ends - guide_start).total_seconds() / 60,
        "guide_time": start_label,
        "guide_time_range": f"{start_label}–{end_label}",
        "artwork_url": _field(program, "artwork_url", ""),
        "airing_kind": _field(program, "airing_kind", ""),
    }


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
    rows = []
    for station in StationManager().stations:
        if station.get("hidden") or not (
            station.get("_has_schedule")
            or station.get("network_type") == "live_news"
        ):
            continue
        payload = _schedule_payload(
            station["network_name"], _iso(start), _iso(end), True, True
        )
        programs = payload.get("schedule_blocks", [])
        if station.get("network_type") == "live_news":
            for program in programs:
                program["artwork_url"] = (
                    f"/api/watch/channels/{station['channel_number']}/artwork"
                )
        else:
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
        programs = [_compact_tv_program(program, start) for program in programs]
        rows.append({
            "channel_number": str(station["channel_number"]),
            "channel_name": station.get("network_long_name")
            or station["network_name"],
            "network_name": station["network_name"],
            "is_live_source": station.get("network_type") == "live_news",
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
    return {
        "start": _iso(start),
        "end": _iso(end),
        "server_time": _iso(now),
        "current_offset_minute": (now - start).total_seconds() / 60,
        "channels": rows,
        "artwork_ready": True,
        "timeline": [
            {
                "minute": minute,
                "label": (start + dt.timedelta(minutes=minute)).strftime(
                    "%I:%M%p"
                ).lstrip("0").lower(),
            }
            for minute in range(0, hours * 60 + 1, 30)
        ],
    }
