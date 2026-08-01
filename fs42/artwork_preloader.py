"""Proactively prepare canonical artwork before viewers browse the guide."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import threading
from pathlib import Path

from fs42.catalog_api import CatalogAPI
from fs42.metadata_enrichment import (
    MetadataEnricher, _episode_identity, _series_key, artwork_root,
)
from fs42.metadata_io import MetadataIO
from fs42.station_manager import StationManager


_LOCK = threading.Lock()
_STATUS = {"state": "idle", "total": 0, "complete": 0, "failed": 0}
_INDEX = {"version": 2, "generated": None, "entries": {}, "paths": {}}
INDEX_NAME = "guide-artwork-index.json"


def _index_path() -> Path:
    return artwork_root() / INDEX_NAME


def canonical_key(path: str, metadata: dict | None = None) -> tuple[str, str, str]:
    """Return (key, kind, label) without ever using an episode title as art ID."""
    metadata = metadata or {}
    identity = _episode_identity(path, metadata)
    series = str(identity.get("series") or "").strip()
    if series:
        return f"series:{_series_key(series)}", "series", series
    resolved = os.path.realpath(path)
    title = str(metadata.get("title") or Path(path).stem).strip()
    digest = hashlib.sha256(resolved.encode()).hexdigest()
    return f"movie:{digest}", "movie", title


def _valid_record(record: dict | None) -> bool:
    name = Path(str((record or {}).get("file", ""))).name
    target = artwork_root() / name
    return bool(
        re.fullmatch(r"[a-f0-9]{64}\.jpg", name)
        and target.is_file()
        and target.stat().st_size
    )


def _load_index() -> dict:
    try:
        value = json.loads(_index_path().read_text(encoding="utf-8"))
        if isinstance(value, dict) and isinstance(value.get("entries"), dict):
            return value
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        pass
    return {"version": 2, "generated": None, "entries": {}, "paths": {}}


def _path_digest(path: str) -> str:
    return hashlib.sha256(os.path.realpath(path).encode()).hexdigest()


def artwork_record(path: str, metadata: dict | None = None) -> dict | None:
    """Read one already-prepared canonical record; never generates artwork."""
    with _LOCK:
        key = _INDEX.get("paths", {}).get(_path_digest(path))
        if not key:
            key, _kind, _label = canonical_key(path, metadata)
        record = dict(_INDEX.get("entries", {}).get(key, {}))
    return record if _valid_record(record) else None


def artwork_url(path: str, metadata: dict | None = None) -> str | None:
    record = artwork_record(path, metadata)
    return f"/api/watch/artwork/{record['file']}" if record else None


def _ensure_named_fallback(key: str, label: str) -> str | None:
    """Create a deterministic program-specific card only as a last resort."""
    from PIL import Image, ImageDraw, ImageFont

    name = hashlib.sha256(f"named-guide-art\0{key}".encode()).hexdigest() + ".jpg"
    target = artwork_root() / name
    if target.is_file() and target.stat().st_size:
        return name
    artwork_root().mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(key.encode()).digest()
    base = (10 + digest[0] // 8, 25 + digest[1] // 7, 42 + digest[2] // 6)
    image = Image.new("RGB", (1280, 720), base)
    draw = ImageDraw.Draw(image)
    for y in range(720):
        shade = int(35 * y / 719)
        draw.line((0, y, 1280, y), fill=tuple(max(0, value - shade) for value in base))
    draw.rounded_rectangle((72, 72, 1208, 648), 36, outline=(88, 206, 255), width=5)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 72)
        small = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
    except OSError:
        font = ImageFont.load_default()
        small = font
    words = label.split()
    lines, line = [], ""
    for word in words:
        candidate = f"{line} {word}".strip()
        if line and draw.textbbox((0, 0), candidate, font=font)[2] > 1010:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    lines = lines[:3] or ["myHomeTV"]
    heights = [draw.textbbox((0, 0), value, font=font)[3] for value in lines]
    y = (720 - sum(heights) - 24 * (len(lines) - 1)) // 2
    for value, height in zip(lines, heights):
        width = draw.textbbox((0, 0), value, font=font)[2]
        draw.text(((1280 - width) / 2, y), value, font=font, fill="white")
        y += height + 24
    caption = "SERIES" if key.startswith("series:") else "MOVIE"
    draw.text((96, 100), caption, font=small, fill=(98, 213, 255))
    temporary = target.with_suffix(".tmp.jpg")
    image.save(temporary, "JPEG", quality=88, optimize=True)
    temporary.replace(target)
    return name if target.is_file() and target.stat().st_size else None


def status() -> dict:
    with _LOCK:
        return dict(_STATUS)


def prepare_all_series() -> dict:
    """Cache every series and movie; safe to repeat after every rebuild."""
    paths: dict[str, tuple[str, str, dict]] = {}
    movies: dict[str, tuple[str, dict]] = {}
    path_keys: dict[str, str] = {}
    try:
        for station in StationManager().stations:
            if not station.get("_has_catalog"):
                continue
            for entry in CatalogAPI.get_entries(station) or []:
                path = getattr(entry, "path", "")
                if (
                    not path
                    or getattr(entry, "media_type", "video") != "video"
                    or getattr(entry, "content_type", "feature") != "feature"
                ):
                    continue
                metadata = MetadataIO.read(path) or {}
                identity = _episode_identity(path, metadata)
                series = str(identity.get("series") or "").strip()
                if series:
                    series_key = _series_key(series)
                    paths.setdefault(series_key, (series, path, metadata))
                    path_keys[_path_digest(path)] = f"series:{series_key}"
                else:
                    key, _kind, _label = canonical_key(path, metadata)
                    movies.setdefault(key, (path, metadata))
                    path_keys[_path_digest(path)] = key
    except Exception as exc:
        with _LOCK:
            _STATUS.update(
                state="degraded", total=0, complete=0, failed=1,
                error=str(exc),
            )
        return status()
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(
            state="running", total=len(paths) + len(movies),
            complete=0, failed=0,
        )
    enricher = MetadataEnricher()
    previous = _load_index()
    current_keys = {
        *(f"series:{key}" for key in paths),
        *movies,
    }
    entries = {
        key: record for key, record in previous.get("entries", {}).items()
        if key in current_keys and _valid_record(record)
    }
    failures = []
    for series, path, _metadata in paths.values():
        try:
            key, kind, label = canonical_key(path, _metadata)
            existing = entries.get(key)
            name = existing.get("file") if _valid_record(existing) else None
            if not name:
                name = (
                    enricher.ensure_series_artwork(series, path)
                    or _ensure_named_fallback(key, series)
                )
            if not name:
                raise RuntimeError("no readable artwork source")
            entries[key] = {
                "file": Path(name).name, "kind": kind, "label": label,
            }
            with _LOCK:
                _STATUS["complete"] += 1
        except Exception as exc:
            failures.append({"kind": "series", "label": series, "error": str(exc)})
            with _LOCK:
                _STATUS["failed"] += 1
    for key, (path, metadata) in movies.items():
        try:
            existing = entries.get(key)
            name = existing.get("file") if _valid_record(existing) else None
            configured = Path(str(metadata.get("artwork_file", ""))).name
            record = {"file": configured} if configured else None
            if not name:
                name = configured if _valid_record(record) else enricher.ensure_local_artwork(path)
            if not name:
                name = _ensure_named_fallback(key, str(metadata.get("title") or Path(path).stem))
            if not name:
                raise RuntimeError("no readable movie artwork source")
            _key, kind, label = canonical_key(path, metadata)
            entries[key] = {
                "file": Path(name).name, "kind": kind, "label": label,
            }
            with _LOCK:
                _STATUS["complete"] += 1
        except Exception as exc:
            failures.append({"kind": "movie", "label": path, "error": str(exc)})
            with _LOCK:
                _STATUS["failed"] += 1
    index = {
        "version": 2,
        "generated": dt.datetime.now().isoformat(),
        "entries": entries,
        "paths": path_keys,
    }
    target = _index_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(index, indent=2), encoding="utf-8")
    temporary.replace(target)
    with _LOCK:
        _INDEX.clear()
        _INDEX.update(index)
        _STATUS["state"] = "ready" if not _STATUS["failed"] else "degraded"
        _STATUS["failures"] = failures[:50]
        _STATUS["indexed"] = len(entries)
    return status()


with _LOCK:
    _INDEX.update(_load_index())
