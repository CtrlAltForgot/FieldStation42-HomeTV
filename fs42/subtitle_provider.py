"""High-confidence, hash-matched English subtitle acquisition."""

from __future__ import annotations

import hashlib
import json
import os
import struct
import threading
from pathlib import Path

import requests

from fs42.station_manager import StationManager


LOCK = threading.Lock()
API_BASE = "https://api.opensubtitles.com/api/v1"


def subtitle_cache_root() -> Path:
    return Path(
        os.environ.get("FS42_SUBTITLE_CACHE_DIR", "catalog/.subtitle_cache")
    ).resolve()


def opensubtitles_hash(path: str) -> tuple[str, int]:
    """Return the official 64-bit OpenSubtitles movie hash and byte size."""
    size = os.path.getsize(path)
    if size < 131072:
        raise ValueError("Media is too small for reliable subtitle matching")
    value = size
    with open(path, "rb") as handle:
        for _ in range(8192):
            chunk = handle.read(8)
            if len(chunk) != 8:
                raise ValueError("Could not hash the beginning of the media")
            value = (value + struct.unpack("<Q", chunk)[0]) & 0xFFFFFFFFFFFFFFFF
        handle.seek(max(0, size - 65536))
        for _ in range(8192):
            chunk = handle.read(8)
            if len(chunk) != 8:
                raise ValueError("Could not hash the end of the media")
            value = (value + struct.unpack("<Q", chunk)[0]) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}", size


class SubtitleProvider:
    def __init__(self, api_key: str | None = None, session=None):
        self.api_key = api_key or StationManager().server_conf.get(
            "opensubtitles_api_key", ""
        )
        self.session = session or requests.Session()

    def fetch_english(self, media_path: str) -> Path | None:
        """Download only an exact file-hash match; never guess by title."""
        if not self.api_key:
            return None
        try:
            movie_hash, size = opensubtitles_hash(media_path)
        except (OSError, ValueError):
            return None
        root = subtitle_cache_root()
        root.mkdir(parents=True, exist_ok=True)
        cache_key = hashlib.sha256(
            f"{os.path.realpath(media_path)}\0{movie_hash}\0{size}".encode()
        ).hexdigest()
        target = root / f"{cache_key}.en.srt"
        if target.is_file() and target.stat().st_size:
            return target
        with LOCK:
            if target.is_file() and target.stat().st_size:
                return target
            headers = {
                "Api-Key": self.api_key,
                "User-Agent": "myHomeTV/1.0",
                "Accept": "application/json",
            }
            try:
                response = self.session.get(
                    f"{API_BASE}/subtitles",
                    params={
                        "moviehash": movie_hash,
                        "moviebytesize": size,
                        "languages": "en",
                        "order_by": "download_count",
                        "order_direction": "desc",
                    },
                    headers=headers,
                    timeout=15,
                )
                response.raise_for_status()
                matches = []
                for result in response.json().get("data", []):
                    attributes = result.get("attributes", {})
                    if attributes.get("moviehash_match") is not True:
                        continue
                    for item in attributes.get("files", []):
                        if item.get("file_id"):
                            matches.append((result, item))
                if not matches:
                    return None
                result, item = matches[0]
                download = self.session.post(
                    f"{API_BASE}/download",
                    json={"file_id": item["file_id"]},
                    headers=headers,
                    timeout=15,
                )
                download.raise_for_status()
                link = download.json().get("link")
                if not link:
                    return None
                payload = self.session.get(link, timeout=20)
                payload.raise_for_status()
                if not payload.content or len(payload.content) > 8 * 1024 * 1024:
                    return None
                temporary = target.with_suffix(".tmp")
                temporary.write_bytes(payload.content)
                os.replace(temporary, target)
                provenance = {
                    "provider": "opensubtitles.com",
                    "confidence": "exact-file-hash",
                    "movie_hash": movie_hash,
                    "feature_id": result.get("id"),
                    "file_id": item["file_id"],
                }
                target.with_suffix(".json").write_text(
                    json.dumps(provenance, indent=2) + "\n", encoding="utf-8"
                )
                return target
            except (requests.RequestException, OSError, ValueError, KeyError):
                return None
