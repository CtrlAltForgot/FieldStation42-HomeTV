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
    start = dt.datetime.now().replace(second=0, microsecond=0)
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
        rows.append({
            "channel_number": str(station["channel_number"]),
            "channel_name": station.get("network_long_name")
            or station["network_name"],
            "network_name": station["network_name"],
            "is_live_source": station.get("network_type") == "live_news",
            "artwork_url": (
                f"/api/watch/channels/{station['channel_number']}/artwork"
            ),
            "programs": payload.get("schedule_blocks", []),
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
        "server_time": _iso(dt.datetime.now()),
        "channels": rows,
    }
