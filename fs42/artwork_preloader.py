"""Proactively prepare canonical artwork before viewers browse the guide."""

from __future__ import annotations

import threading

from fs42.catalog_api import CatalogAPI
from fs42.metadata_enrichment import MetadataEnricher, _episode_identity
from fs42.metadata_io import MetadataIO
from fs42.station_manager import StationManager


_LOCK = threading.Lock()
_STATUS = {"state": "idle", "total": 0, "complete": 0, "failed": 0}


def status() -> dict:
    with _LOCK:
        return dict(_STATUS)


def prepare_all_series() -> dict:
    """Cache one image per catalog series; safe to repeat after every rebuild."""
    paths: dict[str, tuple[str, str]] = {}
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
                    paths.setdefault(series.casefold(), (series, path))
    except Exception as exc:
        with _LOCK:
            _STATUS.update(
                state="degraded", total=0, complete=0, failed=1,
                error=str(exc),
            )
        return status()
    with _LOCK:
        _STATUS.clear()
        _STATUS.update(state="running", total=len(paths), complete=0, failed=0)
    enricher = MetadataEnricher()
    for series, path in paths.values():
        try:
            if not enricher.ensure_series_artwork(series, path):
                raise RuntimeError("no readable artwork source")
            with _LOCK:
                _STATUS["complete"] += 1
        except Exception:
            with _LOCK:
                _STATUS["failed"] += 1
    with _LOCK:
        _STATUS["state"] = "ready" if not _STATUS["failed"] else "degraded"
    return status()
