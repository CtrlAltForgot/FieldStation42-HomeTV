"""Stateful, broadcast-style premiere and rerun selection."""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass

from fs42.database import connect
from fs42.metadata_io import MetadataIO
from fs42.title_parser import TitleParser


NUMBER_RE = re.compile(r"0*(\d+)")
FILE_EPISODE_RE = re.compile(r"(?i)s(\d{1,3})[ ._-]*e(\d{1,3})([a-z]?)")
SEASON_DIR_RE = re.compile(r"(?i)^(?:season[ ._-]*|s)(\d+)$")


def _number(value):
    match = NUMBER_RE.match(str(value or ""))
    return int(match.group(1)) if match else None


def _series_from_path(path: str) -> str:
    parent = os.path.dirname(path)
    name = os.path.basename(parent)
    if SEASON_DIR_RE.match(name):
        name = os.path.basename(os.path.dirname(parent))
    return TitleParser.parse_title(name)


@dataclass(frozen=True)
class EpisodeIdentity:
    entry: object
    path: str
    series: str
    series_key: str
    season: int
    episode: int
    part: str = ""

    @property
    def order(self):
        return (self.season, self.episode, self.part.casefold(), self.path.casefold())


def episode_identity(entry) -> EpisodeIdentity | None:
    path = os.path.realpath(getattr(entry, "realpath", None) or entry.path)
    metadata = MetadataIO.read(path) or {}
    if str(metadata.get("type", "")).casefold() in {"movie", "film"}:
        return None
    match = FILE_EPISODE_RE.search(os.path.basename(path))
    season = _number(metadata.get("season"))
    episode = _number(metadata.get("episode"))
    part = ""
    if match:
        season = season if season is not None else int(match.group(1))
        episode = episode if episode is not None else int(match.group(2))
        part = match.group(3) or ""
    if season is None or episode is None:
        return None
    series = str(metadata.get("show_title") or _series_from_path(path)).strip()
    if not series:
        return None
    return EpisodeIdentity(
        entry=entry,
        path=path,
        series=series,
        series_key=re.sub(r"[^a-z0-9]+", "-", series.casefold()).strip("-"),
        season=season,
        episode=episode,
        part=part,
    )


class BroadcastHistory:
    """Persist reservations separately from programs whose airtime passed."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_table()

    def _init_table(self):
        with connect(self.db_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS broadcast_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    station TEXT NOT NULL,
                    slot_id TEXT NOT NULL,
                    occurrence_key TEXT NOT NULL,
                    series_key TEXT NOT NULL,
                    media_path TEXT NOT NULL,
                    season INTEGER,
                    episode INTEGER,
                    scheduled_start TIMESTAMP NOT NULL,
                    scheduled_end TIMESTAMP NOT NULL,
                    airing_kind TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'reserved',
                    aired_at TIMESTAMP,
                    part TEXT NOT NULL DEFAULT '',
                    UNIQUE(station, scheduled_start, media_path)
                )"""
            )
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(broadcast_history)"
                ).fetchall()
            }
            if "part" not in columns:
                connection.execute(
                    "ALTER TABLE broadcast_history "
                    "ADD COLUMN part TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_broadcast_history_series "
                "ON broadcast_history(station, series_key, status, scheduled_start)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_broadcast_history_slot "
                "ON broadcast_history(station, slot_id, occurrence_key)"
            )

    def reconcile(self, station: str, now: dt.datetime):
        with connect(self.db_path) as connection:
            connection.execute(
                """UPDATE broadcast_history
                   SET status='aired', aired_at=scheduled_end
                   WHERE station=? AND status='reserved' AND scheduled_end<=?""",
                (station, now),
            )

    def release_future(self, station: str, now: dt.datetime | None = None):
        now = now or dt.datetime.now()
        self.reconcile(station, now)
        with connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM broadcast_history "
                "WHERE station=? AND status='reserved' AND scheduled_start>=?",
                (station, now),
            )

    def rows(self, station: str, series_key: str):
        with connect(self.db_path) as connection:
            return connection.execute(
                """SELECT slot_id, occurrence_key, media_path, season, episode,
                          scheduled_start, scheduled_end, airing_kind, status,
                          aired_at, part
                   FROM broadcast_history
                   WHERE station=? AND series_key=?
                   ORDER BY scheduled_start""",
                (station, series_key),
            ).fetchall()

    def summaries(self, station: str) -> list[dict]:
        """Return compact operator-facing progress without exposing paths."""
        self.reconcile(station, dt.datetime.now())
        with connect(self.db_path) as connection:
            rows = connection.execute(
                """SELECT series_key,
                          SUM(CASE WHEN status='aired' THEN 1 ELSE 0 END),
                          SUM(CASE WHEN status='reserved' THEN 1 ELSE 0 END),
                          MAX(CASE WHEN status='aired' THEN scheduled_end END),
                          MIN(CASE WHEN status='reserved' THEN scheduled_start END)
                   FROM broadcast_history
                   WHERE station=?
                   GROUP BY series_key
                   ORDER BY series_key""",
                (station,),
            ).fetchall()
        return [
            {
                "series_key": row[0],
                "aired_count": row[1] or 0,
                "reserved_count": row[2] or 0,
                "last_aired": row[3],
                "next_reserved": row[4],
            }
            for row in rows
        ]

    def reserve(self, station: str, programming: dict, start: dt.datetime, end: dt.datetime):
        with connect(self.db_path) as connection:
            connection.execute(
                """INSERT OR IGNORE INTO broadcast_history
                   (station, slot_id, occurrence_key, series_key, media_path,
                    season, episode, scheduled_start, scheduled_end,
                    airing_kind, status, part)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'reserved', ?)""",
                (
                    station,
                    programming["slot_id"],
                    programming["occurrence_key"],
                    programming["series_key"],
                    programming["media_path"],
                    programming.get("season"),
                    programming.get("episode"),
                    start,
                    end,
                    programming["airing_kind"],
                    programming.get("part", ""),
                ),
            )


class BroadcastScheduler:
    """Select canonical premieres and previously aired reruns for one station."""

    MODES = {"premiere", "rerun", "mixed"}

    def __init__(self, station: str, db_path: str):
        self.station = station
        self.history = BroadcastHistory(db_path)
        self.pending: list[dict] = []
        self.history.reconcile(station, dt.datetime.now())

    @staticmethod
    def _occurrence_key(when: dt.datetime) -> str:
        iso_year, iso_week, _ = when.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    @staticmethod
    def _premiere_due(policy: dict, when: dt.datetime) -> bool:
        day = when.date()
        interval = max(1, int(policy.get("premiere_interval_weeks", 1)))
        try:
            anchor = dt.date.fromisoformat(
                str(policy.get("cadence_anchor_date", "1970-01-05"))
            )
        except ValueError:
            return False
        if ((day - anchor).days // 7) % interval:
            return False
        for key, comparison in (
            ("premiere_start_date", lambda value: day >= value),
            ("premiere_end_date", lambda value: day <= value),
        ):
            value = policy.get(key)
            if value:
                try:
                    if not comparison(dt.date.fromisoformat(str(value))):
                        return False
                except ValueError:
                    return False
        for hiatus in policy.get("hiatus_ranges", []):
            if not isinstance(hiatus, dict):
                continue
            try:
                start = dt.date.fromisoformat(str(hiatus["start"]))
                end = dt.date.fromisoformat(str(hiatus["end"]))
            except (KeyError, ValueError):
                continue
            if start <= day <= end:
                return False
        return True

    def select(self, slot: dict, tag: str, candidates: list, when: dt.datetime):
        policy = slot.get("programming")
        if not isinstance(policy, dict):
            return None
        mode = str(policy.get("mode", "mixed")).casefold()
        if mode not in self.MODES:
            raise ValueError(f"Unknown realistic programming mode: {mode}")
        identities = [identity for item in candidates if (identity := episode_identity(item))]
        requested_series = str(policy.get("series", "")).casefold().strip()
        if requested_series:
            identities = [
                item for item in identities
                if item.series.casefold() == requested_series
                or item.series_key == requested_series
            ]
        if not identities:
            return None
        identities.sort(key=lambda item: item.order)
        series_key = identities[0].series_key
        identities = [item for item in identities if item.series_key == series_key]
        slot_id = str(
            policy.get("slot_id")
            or f"{when.strftime('%A').casefold()}-{when.hour:02d}-{tag}-{series_key}"
        )
        occurrence = self._occurrence_key(when)
        rows = self.history.rows(self.station, series_key)
        all_rows = rows + [
            (
                item["slot_id"], item["occurrence_key"], item["media_path"],
                item.get("season"), item.get("episode"), item["scheduled_start"],
                item["scheduled_end"], item["airing_kind"], "reserved", None,
                item.get("part", ""),
            )
            for item in self.pending if item["series_key"] == series_key
        ]
        occurrence_has_premiere = any(
            row[0] == slot_id and row[1] == occurrence and row[7] == "premiere"
            for row in all_rows
        )
        def logical_key(season, episode, part, path):
            if season is not None and episode is not None:
                return (int(season), int(episode), str(part or "").casefold())
            return (None, None, os.path.realpath(path).casefold())

        used_premieres = {
            logical_key(row[3], row[4], row[10], row[2])
            for row in all_rows if row[7] in {"premiere", "legacy"}
        }

        selected = None
        kind = None
        if (
            mode in {"premiere", "mixed"}
            and not occurrence_has_premiere
            and self._premiere_due(policy, when)
        ):
            selected = next(
                (
                    item for item in identities
                    if logical_key(
                        item.season, item.episode, item.part, item.path
                    ) not in used_premieres
                ),
                None,
            )
            if selected and policy.get("catch_up_policy") == "wait_for_missing":
                numbered = [
                    (row[3], row[4]) for row in all_rows
                    if row[7] in {"premiere", "legacy"}
                    and row[3] is not None and row[4] is not None
                ]
                if numbered:
                    last_season, last_episode = max(numbered)
                    if (
                        selected.season == last_season
                        and selected.episode > last_episode + 1
                    ):
                        selected = None
            if selected:
                kind = "premiere"

        if selected is None:
            library_exhausted = all(
                logical_key(
                    item.season, item.episode, item.part, item.path
                ) in used_premieres
                for item in identities
            )
            end_policy = str(
                policy.get("library_end_policy", "reruns")
            ).casefold()
            if library_exhausted and mode in {"premiere", "mixed"}:
                if end_policy == "hold":
                    return None
                if end_policy == "restart_after_hiatus":
                    hiatus_weeks = max(
                        1, int(policy.get("restart_hiatus_weeks", 4))
                    )
                    premiere_ends = [
                        row[6] for row in all_rows if row[7] == "premiere"
                    ]
                    premiere_ends = [
                        dt.datetime.fromisoformat(value)
                        if isinstance(value, str) else value
                        for value in premiere_ends
                    ]
                    if premiere_ends and when < (
                        max(premiere_ends)
                        + dt.timedelta(weeks=hiatus_weeks)
                    ):
                        return None
            aired_keys = {
                logical_key(row[3], row[4], row[10], row[2])
                for row in all_rows
                if row[8] == "aired" and row[7] in {"premiere", "legacy"}
            }
            gap_days = max(0, int(policy.get("minimum_rerun_gap_days", 7)))
            cutoff = when - dt.timedelta(days=gap_days)
            last_air = {}
            for row in all_rows:
                if row[8] != "aired":
                    continue
                stamp = dt.datetime.fromisoformat(row[5]) if isinstance(row[5], str) else row[5]
                key = logical_key(row[3], row[4], row[10], row[2])
                last_air[key] = max(
                    stamp, last_air.get(key, dt.datetime.min)
                )
            eligible = [
                item for item in identities
                if logical_key(
                    item.season, item.episode, item.part, item.path
                ) in aired_keys
                and last_air.get(
                    logical_key(
                        item.season, item.episode, item.part, item.path
                    ),
                    dt.datetime.min,
                ) <= cutoff
            ]
            recent_seasons = policy.get("rerun_recent_seasons")
            if recent_seasons and eligible:
                newest = max(item.season for item in identities)
                earliest = newest - max(1, int(recent_seasons)) + 1
                eligible = [item for item in eligible if item.season >= earliest]
            if eligible:
                selected = min(
                    eligible,
                    key=lambda item: (
                        last_air.get(
                            logical_key(
                                item.season, item.episode,
                                item.part, item.path,
                            ),
                            dt.datetime.min,
                        ),
                        item.order,
                    ),
                )
                kind = "rerun"
        if selected is None:
            return None

        programming = {
            "slot_id": slot_id,
            "occurrence_key": occurrence,
            "series_key": series_key,
            "series": selected.series,
            "media_path": selected.path,
            "season": selected.season,
            "episode": selected.episode,
            "part": selected.part,
            "airing_kind": kind,
            "library_end_policy": policy.get("library_end_policy", "reruns"),
        }
        if kind == "premiere":
            programming["season_premiere"] = selected.episode == 1
            programming["season_finale"] = not any(
                item.series_key == selected.series_key
                and item.season == selected.season
                and item.order > selected.order
                for item in identities
            )
        return selected.entry, programming

    def stage(self, programming: dict, start: dt.datetime, end: dt.datetime):
        self.pending.append({**programming, "scheduled_start": start, "scheduled_end": end})

    def commit(self):
        for item in self.pending:
            self.history.reserve(
                self.station, item, item["scheduled_start"], item["scheduled_end"]
            )
        self.pending.clear()

    def sync_blocks(self, blocks):
        """Recover reservations and migrate already-aired legacy schedules."""
        now = dt.datetime.now()
        for block in blocks or []:
            programming = getattr(block, "programming", None)
            if programming:
                self.history.reserve(
                    self.station, programming, block.start_time, block.end_time
                )
                continue
            if block.end_time > now or isinstance(
                getattr(block, "content", None), list
            ):
                continue
            identity = episode_identity(getattr(block, "content", None)) \
                if getattr(block, "content", None) else None
            if not identity:
                continue
            self.history.reserve(
                self.station,
                {
                    "slot_id": "legacy",
                    "occurrence_key": self._occurrence_key(block.start_time),
                    "series_key": identity.series_key,
                    "media_path": identity.path,
                    "season": identity.season,
                    "episode": identity.episode,
                    "part": identity.part,
                    "airing_kind": "legacy",
                },
                block.start_time,
                block.end_time,
            )
        self.history.reconcile(self.station, now)
