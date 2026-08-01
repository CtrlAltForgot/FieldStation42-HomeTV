"""Incremental TMDB metadata and artwork enrichment for catalog video files."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable, Iterable

from fs42.database import connect
from fs42.media_processor import MediaProcessor
from fs42.title_parser import TitleParser

LOG = logging.getLogger("MetadataEnrichment")
EPISODE_RE = re.compile(
    r"(?i)^(?P<series>.*?)[\s._-]*s(?P<season>\d{1,3})"
    r"[\s._-]*e(?P<episode>\d{1,3})[a-z]?[\s._-]*(?P<title>.*)$"
)
LEADING_EPISODE_RE = re.compile(
    r"(?i)^0*(?P<season>\d{1,3})x0*(?P<episode>\d{1,3})"
    r"[\s._-]*(?P<title>.*)$"
)
SEASON_DIR_RE = re.compile(r"(?i)^(?:season[\s._-]*|s)(\d+)$")
YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
ARTWORK_LOCK = threading.Lock()


def artwork_root() -> Path:
    return Path(
        os.environ.get("FS42_METADATA_ART_DIR", "catalog/.metadata_art")
    ).resolve()


def _episode_identity(path: str, existing: dict) -> dict:
    media = Path(path)
    filename = media.stem
    match = EPISODE_RE.match(filename) or LEADING_EPISODE_RE.match(filename)
    if not match and existing.get("type") != "episode":
        return {}
    parent = media.parent
    if SEASON_DIR_RE.match(parent.name):
        parent = parent.parent
    series = existing.get("show_title")
    if not series and match and match.groupdict().get("series"):
        series = TitleParser.parse_title(match.group("series").strip(" ._-"))
    series = series or TitleParser.parse_title(parent.name)
    def number(value):
        match_number = re.match(r"0*(\d+)", str(value or ""))
        return int(match_number.group(1)) if match_number else None

    existing_season = number(existing.get("season"))
    existing_episode = number(existing.get("episode"))
    return {
        "series": series,
        "season": existing_season if existing_season is not None else (int(match.group("season")) if match else None),
        "episode": existing_episode if existing_episode is not None else (int(match.group("episode")) if match else None),
        "title": existing.get("title") or (
            TitleParser.parse_title(match.group("title"))
            if match and match.groupdict().get("title") else ""
        ),
    }


def _movie_query(path: str, existing: dict) -> str:
    if existing.get("title"):
        title = str(existing["title"])
        if existing.get("year"):
            return f"{title} ({existing['year']})"
        return title
    candidate = Path(path).stem
    return TitleParser.parse_title(candidate)


class MetadataEnricher:
    """Fill missing video metadata without re-querying completed entries."""

    def __init__(self, helper=None, db_path: str | None = None):
        if helper is None:
            from fs42.fs42_server.api.tmdb_helper import get_tmdb_helper

            helper = get_tmdb_helper()
        self.helper = helper
        if db_path is None:
            from fs42.station_manager import StationManager

            db_path = StationManager().server_conf["db_path"]
        self.db_path = db_path
        self.art_dir = artwork_root()

    def scan(
        self,
        paths: Iterable[str],
        force: bool = False,
        log: Callable[[str], None] | None = None,
    ) -> dict:
        report = log or (lambda message: LOG.info(message))
        unique_paths = list(dict.fromkeys(os.path.realpath(path) for path in paths))
        stats = {"total": len(unique_paths), "updated": 0, "skipped": 0, "unmatched": 0}
        tmdb_configured = self.helper.is_configured()
        if not tmdb_configured:
            report(
                "TMDB is not configured; caching local video stills without "
                "online descriptions."
            )
            stats["unconfigured"] = True
        self.art_dir.mkdir(parents=True, exist_ok=True)
        report(f"Scanning metadata for {len(unique_paths)} catalog features.")
        with connect(self.db_path) as connection:
            for index, path in enumerate(unique_paths, start=1):
                row = connection.execute(
                    "SELECT meta, media_type FROM file_meta WHERE path=?", (path,)
                ).fetchone()
                if not row or (row[1] and row[1] != "video"):
                    stats["skipped"] += 1
                    continue
                try:
                    existing = json.loads(row[0]) if row[0] else {}
                except (TypeError, json.JSONDecodeError):
                    existing = {}
                art_exists = bool(
                    existing.get("artwork_file")
                    and (self.art_dir / Path(existing["artwork_file"]).name).is_file()
                )
                complete = bool(
                    art_exists
                    and (
                        not tmdb_configured
                        or (existing.get("plot") and existing.get("title"))
                    )
                )
                if complete and not force:
                    stats["skipped"] += 1
                    continue
                try:
                    enriched = (
                        self._enrich(path, existing)
                        if tmdb_configured
                        else dict(existing)
                    )
                    enriched = enriched or dict(existing)
                    if not art_exists and not enriched.get("artwork_file"):
                        identity = _episode_identity(path, enriched)
                        artwork_file = (
                            self.ensure_series_artwork(identity["series"], path)
                            if identity.get("series")
                            else self.ensure_local_artwork(path)
                        )
                        if artwork_file:
                            enriched["artwork_file"] = artwork_file
                            if identity.get("series"):
                                enriched["series_artwork_file"] = artwork_file
                            enriched["artwork_source"] = "video-still"
                except Exception as exc:
                    LOG.warning("Metadata enrichment failed for %s: %s", path, exc)
                    stats["unmatched"] += 1
                    continue
                if not enriched:
                    stats["unmatched"] += 1
                    continue
                connection.execute(
                    "UPDATE file_meta SET meta=?, last_checked=? WHERE path=?",
                    (json.dumps(enriched), dt.datetime.now(), path),
                )
                connection.commit()
                stats["updated"] += 1
                if index == 1 or index % 25 == 0 or index == len(unique_paths):
                    report(
                        f"Metadata progress: {index}/{len(unique_paths)} "
                        f"({stats['updated']} updated, {stats['unmatched']} unmatched)."
                    )
        report(
            f"Metadata scan complete: {stats['updated']} updated, "
            f"{stats['skipped']} already complete, {stats['unmatched']} unmatched."
        )
        return stats

    def _enrich(self, path: str, existing: dict) -> dict | None:
        if MediaProcessor.is_movie(path, existing):
            series_artwork = False
            result = self.helper.search_movie(_movie_query(path, existing))
            if not result:
                return None
            details = self.helper.get_movie_details(result["tmdb_id"]) or {}
            remote = {
                "type": "movie",
                "title": result.get("title"),
                "year": (result.get("release_date") or "")[:4],
                "plot": details.get("overview") or result.get("overview"),
                "genre": [item.get("name") for item in details.get("genres", []) if item.get("name")],
                "tmdb_id": result.get("tmdb_id"),
                "metadata_source": "tmdb",
            }
            art_url = result.get("backdrop_url") or result.get("poster_url")
        else:
            series_artwork = True
            identity = _episode_identity(path, existing)
            series = identity.get("series") or existing.get("show_title")
            if not series:
                parent = Path(path).parent
                if SEASON_DIR_RE.match(parent.name):
                    parent = parent.parent
                series = TitleParser.parse_title(parent.name)
            result = self.helper.search_tv(series)
            if not result:
                return None
            remote = {
                "type": "episode" if identity else "tvshow",
                "show_title": result.get("name"),
                "original_show_title": result.get("original_name"),
                "title": identity.get("title") or result.get("name"),
                "season": identity.get("season"),
                "episode": identity.get("episode"),
                "year": (result.get("first_air_date") or "")[:4],
                "plot": result.get("overview"),
                "genre": result.get("genre") or [],
                "tmdb_id": result.get("tmdb_id"),
                "metadata_source": "tmdb",
            }
            episode = None
            if identity.get("season") is not None and identity.get("episode") is not None:
                episode = self.helper.get_tv_episode(
                    result["tmdb_id"], int(identity["season"]), int(identity["episode"])
                )
            if episode:
                remote.update(
                    title=episode.get("name") or remote["title"],
                    plot=episode.get("overview") or remote["plot"],
                    aired=episode.get("air_date") or "",
                )
            art_url = result.get("backdrop_url") or result.get("poster_url")
        remote = {key: value for key, value in remote.items() if value not in (None, "", [])}
        # Local NFO data wins for episode-specific facts, while TMDB's en-US
        # show title intentionally wins so anime and foreign series display in
        # English throughout the guide.
        merged = {**remote, **existing}
        if remote.get("show_title"):
            merged["show_title"] = remote["show_title"]
            merged["original_show_title"] = remote.get("original_show_title", "")
        if art_url:
            artwork_file = self._download_artwork(
                f"series:{remote.get('show_title', path)}" if series_artwork else path,
                art_url,
            )
            if artwork_file:
                merged["artwork_file"] = artwork_file
                if series_artwork:
                    merged["series_artwork_file"] = artwork_file
        return merged

    def _download_artwork(self, media_path: str, url: str) -> str | None:
        name = hashlib.sha256(f"{media_path}\0{url}".encode()).hexdigest() + ".jpg"
        target = self.art_dir / name
        if target.is_file():
            return name
        temporary = target.with_suffix(".tmp")
        try:
            response = self.helper.session.get(url, timeout=15, stream=True)
            response.raise_for_status()
            size = 0
            with temporary.open("wb") as handle:
                for chunk in response.iter_content(64 * 1024):
                    size += len(chunk)
                    if size > 15 * 1024 * 1024:
                        raise ValueError("Artwork exceeds 15 MB")
                    handle.write(chunk)
            temporary.replace(target)
            return name
        except Exception as exc:
            LOG.warning("Could not cache artwork for %s: %s", media_path, exc)
            temporary.unlink(missing_ok=True)
            return None

    def ensure_local_artwork(self, media_path: str) -> str | None:
        """Return a persistent cached still, extracting it only when missing."""
        try:
            source = Path(media_path).resolve(strict=True)
            stat = source.stat()
        except (FileNotFoundError, OSError):
            return None
        signature = f"{source}\0video-still\0{stat.st_size}\0{stat.st_mtime_ns}"
        name = hashlib.sha256(signature.encode()).hexdigest() + ".jpg"
        self.art_dir.mkdir(parents=True, exist_ok=True)
        target = self.art_dir / name
        if target.is_file() and target.stat().st_size:
            return name

        with ARTWORK_LOCK:
            if target.is_file() and target.stat().st_size:
                return name
            temporary = self.art_dir / f"{target.stem}.tmp.jpg"
            for seek in ("10", "1"):
                try:
                    subprocess.run(
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error",
                            "-ss", seek, "-i", str(source), "-frames:v", "1",
                            "-vf", "scale=1280:-2", "-q:v", "3", "-y",
                            str(temporary),
                        ],
                        check=True,
                        capture_output=True,
                        timeout=30,
                    )
                    if temporary.is_file() and temporary.stat().st_size:
                        temporary.replace(target)
                        return name
                except (OSError, subprocess.SubprocessError):
                    temporary.unlink(missing_ok=True)
        LOG.warning("Could not extract cached artwork from %s", media_path)
        return None

    def ensure_series_artwork(self, series: str, media_path: str) -> str | None:
        """Cache one stable, spoiler-safe representative image per series."""
        name = hashlib.sha256(
            f"series-artwork\0{series.casefold().strip()}".encode()
        ).hexdigest() + ".jpg"
        self.art_dir.mkdir(parents=True, exist_ok=True)
        target = self.art_dir / name
        if target.is_file() and target.stat().st_size:
            return name
        local_name = self.ensure_local_artwork(media_path)
        if not local_name:
            return None
        with ARTWORK_LOCK:
            if not target.is_file():
                shutil.copyfile(self.art_dir / local_name, target)
        return name
