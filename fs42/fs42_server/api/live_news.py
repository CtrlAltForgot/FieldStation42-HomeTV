from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fs42.live_news import SOURCES, station_config
from fs42.station_manager import StationManager

router = APIRouter(prefix="/api/live-news", tags=["live-news"])


class InstallRequest(BaseModel):
    source_ids: list[str] | None = None


@router.get("/sources")
async def sources():
    manager = StationManager()
    installed = {
        (station.get("live_source") or {}).get("source_id"): station.get("channel_number")
        for station in manager.stations if station.get("network_type") == "live_news"
    }
    return {"sources": [{**item, "installed_channel": installed.get(item["id"])} for item in SOURCES]}


@router.post("/install")
async def install(body: InstallRequest):
    requested = set(body.source_ids or [item["id"] for item in SOURCES])
    unknown = requested - {item["id"] for item in SOURCES}
    if unknown:
        raise HTTPException(422, f"Unknown live-news source: {sorted(unknown)[0]}")
    manager = StationManager()
    used = {int(station["channel_number"]) for station in manager.stations}
    existing = {
        (station.get("live_source") or {}).get("source_id"): station
        for station in manager.stations if station.get("network_type") == "live_news"
    }
    results = []
    for item in SOURCES:
        if item["id"] not in requested:
            continue
        old = existing.get(item["id"])
        channel = int(old["channel_number"]) if old else item["channel"]
        while not old and channel in used:
            channel += 1
        config = station_config(item, channel)
        success, message, _ = manager.write_station_config(
            old["network_name"] if old else item["name"], config, is_update=bool(old)
        )
        if not success:
            raise HTTPException(409, message)
        used.add(channel)
        results.append({"source_id": item["id"], "name": item["name"], "channel": channel})
    return {"installed": results}
