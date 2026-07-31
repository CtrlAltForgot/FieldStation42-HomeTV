"""Headless schedule resolution and browser HLS session management."""

from __future__ import annotations

import datetime as dt
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from fs42.catalog_api import CatalogAPI
from fs42.liquid_api import LiquidAPI
from fs42.station_manager import StationManager

LOG = logging.getLogger("HomeTV")
ASSET_RE = re.compile(r"^(?:master\.m3u8|stream\d+\.ts)$")


class WatchError(Exception):
    """A watch request cannot be fulfilled."""


class ChannelNotFound(WatchError):
    pass


class ProgramNotFound(WatchError):
    pass


class UnsafeMediaPath(WatchError):
    pass


@dataclass(frozen=True)
class Airing:
    channel_number: str
    channel_name: str
    program_title: str
    item_title: str
    start: dt.datetime
    end: dt.datetime
    item_start: dt.datetime
    item_end: dt.datetime
    media_path: str
    offset: float
    duration: float
    remaining: float
    content_type: str = "feature"
    identity_path: str | None = None

    def public_dict(self, now: dt.datetime) -> dict:
        elapsed = max(0.0, min((now - self.start).total_seconds(), self.duration))
        item_remaining = max(0.0, (self.item_end - now).total_seconds())
        return {
            "channel_number": self.channel_number,
            "channel_name": self.channel_name,
            "program_title": self.program_title,
            "item_title": self.item_title,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "item_end": self.item_end.isoformat(),
            "item_remaining": item_remaining,
            "duration": self.duration,
            "elapsed": elapsed,
            "progress": elapsed / self.duration if self.duration > 0 else 0,
            "playback_offset": self.offset,
            "server_time": now.isoformat(),
        }


def _local_now() -> dt.datetime:
    return dt.datetime.now()


class ScheduleResolver:
    """Resolve an opaque channel selection to its current scheduled media."""

    def __init__(self, manager: StationManager | None = None):
        self.manager = manager or StationManager()

    def channels(self) -> list[dict]:
        channels = []
        for station in self.manager.stations:
            if station.get("hidden") or not station.get("_has_schedule", False):
                continue
            channels.append(
                {
                    "channel_number": str(station["channel_number"]),
                    "channel_name": station["network_name"],
                }
            )
        return channels

    def station(self, channel: str):
        value = str(channel)
        for station in self.manager.stations:
            if (
                str(station.get("channel_number")) == value
                or station.get("network_name") == value
            ):
                if station.get("_has_schedule", False) and not station.get("hidden"):
                    return station
                break
        raise ChannelNotFound(f"Unknown scheduled channel: {channel}")

    def now(self, channel: str, when: dt.datetime | None = None) -> Airing:
        when = when or _local_now()
        station = self.station(channel)
        # Query a one-microsecond interval so SQLite selects only a covering row.
        blocks = LiquidAPI.get_blocks(
            station, when, when + dt.timedelta(microseconds=1)
        )
        block = next(
            (
                item
                for item in (blocks or [])
                if item.start_time <= when < item.end_time
            ),
            None,
        )
        if block is None:
            raise ProgramNotFound(
                f"No programming is scheduled now on {station['network_name']}"
            )

        cursor = block.start_time
        selected = None
        for item in block.plan:
            item_end = cursor + dt.timedelta(seconds=max(0, item.duration))
            if cursor <= when < item_end:
                selected = (item, cursor, item_end)
                break
            cursor = item_end
        if selected is None:
            raise ProgramNotFound("The current schedule block has no playable item")

        item, item_start, item_end = selected
        if item.is_stream:
            raise ProgramNotFound("Live URL schedule items are not supported by the MVP")
        media_path = self._approved_media_path(station, item.path)
        offset = max(0.0, float(item.skip) + (when - item_start).total_seconds())
        item_title = Path(item.path).stem
        identity_item = next(
            (
                plan_item
                for plan_item in block.plan
                if getattr(plan_item, "content_type", "feature") == "feature"
            ),
            item,
        )
        return Airing(
            channel_number=str(station["channel_number"]),
            channel_name=station["network_name"],
            program_title=getattr(block, "raw_title", block.title),
            item_title=item_title,
            start=block.start_time,
            end=block.end_time,
            item_start=item_start,
            item_end=item_end,
            media_path=media_path,
            offset=offset,
            duration=(block.end_time - block.start_time).total_seconds(),
            remaining=(item_end - when).total_seconds(),
            content_type=getattr(item, "content_type", "feature"),
            identity_path=getattr(identity_item, "path", item.path),
        )

    @staticmethod
    def _approved_media_path(station: dict, scheduled_path: str) -> str:
        requested = os.path.realpath(scheduled_path)
        for entry in CatalogAPI.get_entries(station) or []:
            candidates = [entry.path, getattr(entry, "realpath", None)]
            if any(path and os.path.realpath(path) == requested for path in candidates):
                if not os.path.isfile(requested):
                    raise ProgramNotFound("The scheduled media file is unavailable")
                return requested
        raise UnsafeMediaPath("Scheduled media is not present in the channel catalog")


@dataclass
class StreamSession:
    session_id: str
    channel: str
    profile: str
    directory: Path
    process: subprocess.Popen
    created_at: float
    last_access: float
    broadcast_id: str = ""


@dataclass
class ChannelBroadcast:
    broadcast_id: str
    key: tuple[str, str]
    directory: Path
    process: subprocess.Popen
    media_path: str
    item_end: dt.datetime
    created_at: float
    last_access: float
    leases: set[str]


class HLSSessionManager:
    PROFILES = {"auto", "copy"}

    def __init__(
        self,
        resolver: ScheduleResolver | None = None,
        root: str | Path | None = None,
        idle_seconds: int | None = None,
        max_sessions: int | None = None,
        process_factory: Callable[..., subprocess.Popen] = subprocess.Popen,
    ):
        conf = StationManager().server_conf
        self.resolver = resolver or ScheduleResolver()
        self.root = Path(
            root
            or os.environ.get("FS42_HLS_DIR")
            or conf.get("hls_dir", "runtime/hls")
        ).resolve()
        self.idle_seconds = int(
            idle_seconds
            or os.environ.get("FS42_HLS_IDLE_SECONDS")
            or conf.get("hls_idle_seconds", 30)
        )
        self.max_sessions = int(
            max_sessions
            or os.environ.get("FS42_HLS_MAX_SESSIONS")
            or conf.get("hls_max_sessions", 4)
        )
        self.process_factory = process_factory
        self.sessions: dict[str, StreamSession] = {}
        self.broadcasts: dict[tuple[str, str], ChannelBroadcast] = {}
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True)
        # A single production worker owns this cache. Remove only UUID-shaped
        # directories left behind by a previous unclean container shutdown.
        for child in self.root.iterdir():
            try:
                uuid.UUID(child.name)
            except (ValueError, AttributeError):
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)

    def create(
        self,
        channel: str,
        profile: str = "auto",
        boundary_at: dt.datetime | None = None,
    ) -> tuple[StreamSession, Airing]:
        if profile not in self.PROFILES:
            raise ValueError(f"Unknown client profile: {profile}")
        with self.lock:
            self.cleanup()
            airing = (
                self.resolver.now(channel, boundary_at)
                if boundary_at is not None
                else self.resolver.now(channel)
            )
            if boundary_at is not None and airing.content_type in {
                "commercial", "bump"
            }:
                try:
                    following = self.resolver.now(channel, airing.item_end)
                except WatchError:
                    following = None
                if following is not None and following.content_type == "feature":
                    wall_remaining = max(
                        0.1, (airing.item_end - _local_now()).total_seconds()
                    )
                    airing = replace(
                        airing,
                        remaining=min(airing.remaining, wall_remaining),
                    )
            key = (airing.channel_number, profile)
            broadcast = self.broadcasts.get(key)
            if broadcast is not None and (
                broadcast.process.poll() is not None
                or broadcast.media_path != airing.media_path
                or broadcast.item_end != airing.item_end
            ):
                self._remove_broadcast(key)
                broadcast = None

            if broadcast is None:
                if len(self.broadcasts) >= self.max_sessions:
                    self._evict_unused_broadcast()
                if len(self.broadcasts) >= self.max_sessions:
                    raise WatchError("The server has reached its channel broadcast limit")
                broadcast = self._start_broadcast(key, airing, profile)

            session_id = str(uuid.uuid4())
            timestamp = time.monotonic()
            session = StreamSession(
                session_id,
                airing.channel_number,
                profile,
                broadcast.directory,
                broadcast.process,
                broadcast.created_at,
                timestamp,
                broadcast.broadcast_id,
            )
            self.sessions[session_id] = session
            broadcast.leases.add(session_id)
            broadcast.last_access = timestamp
            LOG.info(
                "Attached viewer %s to channel broadcast %s (%s viewers)",
                session_id,
                broadcast.broadcast_id,
                len(broadcast.leases),
            )
            return session, airing

    def _start_broadcast(
        self, key: tuple[str, str], airing: Airing, profile: str
    ) -> ChannelBroadcast:
        broadcast_id = str(uuid.uuid4())
        directory = self.root / broadcast_id
        directory.mkdir(mode=0o700)
        streams = self._probe_streams(airing.media_path) if profile == "auto" else []
        subtitle = self._select_english_subtitle(streams)
        command = self._ffmpeg_command(
            airing, profile, directory, subtitle, streams
        )
        LOG.info(
            "Starting shared HLS broadcast %s for channel %s at %.3fs (%s)",
            broadcast_id,
            airing.channel_number,
            airing.offset,
            profile,
        )
        try:
            process = self.process_factory(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=None,
                start_new_session=True,
            )
        except Exception:
            shutil.rmtree(directory, ignore_errors=True)
            raise
        timestamp = time.monotonic()
        broadcast = ChannelBroadcast(
            broadcast_id,
            key,
            directory,
            process,
            airing.media_path,
            airing.item_end,
            timestamp,
            timestamp,
            set(),
        )
        self.broadcasts[key] = broadcast
        return broadcast

    @staticmethod
    def _probe_streams(media_path: str) -> list[dict]:
        try:
            result = subprocess.run(
                [
                    os.environ.get("FS42_FFPROBE", "ffprobe"),
                    "-v",
                    "error",
                    "-show_entries",
                    (
                        "stream=codec_type,codec_name:"
                        "stream_tags=language,title:"
                        "stream_disposition=default,forced"
                    ),
                    "-of",
                    "json",
                    media_path,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            streams = __import__("json").loads(result.stdout).get("streams", [])
        except Exception as exc:
            LOG.warning("Could not inspect media streams for %s: %s", media_path, exc)
            return []
        return streams

    @staticmethod
    def _select_english_subtitle(streams: list[dict]) -> tuple[str, int] | None:
        """Select an English subtitle only for explicitly non-English audio."""
        audio = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        audio_language = (
            (audio or {}).get("tags", {}).get("language", "und").casefold()
        )
        if audio_language in {"eng", "en", "und", ""}:
            return None

        subtitles = [
            stream for stream in streams if stream.get("codec_type") == "subtitle"
        ]
        candidates = []
        for index, stream in enumerate(subtitles):
            language = stream.get("tags", {}).get("language", "").casefold()
            if language in {"eng", "en"}:
                title = stream.get("tags", {}).get("title", "").casefold()
                disposition = stream.get("disposition", {})
                score = 10 if disposition.get("default") else 0
                if any(word in title for word in ("full", "dialogue", "dialog")):
                    score += 20
                if disposition.get("forced") or any(
                    word in title for word in ("sign", "song", "forced")
                ):
                    score -= 100
                candidates.append(
                    (score, stream.get("codec_name", ""), index)
                )
        if candidates:
            _score, codec, index = max(candidates, key=lambda item: item[0])
            return codec, index
        return None

    @staticmethod
    def _english_subtitle(media_path: str) -> tuple[str, int] | None:
        return HLSSessionManager._select_english_subtitle(
            HLSSessionManager._probe_streams(media_path)
        )

    @staticmethod
    def _transcode_threads() -> int:
        try:
            threads = int(os.environ.get("FS42_HLS_TRANSCODE_THREADS", "2"))
        except ValueError as exc:
            raise ValueError("FS42_HLS_TRANSCODE_THREADS must be an integer") from exc
        if not 1 <= threads <= 8:
            raise ValueError("FS42_HLS_TRANSCODE_THREADS must be between 1 and 8")
        return threads

    @staticmethod
    def _ffmpeg_command(
        airing: Airing,
        profile: str,
        directory: Path,
        subtitle: tuple[str, int] | None = None,
        streams: list[dict] | None = None,
    ) -> list[str]:
        command = [
            os.environ.get("FS42_FFMPEG", "ffmpeg"),
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-fflags",
            "+genpts+discardcorrupt",
            "-ss",
            f"{airing.offset:.3f}",
            "-re",
            "-i",
            airing.media_path,
            "-t",
            f"{max(0.1, airing.remaining):.3f}",
        ]
        if profile == "copy":
            command += ["-c", "copy"]
        else:
            streams = streams or []
            video = next(
                (stream for stream in streams if stream.get("codec_type") == "video"),
                {},
            )
            audio = next(
                (stream for stream in streams if stream.get("codec_type") == "audio"),
                {},
            )
            # The auto profile always normalizes timestamps, GOP cadence, and
            # codecs. Stream-copying superficially compatible sources leaves
            # arbitrary keyframes and timestamps that make live HLS unstable.
            copy_video = False
            copy_audio = False
            video_map = "0:v:0"
            video_filter = []
            if subtitle:
                codec, subtitle_index = subtitle
                if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle"}:
                    command += [
                        "-filter_complex",
                        f"[0:v:0][0:s:{subtitle_index}]overlay[v]",
                    ]
                    video_map = "[v]"
                else:
                    escaped_path = (
                        airing.media_path.replace("\\", "\\\\")
                        .replace(":", "\\:")
                        .replace("'", "\\'")
                    )
                    video_filter = [
                        "-vf",
                        (
                            f"setpts=PTS+{airing.offset:.3f}/TB,"
                            f"subtitles='{escaped_path}':si={subtitle_index},"
                            "setpts=PTS-STARTPTS"
                        ),
                    ]
            command += ["-map", video_map, "-map", "0:a:0?", *video_filter]
            if copy_video:
                command += ["-c:v", "copy"]
            else:
                encoder = os.environ.get(
                    "FS42_HLS_VIDEO_ENCODER", "libx264"
                ).casefold()
                if encoder == "h264_nvenc":
                    command += [
                        "-c:v", "h264_nvenc",
                        "-preset", "p4",
                        "-tune", "ll",
                        "-rc", "vbr",
                        "-cq", "21",
                        "-b:v", "5M",
                        "-maxrate", "8M",
                        "-bufsize", "10M",
                        "-g", "60",
                        "-no-scenecut", "1",
                    ]
                elif encoder == "libx264":
                    command += [
                        "-c:v", "libx264",
                        "-preset", "ultrafast",
                        "-tune", "zerolatency",
                        "-threads:v",
                        str(HLSSessionManager._transcode_threads()),
                        "-sc_threshold", "0",
                    ]
                else:
                    raise ValueError(
                        "FS42_HLS_VIDEO_ENCODER must be libx264 or h264_nvenc"
                    )
                command += [
                    "-pix_fmt", "yuv420p",
                    "-force_key_frames", "expr:gte(t,n_forced*1)",
                ]
            if copy_audio:
                command += ["-c:a", "copy"]
            else:
                command += [
                    "-af", "aresample=async=1:first_pts=0",
                    "-c:a", "aac",
                    "-profile:a", "aac_low",
                    "-b:a", "160k",
                    "-ar", "48000",
                    "-ac", "2",
                ]
        command += [
            "-avoid_negative_ts",
            "make_zero",
            "-f",
            "hls",
            "-hls_time",
            "1",
            "-hls_list_size",
            "60",
            "-hls_delete_threshold",
            "10",
            "-hls_flags",
            "delete_segments+independent_segments+temp_file",
            "-hls_segment_filename",
            str(directory / "stream%05d.ts"),
            str(directory / "master.m3u8"),
        ]
        return command

    def get(self, session_id: str) -> StreamSession:
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                raise KeyError(session_id)
            session.last_access = time.monotonic()
            broadcast = self.broadcasts.get((session.channel, session.profile))
            if broadcast is not None:
                broadcast.last_access = session.last_access
            return session

    def asset(self, session_id: str, asset: str) -> Path:
        if not ASSET_RE.fullmatch(asset):
            raise ValueError("Invalid HLS asset")
        session = self.get(session_id)
        target = (session.directory / asset).resolve()
        if target.parent != session.directory.resolve():
            raise ValueError("Invalid HLS asset")
        return target

    def delete(self, session_id: str) -> bool:
        with self.lock:
            session = self.sessions.pop(session_id, None)
            if session is None:
                return False
            key = (session.channel, session.profile)
            broadcast = self.broadcasts.get(key)
            if broadcast is not None:
                broadcast.leases.discard(session_id)
                broadcast.last_access = time.monotonic()
                if broadcast.process.poll() is not None:
                    self._remove_broadcast(key)
            return True

    def fail(self, session_id: str) -> bool:
        """Remove a failed viewer lease and its unusable broadcaster."""
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return False
            self._remove_broadcast((session.channel, session.profile))
            return True

    def ready(self, session_id: str, minimum_segments: int = 3) -> bool:
        session = self.get(session_id)
        if session.process.poll() is not None:
            return False
        playlist = session.directory / "master.m3u8"
        if not playlist.is_file():
            return False
        return sum(1 for _ in session.directory.glob("stream*.ts")) >= minimum_segments

    def status(self) -> dict:
        with self.lock:
            now = time.monotonic()
            return {
                "broadcasts": [
                    {
                        "channel": broadcast.key[0],
                        "profile": broadcast.key[1],
                        "viewers": len(broadcast.leases),
                        "running": broadcast.process.poll() is None,
                        "segments": sum(
                            1 for _ in broadcast.directory.glob("stream*.ts")
                        ),
                        "age_seconds": round(now - broadcast.created_at, 1),
                    }
                    for broadcast in self.broadcasts.values()
                ]
            }

    def cleanup(self) -> None:
        cutoff = time.monotonic() - self.idle_seconds
        with self.lock:
            expired_leases = [
                key
                for key, session in self.sessions.items()
                if session.last_access < cutoff
            ]
            for session_id in expired_leases:
                session = self.sessions.pop(session_id)
                broadcast = self.broadcasts.get((session.channel, session.profile))
                if broadcast is not None:
                    broadcast.leases.discard(session_id)
            expired_broadcasts = [
                key
                for key, broadcast in self.broadcasts.items()
                if broadcast.process.poll() is not None
                or (not broadcast.leases and broadcast.last_access < cutoff)
            ]
            for key in expired_broadcasts:
                self._remove_broadcast(key)

    def close(self) -> None:
        with self.lock:
            self.sessions.clear()
            for key in list(self.broadcasts):
                self._remove_broadcast(key)

    def _evict_unused_broadcast(self) -> bool:
        """Free the least-recently-used channel that has no attached viewers."""
        candidates = [
            broadcast
            for broadcast in self.broadcasts.values()
            if not broadcast.leases
        ]
        if not candidates:
            return False
        victim = min(candidates, key=lambda broadcast: broadcast.last_access)
        LOG.info(
            "Evicting idle channel broadcast %s for channel %s",
            victim.broadcast_id,
            victim.key[0],
        )
        self._remove_broadcast(victim.key)
        return True

    def _remove_broadcast(self, key: tuple[str, str]) -> None:
        broadcast = self.broadcasts.pop(key, None)
        if broadcast is None:
            return
        for session_id in list(broadcast.leases):
            self.sessions.pop(session_id, None)
        self._stop(broadcast)

    @staticmethod
    def _stop(session: StreamSession) -> None:
        process = session.process
        if process.poll() is None:
            HLSSessionManager._signal_process(process, signal.SIGTERM)
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                HLSSessionManager._signal_process(process, signal.SIGKILL)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    LOG.error(
                        "FFmpeg process %s did not exit after SIGKILL",
                        getattr(process, "pid", "unknown"),
                    )
        # FFmpeg is its process-group leader. Kill any helper processes that
        # survived after the leader exited before removing the session.
        HLSSessionManager._signal_process(process, signal.SIGKILL, fallback=False)
        shutil.rmtree(session.directory, ignore_errors=True)

    @staticmethod
    def _signal_process(
        process: subprocess.Popen, sig: signal.Signals, fallback: bool = True
    ) -> None:
        pid = getattr(process, "pid", None)
        if pid is not None and hasattr(os, "killpg"):
            try:
                os.killpg(pid, sig)
                return
            except ProcessLookupError:
                return
            except OSError as exc:
                LOG.warning("Could not signal FFmpeg process group %s: %s", pid, exc)
        if not fallback or process.poll() is not None:
            return
        if sig == signal.SIGKILL:
            process.kill()
        else:
            process.terminate()
