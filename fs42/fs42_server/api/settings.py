"""Redacted, atomic provider settings for the myHomeTV admin UI."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import requests
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from fs42.station_manager import StationManager
from .tmdb_helper import refresh_tmdb_helper


router = APIRouter(prefix="/settings", tags=["settings"])
CONFIG_PATH = Path("confs/main_config.json")


class ProviderSettings(BaseModel):
    tmdb_api_key: str | None = None
    opensubtitles_api_key: str | None = None
    clear_tmdb: bool = False
    clear_opensubtitles: bool = False


def _masked(value: str | None) -> str | None:
    return f"••••{value[-4:]}" if value else None


def _response(manager: StationManager):
    tmdb = str(manager.server_conf.get("tmdb_api_key", ""))
    opensubs = str(manager.server_conf.get("opensubtitles_api_key", ""))
    return {
        "tmdb": {"configured": bool(tmdb), "masked": _masked(tmdb)},
        "opensubtitles": {
            "configured": bool(opensubs), "masked": _masked(opensubs)
        },
    }


def _validate_tmdb_key(api_key: str) -> None:
    """Verify a replacement key before touching the known-good settings file."""
    try:
        response = requests.get(
            "https://api.themoviedb.org/3/configuration",
            params={"api_key": api_key},
            headers={"Accept": "application/json"},
            timeout=8,
        )
    except requests.RequestException as error:
        raise HTTPException(
            503,
            "Could not reach TMDB to test that key. Your existing key was not changed.",
        ) from error
    if response.status_code in {401, 403}:
        raise HTTPException(
            400, "TMDB rejected that key. Check the v3 API key and try again."
        )
    try:
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(
            502,
            "TMDB could not validate the key right now. Your existing key was not changed.",
        ) from error


@router.get("")
async def get_settings():
    return _response(StationManager())


@router.put("")
async def save_settings(settings: ProviderSettings):
    tmdb = (settings.tmdb_api_key or "").strip()
    opensubs = (settings.opensubtitles_api_key or "").strip()
    if tmdb and not re.fullmatch(r"[A-Fa-f0-9]{32}", tmdb):
        raise HTTPException(400, "TMDB v3 API key must contain 32 hexadecimal characters")
    if opensubs and not 8 <= len(opensubs) <= 128:
        raise HTTPException(400, "OpenSubtitles API key has an invalid length")

    manager = StationManager()
    existing_tmdb = str(manager.server_conf.get("tmdb_api_key", ""))
    if tmdb and tmdb != existing_tmdb:
        _validate_tmdb_key(tmdb)

    current = {}
    if CONFIG_PATH.is_file():
        try:
            current = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise HTTPException(500, f"Could not read application settings: {error}")
    if settings.clear_tmdb:
        current.pop("tmdb_api_key", None)
    elif tmdb:
        current["tmdb_api_key"] = tmdb
    if settings.clear_opensubtitles:
        current.pop("opensubtitles_api_key", None)
    elif opensubs:
        current["opensubtitles_api_key"] = opensubs

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=CONFIG_PATH.parent,
            prefix=".main-config-", suffix=".tmp", delete=False,
        ) as handle:
            temporary_name = handle.name
            json.dump(current, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, CONFIG_PATH)
    except OSError as error:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)
        raise HTTPException(500, f"Could not save application settings: {error}")

    if settings.clear_tmdb:
        manager.server_conf.pop("tmdb_api_key", None)
    elif tmdb:
        manager.server_conf["tmdb_api_key"] = tmdb
    if settings.clear_opensubtitles:
        manager.server_conf.pop("opensubtitles_api_key", None)
    elif opensubs:
        manager.server_conf["opensubtitles_api_key"] = opensubs
    refresh_tmdb_helper()
    return _response(manager)
