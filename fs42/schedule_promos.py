"""Render restrained, schedule-derived channel promo bumpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import subprocess
from pathlib import Path

from fs42.channel_bumpers import ensure_channel_folders
from fs42.metadata_enrichment import MetadataEnricher, artwork_root


class SchedulePromoAgent:
    """Attach at most one cached promo ahead of each weekly premiere."""

    DEFAULT_DURATION = 6

    @staticmethod
    def _copy(programming: dict, when: dt.datetime) -> tuple[str, str]:
        series = programming.get("series") or "New episode"
        hour = when.strftime("%I:%M %p").lstrip("0")
        if programming.get("season_finale"):
            message = f"Season finale {when.strftime('%A')} at {hour} Central"
        elif programming.get("season_premiere"):
            message = f"New season premieres {when.strftime('%A')} at {hour} Central"
        else:
            message = f"New episode {when.strftime('%A')} at {hour} Central"
        return series, message

    @classmethod
    def _render(
        cls, station: str, programming: dict, when: dt.datetime,
        duration: int,
    ) -> Path | None:
        logger = logging.getLogger("schedule-promos")
        artwork_name = MetadataEnricher().ensure_series_artwork(
            programming.get("series") or programming["series_key"],
            programming["media_path"],
        )
        artwork = artwork_root() / artwork_name if artwork_name else None
        if not artwork or not artwork.is_file():
            return None
        title, message = cls._copy(programming, when)
        digest = hashlib.sha256(
            f"{station}|{title}|{message}|{duration}|{Path(artwork).stat().st_mtime_ns}".encode()
        ).hexdigest()[:18]
        folder = ensure_channel_folders(station)["promos"]
        output = folder / f"scheduled-{digest}.mp4"
        if output.is_file() and output.stat().st_size > 0:
            return output
        title_file = folder / f".{digest}-title.txt"
        message_file = folder / f".{digest}-message.txt"
        station_file = folder / f".{digest}-station.txt"
        title_file.write_text(title, encoding="utf-8")
        message_file.write_text(message, encoding="utf-8")
        station_file.write_text(station, encoding="utf-8")
        font = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        vf = (
            "scale=1344:756:force_original_aspect_ratio=increase,"
            "crop=1344:756,zoompan=z='min(zoom+0.00035,1.05)':"
            "d=1:s=1280x720:fps=30,"
            "drawbox=x=0:y=430:w=1280:h=290:color=black@0.62:t=fill,"
            f"drawtext=fontfile={font}:textfile={station_file}:"
            "fontcolor=0x65d7ff:fontsize=24:x=66:y=450,"
            f"drawtext=fontfile={font}:textfile={title_file}:"
            "fontcolor=white:fontsize=54:x=64:y=478,"
            f"drawtext=fontfile={font}:textfile={message_file}:"
            "fontcolor=0x65d7ff:fontsize=35:x=66:y=558"
        )
        temporary = output.with_suffix(".tmp.mp4")
        try:
            result = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-loop", "1", "-i", str(artwork),
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                    "-t", str(duration), "-vf", vf,
                    "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                    str(temporary),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode:
                logger.warning("Promo render failed: %s", result.stderr[-500:])
                return None
            os.replace(temporary, output)
            return output
        except (OSError, subprocess.SubprocessError) as error:
            logger.warning("Promo render failed: %s", error)
            return None
        finally:
            title_file.unlink(missing_ok=True)
            message_file.unlink(missing_ok=True)
            station_file.unlink(missing_ok=True)
            temporary.unlink(missing_ok=True)

    @classmethod
    def apply(cls, station_config: dict, blocks: list) -> int:
        config = station_config.get("schedule_promos", {})
        if config is False or (
            isinstance(config, dict) and not config.get("enabled", True)
        ):
            return 0
        config = config if isinstance(config, dict) else {}
        intensity = str(config.get("intensity", "normal")).casefold()
        thresholds = {"rare": 35, "normal": 70, "frequent": 100}
        threshold = thresholds.get(intensity, thresholds["normal"])
        duration = max(4, min(12, int(config.get("duration", cls.DEFAULT_DURATION))))
        min_spacing = dt.timedelta(
            hours=max(6, int(config.get("minimum_spacing_hours", 24)))
        )
        lead = dt.timedelta(hours=max(1, int(config.get("lead_hours", 72))))
        premieres = [
            block for block in blocks
            if (getattr(block, "programming", None) or {}).get(
                "airing_kind"
            ) == "premiere"
        ]
        last_promo = None
        added = 0
        for premiere in premieres:
            roll = int(hashlib.sha256(
                f"{station_config['network_name']}|{premiere.start_time.isoformat()}".encode()
            ).hexdigest()[:8], 16) % 100
            if roll >= threshold:
                continue
            eligible = [
                block for block in blocks
                if block.start_time < premiere.start_time
                and block.start_time >= premiere.start_time - lead
                and getattr(block, "content", None)
                and not isinstance(block.content, list)
                and not getattr(block, "start_bump", None)
                and block.buffer_duration() >= duration + 2
                and (last_promo is None or block.start_time - last_promo >= min_spacing)
            ]
            if not eligible:
                continue
            target = eligible[-1]
            promo = cls._render(
                station_config["network_name"], premiere.programming,
                premiere.start_time, duration,
            )
            if not promo:
                continue
            target.start_bump = {
                "path": str(promo), "duration": duration,
                "media_type": "video",
            }
            target.break_info["start_bump"] = target.start_bump
            target.break_info["_scheduled_promo"] = {
                "series": premiere.programming.get("series"),
                "premiere_at": premiere.start_time.isoformat(),
            }
            last_promo = target.start_time
            added += 1
        return added
