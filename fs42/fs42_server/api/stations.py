from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from typing import Optional, List, Dict, Any
from fs42.station_manager import StationManager
from fs42.station_io import StationIO
from fs42.liquid_manager import LiquidManager
from fs42.broadcast_scheduler import BroadcastHistory
from fs42.broadcast_scheduler import BroadcastScheduler
from fs42.catalog import ShowCatalog
import datetime as dt

router = APIRouter(prefix="/stations", tags=["stations"])

# Pydantic Models
class StationConfigRequest(BaseModel):
    """Request model for creating/updating station configurations."""
    station_conf: Dict[str, Any] = Field(
        ...,
        description="Station configuration object containing network_name, channel_number, and other settings"
    )

    class Config:
        json_schema_extra = {
            "example": {
                "station_conf": {
                    "network_name": "MyChannel",
                    "channel_number": 42,
                    "network_type": "standard",
                    "schedule_increment": 30
                }
            }
        }

class StationConfigResponse(BaseModel):
    success: bool
    message: str
    network_name: Optional[str] = None
    channel_number: Optional[int] = None
    file_path: Optional[str] = None

class StationListResponse(BaseModel):
    count: int
    stations: List[Dict[str, Any]]

class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    details: Optional[str] = None

# Endpoints

@router.get("", response_model=StationListResponse)
async def list_stations():
    """
    List all stations returning raw file data without processing.
    """
    station_io = StationIO()
    raw_stations = station_io.list_raw_station_configs()

    return {
        "count": len(raw_stations),
        "stations": raw_stations
    }

@router.get("/{network_name}")
async def get_station_config(network_name: str):
    """
    Get a specific station configuration returning raw file data without processing.
    """
    station_io = StationIO()
    success, raw_data, error_msg = station_io.read_raw_station_config(network_name)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=error_msg
        )

    return {"network_name": network_name, "station_config": raw_data}


@router.get("/{network_name}/scheduler-history")
async def get_scheduler_history(network_name: str):
    """Summarize durable aired/reserved state for the scheduler editor."""
    station_manager = StationManager()
    if station_manager.station_by_name(network_name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station '{network_name}' not found",
        )
    history = BroadcastHistory(station_manager.server_conf["db_path"])
    return {
        "network_name": network_name,
        "series": history.summaries(network_name),
    }


@router.get("/{network_name}/scheduler-preview")
async def preview_scheduler(network_name: str, day: str, hour: int, weeks: int = 6):
    """Project weekly choices in memory without reserving or changing history."""
    manager = StationManager()
    station = manager.station_by_name(network_name)
    if station is None:
        raise HTTPException(404, f"Station '{network_name}' not found")
    day = day.casefold()
    weekdays = {
        name: index for index, name in enumerate(
            ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        )
    }
    if day not in weekdays or not 0 <= hour <= 23 or not 1 <= weeks <= 12:
        raise HTTPException(400, "Invalid scheduler preview range")
    slot = station.get(day, {}).get(str(hour), station.get(day, {}).get(hour))
    if not isinstance(slot, dict) or not isinstance(slot.get("programming"), dict):
        raise HTTPException(400, "This slot does not have a realistic scheduler policy")
    tags = slot.get("tags")
    tag = tags[0] if isinstance(tags, list) and tags else tags
    if not tag:
        raise HTTPException(400, "This slot has no content tag")
    catalog = ShowCatalog(station)
    candidates = catalog.get_all_by_tag(tag) or []
    scheduler = BroadcastScheduler(network_name, manager.server_conf["db_path"])
    today = dt.datetime.now().replace(hour=hour, minute=0, second=0, microsecond=0)
    days_ahead = (weekdays[day] - today.weekday()) % 7
    first = today + dt.timedelta(days=days_ahead)
    if first < dt.datetime.now():
        first += dt.timedelta(weeks=1)
    preview = []
    for index in range(weeks):
        when = first + dt.timedelta(weeks=index)
        selected = scheduler.select(slot, str(tag), candidates, when)
        if not selected:
            preview.append({
                "start": when.isoformat(), "status": "fallback",
                "fallback_tag": slot["programming"].get("fallback_tag")
                or station.get("fallback_tag"),
            })
            continue
        entry, programming = selected
        scheduler.stage(
            programming, when,
            when + dt.timedelta(seconds=max(1, float(entry.duration))),
        )
        preview.append({
            "start": when.isoformat(),
            "status": programming["airing_kind"],
            "series": programming["series"],
            "season": programming["season"],
            "episode": programming["episode"],
            "part": programming.get("part", ""),
        })
    return {"network_name": network_name, "slot_id": slot["programming"].get("slot_id"), "preview": preview}

@router.post("", response_model=StationConfigResponse, status_code=status.HTTP_201_CREATED)
async def create_station(config: StationConfigRequest):
    """
    Create a new station configuration.
    Uses StationManager for validation and processing, then reloads.
    """
    station_manager = StationManager()

    # Extract network_name for checking
    if "network_name" not in config.station_conf:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="network_name is required in station_conf"
        )

    network_name = config.station_conf["network_name"]

    # Check if station already exists
    if station_manager.station_by_name(network_name) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Station '{network_name}' already exists. Use PUT to update."
        )

    # Write the configuration (StationManager handles validation and file I/O via StationIO)
    success, message, file_path = station_manager.write_station_config(
        network_name,
        config.model_dump(),
        is_update=False
    )

    if success:
        LiquidManager().reload_schedules()

    if not success:
        # Determine if it's a validation error or conflict
        if "already used" in message or "already exists" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=message
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=message
            )

    return {
        "success": True,
        "message": message,
        "network_name": network_name,
        "channel_number": config.station_conf.get("channel_number"),
        "file_path": file_path
    }

@router.put("/{network_name}", response_model=StationConfigResponse)
async def update_station(network_name: str, config: StationConfigRequest):
    """
    Update an existing station configuration.
    Uses StationManager for validation and processing, then reloads.
    """
    station_manager = StationManager()

    # Check if station exists
    if station_manager.station_by_name(network_name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station '{network_name}' not found"
        )

    # Write the configuration (update mode, StationManager handles validation and file I/O via StationIO)
    success, message, file_path = station_manager.write_station_config(
        network_name,
        config.model_dump(),
        is_update=True
    )

    if success:
        LiquidManager().reload_schedules()

    if not success:
        # Determine if it's a validation error or conflict
        if "already used" in message or "already exists" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=message
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=message
            )

    new_network_name = config.station_conf.get("network_name", network_name)

    return {
        "success": True,
        "message": message,
        "network_name": new_network_name,
        "channel_number": config.station_conf.get("channel_number"),
        "file_path": file_path
    }

@router.delete("/{network_name}", response_model=StationConfigResponse)
async def delete_station(network_name: str):
    """
    Delete a station configuration.
    Uses StationManager for existence checks and file deletion, then reloads.
    """
    station_manager = StationManager()

    # Check if station exists
    if station_manager.station_by_name(network_name) is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Station '{network_name}' not found"
        )

    # Delete the configuration (StationManager handles file deletion via StationIO)
    success, message = station_manager.delete_station_config(network_name)

    if success:
        LiquidManager().reload_schedules()

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=message
        )

    return {
        "success": True,
        "message": message,
        "network_name": network_name
    }
