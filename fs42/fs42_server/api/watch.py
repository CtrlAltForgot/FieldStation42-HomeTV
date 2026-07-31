import asyncio
import logging
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

router = APIRouter(prefix="/api/watch", tags=["watch"])
LOG = logging.getLogger("HomeTV.Client")
PLAYLIST_STARTUP_ATTEMPTS = 200
PLAYLIST_STARTUP_INTERVAL = 0.1
PLAYLIST_READY_SEGMENTS = 1
PLAYLIST_READY_ATTEMPTS = 300


class SessionRequest(BaseModel):
    channel: str
    profile: str = "auto"


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
        return _now_payload(resolver.now(channel, timestamp), timestamp)
    except WatchError as exc:
        raise _watch_error(exc)


@router.post("/sessions", status_code=status.HTTP_201_CREATED)
async def create_session(body: SessionRequest, request: Request):
    session = None
    try:
        manager = _manager(request)
        session, airing = manager.create(body.channel, body.profile)
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
