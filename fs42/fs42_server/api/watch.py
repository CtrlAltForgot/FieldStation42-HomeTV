import asyncio
import datetime as dt
import logging
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel

from fs42.hometv import (
    ChannelNotFound,
    ProgramNotFound,
    UnsafeMediaPath,
    WatchError,
)
from fs42.fs42_server.api.schedules import program_display
from fs42.metadata_io import MetadataIO
from fs42.artwork_preloader import status as artwork_preload_status
from fs42.metadata_enrichment import artwork_root
from fs42.artwork_preloader import artwork_record
from fs42.live_news import (
    artwork_svg, discover_direct_hls, discover_live_video,
    now_payload as live_now_payload,
    station_source,
)

router = APIRouter(prefix="/api/watch", tags=["watch"])
LOG = logging.getLogger("myHomeTV.Client")
PLAYLIST_STARTUP_ATTEMPTS = 200
PLAYLIST_STARTUP_INTERVAL = 0.1
PLAYLIST_READY_SEGMENTS = 1
PLAYLIST_READY_ATTEMPTS = 300


@router.get("/artwork-status")
async def artwork_status():
    return artwork_preload_status()


class SessionRequest(BaseModel):
    channel: str
    profile: str = "auto"
    boundary_at: dt.datetime | None = None
    subtitles: str = "auto"
    refresh_live: bool = False
    client: str = "browser"


class ClientEvent(BaseModel):
    session_id: str | None = None
    event: str
    detail: str = ""


def _manager(request: Request):
    manager = getattr(request.app.state, "hls_sessions", None)
    if manager is None:
        raise HTTPException(503, "Streaming service is not initialized")
    return manager


def _watch_error(exc: Exception):
    if isinstance(exc, ChannelNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(exc))
    if isinstance(exc, (ProgramNotFound, UnsafeMediaPath)):
        return HTTPException(status.HTTP_409_CONFLICT, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc))
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc))


def _now_payload(airing, timestamp):
    payload = airing.public_dict(timestamp)
    identity_path = airing.identity_path or airing.media_path
    display = program_display(
        identity_path,
        airing.program_title,
        MetadataIO.read(identity_path),
    )
    payload["program_title"] = display["display_title"]
    payload["episode_title"] = display.get("episode_title", "")
    payload["season"] = display.get("season")
    payload["episode"] = display.get("episode")
    payload["program_details"] = display.get("program_details", "")
    return payload


@router.get("/channels")
async def channels(request: Request):
    return {"channels": _manager(request).resolver.channels()}


@router.get("/status")
async def broadcast_status(request: Request):
    return _manager(request).status()


@router.post("/client-events", status_code=204)
async def client_event(body: ClientEvent):
    LOG.warning(
        "Browser event session=%s event=%s detail=%s",
        body.session_id or "none",
        body.event[:80],
        body.detail[:500],
    )
    return Response(status_code=204)


@router.get("/channels/{channel}/now")
async def now(channel: str, request: Request):
    try:
        resolver = _manager(request).resolver
        timestamp = __import__("datetime").datetime.now()
        station = resolver.station(channel)
        if station.get("network_type") == "live_news":
            return live_now_payload(station, timestamp)
        return _now_payload(resolver.now(channel, timestamp), timestamp)
    except WatchError as exc:
        raise _watch_error(exc)


@router.post("/channels/{channel}/prewarm", status_code=202)
async def prewarm(channel: str, request: Request):
    """Start an unleased channel broadcast while the viewer browses the guide."""
    manager = _manager(request)
    try:
        station = manager.resolver.station(channel)
        if station.get("network_type") == "live_news":
            hls_url = await asyncio.to_thread(
                discover_direct_hls, station, False
            )
            return {
                "channel": channel,
                "prewarmed": bool(hls_url),
                "reason": "external_live",
            }
        session, _airing = await asyncio.to_thread(
            manager.create, channel, "auto", subtitle_mode="auto"
        )
        manager.delete(session.session_id)
        return {"channel": channel, "prewarmed": True}
    except (WatchError, ValueError) as exc:
        raise _watch_error(exc)


@router.get("/channels/{channel}/artwork")
async def artwork(channel: str, request: Request, at: dt.datetime | None = None):
    """Serve only an already-indexed canonical series/movie image."""
    try:
        resolver = _manager(request).resolver
        station = resolver.station(channel)
        if station.get("network_type") == "live_news":
            return Response(
                artwork_svg(station), media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        airing = resolver.now(channel, at or dt.datetime.now())
        identity = airing.identity_path or airing.media_path
        approved = Path(
            resolver._approved_media_path(resolver.station(channel), identity)
        )
    except WatchError as exc:
        raise _watch_error(exc)

    metadata = MetadataIO.read(str(approved)) or {}
    record = artwork_record(str(approved), metadata)
    if not record:
        raise HTTPException(503, "Canonical program artwork is not ready")
    return _artwork_file(record["file"])


def _artwork_file(asset: str):
    name = Path(asset).name
    target = (artwork_root() / name).resolve()
    if not (
        re.fullmatch(r"[a-f0-9]{64}\.jpg", name)
        and target.parent == artwork_root()
        and target.is_file()
        and target.stat().st_size
    ):
        raise HTTPException(404, "Artwork asset not found")
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/artwork/{asset}")
async def canonical_artwork(asset: str):
    """Serve a content-addressed guide image with year-long caching."""
    return _artwork_file(asset)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionRequest, request: Request):
    session = None
    started_at = time.monotonic()
    try:
        manager = _manager(request)
        # Some integrations provide a minimal resolver and delegate all
        # validation to manager.create(); station lookup is only required for
        # the purpose-built live-news branch.
        station = (
            manager.resolver.station(body.channel)
            if hasattr(manager.resolver, "station") else None
        )
        if station and station.get("network_type") == "live_news":
            if body.profile not in {"auto", "copy"}:
                raise ValueError("Unknown playback profile")
            item = station_source(station)
            if not item:
                raise ValueError("This live-news source is not configured safely")
            timestamp = dt.datetime.now()
            if body.client not in {"browser", "roku", "webos"}:
                raise ValueError("Unknown television client")
            hls_url = await asyncio.to_thread(
                discover_direct_hls, station, body.refresh_live
            )
            video_id = None
            if not hls_url and body.client == "browser":
                video_id = await asyncio.to_thread(
                    discover_live_video, station, body.refresh_live
                )
            if not video_id and not hls_url:
                raise ProgramNotFound(
                    "The publisher does not currently expose a playable live broadcast"
                )
            if hls_url:
                return {
                    "session_id": None, "playback_kind": "external_hls",
                    "playlist_url": hls_url,
                    "now": live_now_payload(station, timestamp),
                    "tune_metrics": {"playlist_ready_ms": 0, "shared_broadcast_age_ms": 0},
                }
            return {
                "session_id": None,
                "playback_kind": "embed",
                "embed_url": (
                    "/static/live_news_player.html?video=" + video_id
                ),
                "now": live_now_payload(station, timestamp),
                "tune_metrics": {"playlist_ready_ms": 0, "shared_broadcast_age_ms": 0},
            }
        boundary_at = body.boundary_at
        if boundary_at is not None:
            now = dt.datetime.now(tz=boundary_at.tzinfo)
            if abs((now - boundary_at).total_seconds()) > 120:
                raise ValueError("Playback boundary is outside the live window")
        session, airing = (
            manager.create(
                body.channel,
                body.profile,
                boundary_at=boundary_at,
                subtitle_mode=body.subtitles,
            )
            if boundary_at is not None
            else manager.create(
                body.channel,
                body.profile,
                subtitle_mode=body.subtitles,
            )
        )
        for _ in range(PLAYLIST_READY_ATTEMPTS):
            if await request.is_disconnected():
                manager.delete(session.session_id)
                raise HTTPException(499, "Channel tune was cancelled")
            if manager.ready(session.session_id, PLAYLIST_READY_SEGMENTS):
                break
            if session.process.poll() is not None:
                manager.fail(session.session_id)
                raise HTTPException(
                    502, "Broadcaster exited before producing a playable buffer"
                )
            await asyncio.sleep(PLAYLIST_STARTUP_INTERVAL)
        else:
            manager.fail(session.session_id)
            raise HTTPException(
                504, "Broadcaster did not produce a playable buffer in time"
            )
        if await request.is_disconnected():
            manager.delete(session.session_id)
            raise HTTPException(499, "Channel tune was cancelled")
        return {
            "session_id": session.session_id,
            "playlist_url": f"/api/watch/sessions/{session.session_id}/master.m3u8",
            "tune_metrics": {
                "playlist_ready_ms": round(
                    (time.monotonic() - started_at) * 1000
                ),
                "shared_broadcast_age_ms": round(
                    max(0, time.monotonic() - session.created_at)
                    * 1000
                ),
            },
            "now": _now_payload(
                airing, __import__("datetime").datetime.now()
            ),
        }
    except (WatchError, ValueError) as exc:
        raise _watch_error(exc)
    except FileNotFoundError:
        raise HTTPException(503, "FFmpeg is not installed or not executable")
    except asyncio.CancelledError:
        if session is not None:
            manager.delete(session.session_id)
        raise


@router.get("/sessions/{session_id}/master.m3u8")
async def playlist(session_id: str, request: Request):
    return await _serve_asset(session_id, "master.m3u8", request)


@router.get("/sessions/{session_id}/subtitles.vtt")
async def subtitles(session_id: str, request: Request, mode: str = "auto"):
    try:
        path = await asyncio.to_thread(
            _manager(request).subtitle_asset, session_id, mode
        )
    except (KeyError, ValueError):
        raise HTTPException(404, "Subtitle track is unavailable")
    if path is None:
        raise HTTPException(404, "No matching English subtitle track was found")
    return FileResponse(
        path,
        media_type="text/vtt",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.get("/sessions/{session_id}/{asset}")
async def segment(session_id: str, asset: str, request: Request):
    return await _serve_asset(session_id, asset, request)


async def _serve_asset(session_id: str, asset: str, request: Request):
    manager = _manager(request)
    try:
        path = manager.asset(session_id, asset)
    except KeyError:
        raise HTTPException(404, "Stream session not found")
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    # FFmpeg startup is asynchronous. Briefly wait for the first playlist.
    if asset == "master.m3u8":
        for _ in range(PLAYLIST_STARTUP_ATTEMPTS):
            if path.is_file():
                break
            session = manager.get(session_id)
            if session.process.poll() is not None:
                manager.delete(session_id)
                raise HTTPException(502, "FFmpeg exited before creating a playlist")
            await asyncio.sleep(PLAYLIST_STARTUP_INTERVAL)
    if not path.is_file():
        if asset == "master.m3u8":
            manager.delete(session_id)
        raise HTTPException(404, "HLS asset is not ready")
    media_type = (
        "application/vnd.apple.mpegurl"
        if path.suffix == ".m3u8"
        else "video/mp2t"
    )
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-store"})


@router.post("/sessions/{session_id}/heartbeat", status_code=204)
async def heartbeat(session_id: str, request: Request):
    try:
        _manager(request).get(session_id)
    except KeyError:
        raise HTTPException(404, "Stream session not found")
    return Response(status_code=204)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(session_id: str, request: Request):
    if not _manager(request).delete(session_id):
        raise HTTPException(404, "Stream session not found")
    return Response(status_code=204)
