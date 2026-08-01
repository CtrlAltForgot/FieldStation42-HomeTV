import datetime as dt
import asyncio
import json
import os
import re
import tempfile
import time
import unittest
from zoneinfo import ZoneInfo
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from fs42.block_plan import BlockPlanEntry
from fs42.database import connect
from fs42.hometv import (
    Airing,
    HLSSessionManager,
    ScheduleResolver,
    UnsafeMediaPath,
)
from fs42.media_processor import MediaProcessor
from fs42.metadata_enrichment import MetadataEnricher, _episode_identity
from fs42.tvmaze_helper import TVmazeHelper
from fs42.broadcast_scheduler import BroadcastScheduler
from fs42.schedule_promos import SchedulePromoAgent
from fs42.subtitle_provider import SubtitleProvider, opensubtitles_hash
from fs42.channel_bumpers import active_seasons, ensure_channel_folders
from fs42.fs42_server.api.watch import (
    PLAYLIST_STARTUP_ATTEMPTS,
    PLAYLIST_STARTUP_INTERVAL,
    PLAYLIST_READY_SEGMENTS,
    SessionRequest,
    _now_payload,
    artwork as artwork_endpoint,
    channels as channel_endpoint,
    create_session as create_session_endpoint,
    prewarm as prewarm_endpoint,
)
from fs42.fs42_server.api import build as build_api
from fs42.fs42_server.api import settings as settings_api
from fs42.fs42_server.api.tv import (
    _compact_tv_program,
    guide_card_rect,
    roku_card_rect,
)
from fs42.fs42_server.api.schedules import (
    _attach_meta,
    _episode_display,
    _movie_display,
    program_display,
)


class FakeManager:
    def __init__(self):
        self.stations = [{
            "channel_number": 42,
            "network_name": "Test TV",
            "_has_schedule": True,
            "_has_catalog": True,
            "hidden": False,
        }]


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class ResolverTests(unittest.TestCase):
    def test_current_plan_item_and_offset_include_scheduler_skip(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as media:
            start = dt.datetime(2026, 7, 30, 12, 0)
            block = SimpleNamespace(
                start_time=start,
                end_time=start + dt.timedelta(minutes=10),
                title="Lunch Show",
                plan=[
                    BlockPlanEntry(media.name, skip=5, duration=120),
                    BlockPlanEntry(media.name, skip=30, duration=480),
                ],
            )
            entry = SimpleNamespace(path=media.name, realpath=os.path.realpath(media.name))
            with (
                patch("fs42.hometv.LiquidAPI.get_blocks", return_value=[block]),
                patch("fs42.hometv.CatalogAPI.get_entries", return_value=[entry]),
            ):
                airing = ScheduleResolver(FakeManager()).now(
                    "42", start + dt.timedelta(seconds=150)
                )
            self.assertEqual(airing.program_title, "Lunch Show")
            self.assertEqual(airing.offset, 60)
            self.assertEqual(airing.remaining, 450)
            self.assertEqual(airing.item_start, start + dt.timedelta(seconds=120))
            self.assertEqual(
                airing.public_dict(start + dt.timedelta(seconds=150))["item_end"],
                (start + dt.timedelta(seconds=600)).isoformat(),
            )
            self.assertEqual(
                airing.public_dict(
                    start + dt.timedelta(seconds=150)
                )["item_remaining"],
                450,
            )

    def test_channel_name_and_number_select_same_station(self):
        resolver = ScheduleResolver(FakeManager())
        self.assertEqual(resolver.station("42")["network_name"], "Test TV")
        self.assertEqual(resolver.station("Test TV")["channel_number"], 42)

    def test_episode_display_uses_series_prefix_without_episode_code(self):
        display = _episode_display(
            "/media/SpongeBob SquarePants/Season 04/"
            "SpongeBob SquarePants S04E15ab Squidtastic Voyage.mp4"
        )
        self.assertEqual(display["display_title"], "SpongeBob SquarePants")
        self.assertEqual(display["episode_title"], "Squidtastic Voyage")
        self.assertEqual(display["season"], 4)
        self.assertEqual(display["episode"], 15)

    def test_episode_display_uses_show_directory_when_filename_starts_with_code(self):
        path = (
            "/media/King of the Hill (1997)/Season 01/"
            "S01E10 Keeping Up With Our Joneses.mkv"
        )
        display = _episode_display(path)
        self.assertEqual(display["display_title"], "King of the Hill")
        self.assertEqual(display["episode_title"], "Keeping Up with Our Joneses")
        self.assertEqual(
            program_display(path)["program_details"],
            "S1E10: Keeping Up with Our Joneses",
        )

    def test_series_prefixed_x_episode_is_identified_everywhere(self):
        path = "/media/Forensic Files/Season 07/Forensic Files 07x36 All Charged Up.mkv"
        display = program_display(path)
        identity = _episode_identity(path, {})
        self.assertEqual(display["display_title"], "Forensic Files")
        self.assertEqual(display["program_details"], "S7E36: All Charged Up")
        self.assertEqual(identity["series"], "Forensic Files")
        self.assertEqual((identity["season"], identity["episode"]), (7, 36))

    def test_verbose_season_episode_name_is_identified(self):
        path = "/media/Example/Season 02/Example Season 2 Episode 11 The Return.mkv"
        identity = _episode_identity(path, {})
        self.assertEqual(identity["series"], "Example")
        self.assertEqual((identity["season"], identity["episode"]), (2, 11))

    def test_bracketed_episode_code_is_identified(self):
        path = "/media/Example/Season 03/Example [S03E09] The Answer.mkv"
        display = program_display(path)
        identity = _episode_identity(path, {})
        self.assertEqual(display["display_title"], "Example")
        self.assertEqual(display["program_details"], "S3E9: The Answer")
        self.assertEqual((identity["season"], identity["episode"]), (3, 9))

    def test_guide_display_metadata_does_not_read_file_metadata(self):
        block = SimpleNamespace(
            content=SimpleNamespace(
                path="/media/King of the Hill/Season 01/S01E10 Episode.mkv"
            )
        )
        with patch("fs42.fs42_server.api.schedules.MetadataIO.read") as read:
            _attach_meta([block], read_meta=False)
        read.assert_not_called()
        self.assertEqual(block.display_title, "King of the Hill")

    def test_guide_display_prefers_complete_raw_schedule_title(self):
        block = SimpleNamespace(
            title="Channel",
            raw_title="Channel 42 Live Fixture",
            content=SimpleNamespace(path="/media/channel-42.mp4"),
        )
        _attach_meta([block], read_meta=False)
        self.assertEqual(block.display_title, "Channel 42 Live Fixture")

    def test_movie_release_name_keeps_only_title_and_year(self):
        display = _movie_display(
            "/media/Movies/War Dogs 2016 2160p Hybrid UHD BluRay Remux DV HDR.mkv"
        )
        self.assertEqual(display["display_title"], "War Dogs (2016)")

    def test_dotted_movie_release_keeps_complete_title(self):
        display = _movie_display(
            "/media/Movies/War.Dogs.2016.2160p.BluRay.mkv"
        )
        self.assertEqual(display["display_title"], "War Dogs (2016)")

    def test_star_wars_movie_uses_canonical_episode_punctuation(self):
        display = _movie_display(
            "/media/Movies/Star Wars Episode Vii The Force Awakens (2015).mkv"
        )
        self.assertEqual(
            display["display_title"],
            "Star Wars: Episode VII - The Force Awakens (2015)",
        )

    def test_filename_repairs_truncated_series_metadata(self):
        display = program_display(
            "/media/Game of Thrones/Season 01/"
            "Game of Thrones S01E01 Winter Is Coming.mkv",
            meta={"type": "episode", "show_title": "Game of", "title": "Winter Is Coming"},
        )
        self.assertEqual(display["display_title"], "Game of Thrones")

    def test_known_series_path_repairs_truncated_schedule_fallback(self):
        display = program_display(
            "/media/Realm/Game of Thrones Collection/episode-01.mkv",
            "Game of",
        )
        self.assertEqual(display["display_title"], "Game of Thrones")

    def test_known_series_alias_repairs_truncated_under_the_dome_fallback(self):
        display = program_display("/media/realm-playlist", "Under the")
        self.assertEqual(display["display_title"], "Under the Dome")

    def test_truncated_multi_item_schedule_title_is_canonicalized(self):
        display = program_display("/media/realm-playlist", "Game of")
        self.assertEqual(display["display_title"], "Game of Thrones")

    def test_el_camino_movie_uses_official_colon(self):
        display = _movie_display(
            "/media/Movies/El Camino a Breaking Bad Movie (2019).mkv"
        )
        self.assertEqual(
            display["display_title"],
            "El Camino: a Breaking Bad Movie (2019)",
        )

    def test_futurama_movie_removes_order_prefix_and_restores_punctuation(self):
        display = _movie_display(
            "/media/Movies/Movie 3 Futurama Benders Game (2008).mkv"
        )
        self.assertEqual(
            display["display_title"],
            "Futurama: Bender's Game (2008)",
        )

    def test_super_mario_movie_restores_abbreviation_period(self):
        display = _movie_display(
            "/media/Movies/The Super Mario Bros Movie (2023).mkv"
        )
        self.assertEqual(
            display["display_title"],
            "The Super Mario Bros. Movie (2023)",
        )

    def test_movie_drops_incorrect_episode_metadata(self):
        display = program_display(
            "/media/Movies/The SpongeBob SquarePants Movie.mkv",
            meta={
                "type": "episode",
                "show_title": "The SpongeBob SquarePants Movie",
                "season": 1,
                "episode": 1,
                "title": "Ac3",
            },
        )
        self.assertEqual(display["display_title"], "The SpongeBob SquarePants Movie")
        self.assertEqual(display["program_details"], "")
        self.assertNotIn("season", display)
        self.assertNotIn("episode", display)

    def test_clean_program_number_is_not_treated_as_episode_number(self):
        display = program_display(
            "/media/channel-42.mp4", "Channel 42 Live Fixture"
        )
        self.assertEqual(display["display_title"], "Channel 42 Live Fixture")

    def test_guide_removes_cast_note_from_series_title(self):
        display = program_display(
            "/media/TV/The Big Bang Theory/file.mkv",
            "The Big Bang Theory (kaley Cuoco)",
        )
        self.assertEqual(display["display_title"], "The Big Bang Theory")

    def test_guide_removes_parenthesized_release_metadata(self):
        display = program_display(
            "/media/TV/El Ministerio Del Tiempo/file.mkv",
            "El Ministerio Del Tiempo (1080p Web Dl X265 10bit Vertag)",
        )
        self.assertEqual(display["display_title"], "El Ministerio Del Tiempo")

    def test_guide_preserves_release_year_parenthetical(self):
        self.assertEqual(
            program_display("/media/file.mkv", "Dark (2017)")["display_title"],
            "Dark (2017)",
        )

    def test_movie_extra_uses_parent_movie_identity(self):
        display = program_display(
            "/media/Movies/War Dogs (2016)/Featurettes/"
            "'On Location Napoleon Dynamite' Documentary.mkv"
        )
        self.assertEqual(display["display_title"], "War Dogs (2016)")

    def test_tv_extra_uses_parent_series_identity(self):
        display = program_display(
            "/media/King of the Hill (1997)/Deleted and Extended Scenes/"
            "Deleted Scene.mkv"
        )
        self.assertEqual(display["display_title"], "King of the Hill")

    def test_guide_uses_known_english_series_alias(self):
        block = SimpleNamespace(
            title="Shingeki No Kyojin The Final Season",
            content=SimpleNamespace(path="/media/anime/Shingeki No Kyojin.mkv"),
        )
        _attach_meta([block], read_meta=False)
        self.assertEqual(block.display_title, "Attack on Titan")

    def test_episode_uses_known_english_series_alias(self):
        display = _episode_display(
            "/media/Shingeki No Kyojin/Season 04/"
            "Shingeki No Kyojin S04E01 The Other Side.mkv"
        )
        self.assertEqual(display["display_title"], "Attack on Titan")

    def test_leading_x_episode_code_uses_parent_series(self):
        display = program_display(
            "/media/Chowder/Season 02/02x11 the Dinner Theater.mkv"
        )
        self.assertEqual(display["display_title"], "Chowder")
        self.assertEqual(
            display["program_details"],
            "S2E11: The Dinner Theater",
        )

    def test_split_episode_code_is_normalized_for_viewers(self):
        display = program_display(
            "/media/SpongeBob SquarePants/Season 06/"
            "SpongeBob SquarePants S06E05A The Splinter.mkv"
        )
        self.assertEqual(display["episode"], 5)
        self.assertEqual(
            display["program_details"],
            "S6E5: The Splinter",
        )

    def test_episode_metadata_removes_parenthesized_resolution(self):
        display = program_display(
            "/media/SpongeBob SquarePants/Season 06/"
            "SpongeBob SquarePants S06E05A The Splinter.mkv",
            meta={
                "type": "episode",
                "show_title": "SpongeBob SquarePants",
                "season": 6,
                "episode": "05A",
                "title": "The Splinter (1080p)",
            },
        )
        self.assertEqual(
            display["program_details"],
            "S6E5: The Splinter",
        )

    def test_episode_metadata_removes_unclosed_resolution_suffix(self):
        display = program_display(
            "/media/SpongeBob/SpongeBob S06E05A The Splinter.mkv",
            meta={
                "type": "episode",
                "show_title": "SpongeBob SquarePants",
                "season": 6,
                "episode": "05A",
                "title": "The Splinter (1080p",
            },
        )
        self.assertEqual(
            display["program_details"],
            "S6E5: The Splinter",
        )

    def test_numbered_short_uses_directory_and_episode_title(self):
        display = program_display(
            "/media/Schoolhouse Rock/"
            "Schoolhouse Rock Multiplication Rock 08 Figure Eight.mkv"
        )
        self.assertEqual(display["display_title"], "Schoolhouse Rock")
        self.assertEqual(display["episode_title"], "Figure Eight")
        self.assertEqual(display["episode"], 8)
        self.assertEqual(display["program_details"], "E8: Figure Eight")

    def test_numbered_short_in_shared_channel_folder_uses_known_series(self):
        display = program_display(
            "/media/Toon Mix/"
            "Schoolhouse Rock Multiplication Rock 08 Figure Eight.mkv"
        )
        self.assertEqual(display["display_title"], "Schoolhouse Rock")
        self.assertEqual(display["episode_title"], "Figure Eight")
        self.assertEqual(display["program_details"], "E8: Figure Eight")

    def test_recursive_scan_ignores_movie_auxiliary_directories(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            feature = root / "War Dogs (2016)" / "War Dogs (2016).mkv"
            extra = root / "War Dogs (2016)" / "Featurettes" / "Documentary.mkv"
            feature.parent.mkdir()
            extra.parent.mkdir()
            feature.touch()
            extra.touch()
            self.assertEqual(MediaProcessor._rfind_media(temp_dir), [str(feature)])

    def test_recursive_scan_ignores_deleted_and_extended_scenes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            episode = root / "King of the Hill" / "S01E10 Episode.mkv"
            extra = (
                root
                / "King of the Hill"
                / "Deleted and Extended Scenes"
                / "Deleted Scene.mkv"
            )
            episode.parent.mkdir()
            extra.parent.mkdir()
            episode.touch()
            extra.touch()
            self.assertEqual(MediaProcessor._rfind_media(temp_dir), [str(episode)])

    def test_recursive_scan_honors_fs42ignore_marker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            included = root / "Included" / "episode.mkv"
            excluded = root / "El Ministerio Del Tiempo" / "episode.mkv"
            included.parent.mkdir()
            excluded.parent.mkdir()
            included.touch()
            excluded.touch()
            (excluded.parent / ".fs42ignore").touch()
            self.assertEqual(MediaProcessor._rfind_media(temp_dir), [str(included)])

    def test_schedule_path_must_exist_in_catalog(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as media:
            with patch("fs42.hometv.CatalogAPI.get_entries", return_value=[]):
                with self.assertRaises(UnsafeMediaPath):
                    ScheduleResolver._approved_media_path(
                        FakeManager().stations[0], media.name
                    )


class SessionTests(unittest.TestCase):
    def test_channel_tuning_starts_from_first_complete_segment(self):
        self.assertEqual(PLAYLIST_READY_SEGMENTS, 1)

    def test_playlist_startup_allows_slow_transcodes(self):
        self.assertGreaterEqual(
            PLAYLIST_STARTUP_ATTEMPTS * PLAYLIST_STARTUP_INTERVAL,
            20,
        )

    def test_default_session_limit_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(
                "os.environ",
                {
                    "FS42_HLS_DIR": temp_dir,
                    "FS42_HLS_MAX_SESSIONS": "",
                },
            ):
                manager = HLSSessionManager(resolver=SimpleNamespace())
        self.assertEqual(manager.max_sessions, 4)

    def test_session_command_and_cleanup(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            when = dt.datetime.now()
            airing = Airing(
                "42", "Test TV", "Show", "Episode", when,
                when + dt.timedelta(minutes=30), when,
                when + dt.timedelta(minutes=30), str(media), 91.25, 1800, 1708.75,
            )
            resolver = SimpleNamespace(now=lambda channel: airing)
            processes = []

            def factory(command, **_kwargs):
                process = FakeProcess(command)
                processes.append(process)
                return process

            manager = HLSSessionManager(
                resolver=resolver,
                root=Path(temp_dir) / "hls",
                process_factory=factory,
            )
            session, _ = manager.create("42")
            self.assertIn("-ss", processes[0].command)
            self.assertIn("-re", processes[0].command)
            self.assertIn("-t", processes[0].command)
            self.assertIn("-force_key_frames", processes[0].command)
            hls_flags = processes[0].command[
                processes[0].command.index("-hls_flags") + 1
            ]
            self.assertNotIn("omit_endlist", hls_flags)
            self.assertEqual(
                processes[0].command[
                    processes[0].command.index("-hls_list_size") + 1
                ],
                "60",
            )
            self.assertEqual(
                processes[0].command[
                    processes[0].command.index("-hls_time") + 1
                ],
                "1",
            )
            self.assertEqual(
                processes[0].command[processes[0].command.index("-ss") + 1],
                "91.250",
            )
            self.assertTrue(session.directory.is_dir())
            self.assertTrue(manager.delete(session.session_id))
            # Releasing a viewer keeps the shared channel broadcaster warm.
            self.assertTrue(session.directory.exists())
            manager.close()
            self.assertFalse(session.directory.exists())
            self.assertEqual(processes[0].returncode, 0)

    def test_viewers_share_one_channel_broadcast(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            when = dt.datetime.now()
            airing = Airing(
                "42", "Test TV", "Show", "Episode", when,
                when + dt.timedelta(minutes=30), when,
                when + dt.timedelta(minutes=30), str(media), 0, 1800, 1800,
            )
            processes = []

            def factory(command, **_kwargs):
                process = FakeProcess(command)
                processes.append(process)
                return process

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=lambda channel: airing),
                root=Path(temp_dir) / "hls",
                process_factory=factory,
            )
            first, _ = manager.create("42")
            second, _ = manager.create("42")
            self.assertNotEqual(first.session_id, second.session_id)
            self.assertEqual(first.broadcast_id, second.broadcast_id)
            self.assertEqual(first.directory, second.directory)
            self.assertEqual(len(processes), 1)
            manager.delete(first.session_id)
            self.assertIsNone(processes[0].returncode)
            self.assertIs(manager.get(second.session_id).process, processes[0])
            manager.close()

    def test_subtitle_mode_changes_share_stream_without_retuning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            when = dt.datetime.now()
            airing = Airing(
                "42", "Test TV", "Show", "Episode", when,
                when + dt.timedelta(minutes=30), when,
                when + dt.timedelta(minutes=30), str(media), 0, 1800, 1800,
            )
            processes = []

            def factory(command, **_kwargs):
                process = FakeProcess(command)
                processes.append(process)
                return process

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=lambda channel: airing),
                root=Path(temp_dir) / "hls",
                process_factory=factory,
            )
            auto, _ = manager.create("42", subtitle_mode="auto")
            english, _ = manager.create("42", subtitle_mode="english")
            off, _ = manager.create("42", subtitle_mode="off")
            self.assertEqual(
                {auto.broadcast_id, english.broadcast_id, off.broadcast_id},
                {auto.broadcast_id},
            )
            self.assertEqual(len(processes), 1)
            manager.close()

    def test_automatic_boundary_resolves_exact_next_item_start(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "commercial.mkv"
            media.touch()
            boundary = dt.datetime(2026, 7, 31, 18, 30)
            airing = Airing(
                "42", "Test TV", "Show", "Commercial", boundary,
                boundary + dt.timedelta(seconds=30), boundary,
                boundary + dt.timedelta(seconds=30), str(media), 0, 30, 30,
                content_type="commercial",
            )
            feature = Airing(
                "42", "Test TV", "Next Show", "Episode", airing.item_end,
                airing.item_end + dt.timedelta(minutes=30), airing.item_end,
                airing.item_end + dt.timedelta(minutes=22), str(media), 0,
                1800, 1320, content_type="feature",
            )
            calls = []

            def resolve(channel, when):
                calls.append((channel, when))
                return airing if when == boundary else feature

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=resolve),
                root=Path(temp_dir) / "hls",
                process_factory=lambda command, **_kwargs: FakeProcess(command),
            )
            with patch(
                "fs42.hometv._local_now",
                return_value=boundary + dt.timedelta(seconds=5),
            ):
                session, selected = manager.create("42", boundary_at=boundary)

            self.assertEqual(
                calls, [("42", boundary), ("42", airing.item_end)]
            )
            self.assertEqual(selected.offset, 0)
            # Only the tail is shortened to put the following show on time.
            self.assertEqual(selected.remaining, 25)
            manager.delete(session.session_id)
            manager.close()

    def test_commercial_before_commercial_is_not_shortened(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "commercial.mkv"
            media.touch()
            boundary = dt.datetime(2026, 7, 31, 18, 30)
            commercial = Airing(
                "42", "Test TV", "Show", "Commercial", boundary,
                boundary + dt.timedelta(seconds=30), boundary,
                boundary + dt.timedelta(seconds=30), str(media), 0, 30, 30,
                content_type="commercial",
            )
            resolver = SimpleNamespace(now=lambda _channel, _when: commercial)
            manager = HLSSessionManager(
                resolver=resolver,
                root=Path(temp_dir) / "hls",
                process_factory=lambda command, **_kwargs: FakeProcess(command),
            )
            with patch(
                "fs42.hometv._local_now",
                return_value=boundary + dt.timedelta(seconds=5),
            ):
                session, selected = manager.create("42", boundary_at=boundary)

            self.assertEqual(selected.offset, 0)
            self.assertEqual(selected.remaining, 30)
            manager.delete(session.session_id)
            manager.close()

    def test_next_ad_can_prewarm_without_stopping_current_ad(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first_media = root / "first-ad.mkv"
            second_media = root / "second-ad.mkv"
            first_media.touch()
            second_media.touch()
            boundary = dt.datetime(2026, 7, 31, 18, 30)
            first = Airing(
                "42", "Test TV", "Show", "First ad",
                boundary - dt.timedelta(seconds=30),
                boundary + dt.timedelta(minutes=30),
                boundary - dt.timedelta(seconds=30), boundary,
                str(first_media), 0, 1800, 30, content_type="commercial",
            )
            second = Airing(
                "42", "Test TV", "Show", "Second ad",
                boundary - dt.timedelta(seconds=30),
                boundary + dt.timedelta(minutes=30),
                boundary, boundary + dt.timedelta(seconds=30),
                str(second_media), 0, 1800, 30, content_type="commercial",
            )
            processes = []

            def resolve(_channel, when=None):
                return second if when == boundary else first

            def factory(command, **_kwargs):
                process = FakeProcess(command)
                processes.append(process)
                return process

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=resolve),
                root=root / "hls", process_factory=factory,
            )
            current, _ = manager.create("42")
            upcoming, _ = manager.create("42", boundary_at=boundary)

            self.assertNotEqual(current.broadcast_id, upcoming.broadcast_id)
            self.assertEqual(len(processes), 2)
            self.assertIsNone(processes[0].returncode)
            self.assertIsNone(processes[1].returncode)
            manager.close()

    def test_channel_limit_evicts_unused_broadcast_for_rapid_tuning(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            media = root / "show.mkv"
            media.touch()
            when = dt.datetime.now()

            def resolve(channel):
                return Airing(
                    str(channel), f"Channel {channel}", "Show", "Episode", when,
                    when + dt.timedelta(minutes=30), when,
                    when + dt.timedelta(minutes=30), str(media), 0, 1800, 1800,
                )

            processes = []

            def factory(command, **_kwargs):
                process = FakeProcess(command)
                processes.append(process)
                return process

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=resolve),
                root=root / "hls",
                max_sessions=2,
                process_factory=factory,
            )
            first, _ = manager.create("1")
            second, _ = manager.create("2")
            manager.delete(first.session_id)

            third, _ = manager.create("3")

            self.assertFalse(any(key[:2] == ("1", "auto") for key in manager.broadcasts))
            self.assertTrue(any(key[:2] == ("2", "auto") for key in manager.broadcasts))
            self.assertTrue(any(key[:2] == ("3", "auto") for key in manager.broadcasts))
            self.assertEqual(processes[0].returncode, 0)
            self.assertIsNone(processes[1].returncode)
            self.assertIsNone(processes[2].returncode)
            manager.delete(second.session_id)
            manager.delete(third.session_id)
            manager.close()

    def test_nvenc_profile_uses_bounded_gpu_encoding(self):
        when = dt.datetime.now()
        airing = Airing(
            "42", "Test TV", "Show", "Episode", when,
            when + dt.timedelta(minutes=30), when,
            when + dt.timedelta(minutes=30), "/media/show.mkv", 0, 1800, 1800,
        )
        with patch.dict(
            "os.environ", {"FS42_HLS_VIDEO_ENCODER": "h264_nvenc"}
        ):
            command = HLSSessionManager._ffmpeg_command(
                airing,
                "auto",
                Path("/tmp/hls"),
                streams=[{"codec_type": "video", "codec_name": "hevc"}],
            )
        self.assertIn("h264_nvenc", command)
        self.assertEqual(command[command.index("-maxrate") + 1], "8M")

    def test_auto_profile_normalizes_even_h264_aac_sources(self):
        when = dt.datetime.now()
        airing = Airing(
            "42", "Test TV", "Show", "Episode", when,
            when + dt.timedelta(minutes=30), when,
            when + dt.timedelta(minutes=30), "/media/show.mkv", 0, 1800, 1800,
        )
        command = HLSSessionManager._ffmpeg_command(
            airing,
            "auto",
            Path("/tmp/hls"),
            streams=[
                {"codec_type": "video", "codec_name": "h264"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
        )
        self.assertIn("libx264", command)
        self.assertIn("aac", command)
        self.assertNotIn("copy", command)

    def test_session_starts_ffmpeg_in_its_own_process_group(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            when = dt.datetime.now()
            airing = Airing(
                "42", "Test TV", "Show", "Episode", when,
                when + dt.timedelta(minutes=30), when,
                when + dt.timedelta(minutes=30), str(media), 0, 1800, 1800,
            )
            process_options = {}

            def factory(command, **kwargs):
                process_options.update(kwargs)
                return FakeProcess(command)

            manager = HLSSessionManager(
                resolver=SimpleNamespace(now=lambda channel: airing),
                root=Path(temp_dir) / "hls",
                process_factory=factory,
            )
            session, _ = manager.create("42")
            self.assertTrue(process_options["start_new_session"])
            manager.delete(session.session_id)

    def test_stop_signals_entire_ffmpeg_process_group(self):
        process = FakeProcess(["ffmpeg"])
        process.pid = 4242
        session = SimpleNamespace(
            process=process,
            directory=Path("/tmp/nonexistent-fs42-hls-test"),
        )
        with patch("fs42.hometv.os.killpg") as killpg:
            HLSSessionManager._stop(session)
        self.assertEqual(
            [call.args for call in killpg.call_args_list],
            [(4242, 15), (4242, 9)],
        )

    def test_non_english_audio_selects_english_subtitles(self):
        probe = SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "audio",
                            "codec_name": "aac",
                            "tags": {"language": "jpn"},
                        },
                        {
                            "codec_type": "subtitle",
                            "codec_name": "ass",
                            "tags": {"language": "eng"},
                        },
                    ]
                }
            )
        )
        with patch("fs42.hometv.subprocess.run", return_value=probe):
            self.assertEqual(
                HLSSessionManager._english_subtitle("/media/show.mkv"),
                ("ass", 0),
            )

    def test_english_audio_does_not_enable_subtitles(self):
        probe = SimpleNamespace(
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "audio",
                            "tags": {"language": "eng"},
                        },
                        {
                            "codec_type": "subtitle",
                            "codec_name": "ass",
                            "tags": {"language": "eng"},
                        },
                    ]
                }
            )
        )
        with patch("fs42.hometv.subprocess.run", return_value=probe):
            self.assertIsNone(
                HLSSessionManager._english_subtitle("/media/show.mkv")
            )

    def test_forced_english_mode_allows_subtitles_with_english_audio(self):
        streams = [
            {"codec_type": "audio", "tags": {"language": "eng"}},
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "eng", "title": "English Full Dialogue"},
            },
        ]
        self.assertEqual(
            HLSSessionManager._select_english_subtitle(
                streams, require_foreign_audio=False
            ),
            ("ass", 0),
        )

    def test_anime_track_titles_recover_missing_language_tags(self):
        streams = [
            {"codec_type": "audio", "tags": {"title": "Japanese 2.0"}},
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"title": "English Full Dialogue"},
            },
        ]
        self.assertEqual(
            HLSSessionManager._select_english_subtitle(streams),
            ("ass", 0),
        )

    def test_full_dialogue_subtitles_are_preferred_over_signs(self):
        streams = [
            {"codec_type": "audio", "tags": {"language": "jpn"}},
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "eng", "title": "Signs & Songs"},
            },
            {
                "codec_type": "subtitle",
                "codec_name": "ass",
                "tags": {"language": "eng", "title": "English Full Dialogue"},
            },
        ]
        self.assertEqual(
            HLSSessionManager._select_english_subtitle(streams),
            ("ass", 1),
        )

    def test_auto_profile_burns_selected_text_subtitle(self):
        when = dt.datetime.now()
        airing = Airing(
            "42",
            "Anime",
            "Attack on Titan",
            "Episode",
            when,
            when + dt.timedelta(minutes=30),
            when,
            when + dt.timedelta(minutes=30),
            "/media/Attack on Titan's Return.mkv",
            30,
            1800,
            1770,
        )
        command = HLSSessionManager._ffmpeg_command(
            airing,
            "auto",
            Path("/tmp/hls"),
            ("ass", 0),
        )
        self.assertIn("-vf", command)
        subtitle_filter = command[command.index("-vf") + 1]
        self.assertIn("subtitles=", subtitle_filter)
        self.assertIn(r"Attack on Titan\'s Return.mkv", subtitle_filter)
        self.assertIn("setpts=PTS+30.000/TB", subtitle_filter)
        self.assertTrue(subtitle_filter.endswith("setpts=PTS-STARTPTS"))

    def test_auto_audio_is_normalized_for_browser_source_buffers(self):
        when = dt.datetime.now()
        airing = Airing(
            "42", "Test TV", "Show", "Episode", when,
            when + dt.timedelta(minutes=30), when,
            when + dt.timedelta(minutes=30), "/media/show.mkv", 0, 1800, 1800,
        )
        command = HLSSessionManager._ffmpeg_command(
            airing, "auto", Path("/tmp/hls"), streams=[]
        )
        self.assertEqual(command[command.index("-profile:a") + 1], "aac_low")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "2")
        self.assertIn("aresample=async=1:first_pts=0", command)

    def test_hls_asset_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            manager = HLSSessionManager(
                resolver=SimpleNamespace(),
                root=temp_dir,
                process_factory=lambda *_args, **_kwargs: None,
            )
            with self.assertRaises(ValueError):
                manager.asset("missing", "../secret")


class DatabaseTests(unittest.TestCase):
    def test_connections_enable_wal_and_busy_timeout(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "test.db")
            connection = connect(db_path)
            try:
                self.assertEqual(
                    connection.execute("PRAGMA journal_mode").fetchone()[0].lower(),
                    "wal",
                )
                self.assertGreaterEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0], 1000
                )
            finally:
                connection.close()


class BroadcastSchedulerTests(unittest.TestCase):
    def _entries(self):
        return [
            SimpleNamespace(path=f"/media/Test Show S01E{number:02d}.mkv", realpath=None)
            for number in range(1, 5)
        ]

    @staticmethod
    def _metadata(path):
        episode = int(Path(path).stem.rsplit("E", 1)[1])
        return {
            "type": "episode",
            "show_title": "Test Show",
            "season": 1,
            "episode": episode,
        }

    def test_future_weeks_reserve_consecutive_premieres_without_marking_aired(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            db_path = str(Path(temp_dir) / "history.db")
            scheduler = BroadcastScheduler("Channel", db_path)
            slot = {"programming": {"mode": "premiere", "slot_id": "tue-20"}}
            starts = [dt.datetime(2026, 8, 4, 20) + dt.timedelta(weeks=i) for i in range(4)]
            selected_paths = []
            for start in starts:
                entry, programming = scheduler.select(slot, "show", self._entries(), start)
                selected_paths.append(entry.path)
                scheduler.stage(programming, start, start + dt.timedelta(minutes=30))
            scheduler.commit()
            rows = scheduler.history.rows("Channel", "test-show")
            self.assertEqual(
                selected_paths,
                [f"/media/Test Show S01E{i:02d}.mkv" for i in range(1, 5)],
            )
            self.assertTrue(all(row[8] == "reserved" for row in rows))

    def test_numeric_order_never_jumps_to_season_twenty(self):
        entries = [
            SimpleNamespace(path=path, realpath=None) for path in (
                "/media/Test Show S20E01.mkv",
                "/media/Test Show S02E01.mkv",
                "/media/Test Show S01E10.mkv",
                "/media/Test Show S01E02.mkv",
            )
        ]
        def metadata(path):
            match = re.search(r"S(\d+)E(\d+)", path)
            return {"type": "episode", "show_title": "Test Show",
                    "season": int(match.group(1)), "episode": int(match.group(2))}
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=metadata
        ):
            scheduler = BroadcastScheduler("Channel", str(Path(temp_dir) / "history.db"))
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            chosen = []
            for week in range(4):
                when = dt.datetime(2026, 1, 1) + dt.timedelta(weeks=week)
                entry, programming = scheduler.select(slot, "show", entries, when)
                chosen.append(entry.path)
                scheduler.stage(programming, when, when + dt.timedelta(hours=1))
            self.assertEqual(chosen, [entries[i].path for i in (3, 2, 1, 0)])

    def test_legacy_equal_play_counts_choose_chronological_episode(self):
        from fs42.catalog import ShowCatalog

        candidates = [
            SimpleNamespace(path=path, realpath=None, count=0) for path in (
                "/media/Test Show S20E01.mkv",
                "/media/Test Show S01E02.mkv",
                "/media/Test Show S01E01.mkv",
            )
        ]
        def metadata(path):
            match = re.search(r"S(\d+)E(\d+)", path)
            return {"type": "episode", "show_title": "Test Show",
                    "season": int(match.group(1)), "episode": int(match.group(2))}
        with patch("fs42.broadcast_scheduler.MetadataIO.read", side_effect=metadata):
            selected = ShowCatalog._lowest_count(SimpleNamespace(), candidates)
        self.assertTrue(selected.path.endswith("S01E01.mkv"))

    def test_history_cursor_and_reset_control_next_premiere(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler("Channel", str(Path(temp_dir) / "history.db"))
            scheduler.history.set_cursor("Channel", "test-show", 1, 3)
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            entry, _ = scheduler.select(slot, "show", self._entries(), dt.datetime(2026, 1, 1))
            self.assertTrue(entry.path.endswith("S01E03.mkv"))
            scheduler.history.reset_series("Channel", "test-show")
            entry, _ = scheduler.select(slot, "show", self._entries(), dt.datetime(2026, 1, 1))
            self.assertTrue(entry.path.endswith("S01E01.mkv"))

    def test_history_editor_can_remove_entry_and_future_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler("Channel", str(Path(temp_dir) / "history.db"))
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            for week in range(2):
                when = dt.datetime.now() + dt.timedelta(weeks=week + 1)
                _, programming = scheduler.select(slot, "show", self._entries(), when)
                scheduler.stage(programming, when, when + dt.timedelta(hours=1))
            scheduler.commit()
            details = scheduler.history.detail_rows("Channel", "test-show")
            self.assertEqual(len(details), 2)
            self.assertTrue(scheduler.history.remove_entry(
                "Channel", "test-show", details[0]["id"]
            ))
            scheduler.history.clear_future("Channel", "test-show")
            self.assertEqual(scheduler.history.detail_rows("Channel", "test-show"), [])

    def test_reruns_use_only_aired_premieres_and_respect_repeat_gap(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            db_path = str(Path(temp_dir) / "history.db")
            scheduler = BroadcastScheduler("Channel", db_path)
            premiere_slot = {"programming": {"mode": "premiere", "slot_id": "tue-20"}}
            rerun_slot = {
                "programming": {
                    "mode": "rerun",
                    "slot_id": "sat-14",
                    "minimum_rerun_gap_days": 7,
                }
            }
            premiere = dt.datetime.now() - dt.timedelta(days=14)
            entry, programming = scheduler.select(
                premiere_slot, "show", self._entries(), premiere
            )
            scheduler.stage(programming, premiere, premiere + dt.timedelta(minutes=30))
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())

            rerun = dt.datetime.now() - dt.timedelta(days=2)
            entry, programming = scheduler.select(
                rerun_slot, "show", self._entries(), rerun
            )
            self.assertTrue(entry.path.endswith("S01E01.mkv"))
            scheduler.stage(programming, rerun, rerun + dt.timedelta(minutes=30))
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())
            self.assertIsNone(
                scheduler.select(rerun_slot, "show", self._entries(), dt.datetime.now())
            )

    def test_library_end_hold_does_not_wrap_episode_one_as_new(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {
                "programming": {
                    "mode": "premiere",
                    "slot_id": "tue-20",
                    "library_end_policy": "hold",
                }
            }
            first = dt.datetime.now() - dt.timedelta(weeks=8)
            for index in range(4):
                start = first + dt.timedelta(weeks=index)
                _, programming = scheduler.select(
                    slot, "show", self._entries(), start
                )
                scheduler.stage(
                    programming, start, start + dt.timedelta(minutes=30)
                )
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())
            self.assertIsNone(
                scheduler.select(slot, "show", self._entries(), dt.datetime.now())
            )

    def test_library_restart_after_hiatus_is_labeled_rerun(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {
                "programming": {
                    "mode": "premiere",
                    "slot_id": "tue-20",
                    "library_end_policy": "restart_after_hiatus",
                    "restart_hiatus_weeks": 2,
                    "minimum_rerun_gap_days": 0,
                }
            }
            first = dt.datetime.now() - dt.timedelta(weeks=8)
            for index in range(4):
                start = first + dt.timedelta(weeks=index)
                _, programming = scheduler.select(
                    slot, "show", self._entries(), start
                )
                scheduler.stage(
                    programming, start, start + dt.timedelta(minutes=30)
                )
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())
            entry, programming = scheduler.select(
                slot, "show", self._entries(), dt.datetime.now()
            )
            self.assertTrue(entry.path.endswith("S01E01.mkv"))
            self.assertEqual(programming["airing_kind"], "rerun")

    def test_renamed_episode_keeps_its_logical_premiere_identity(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {
                "programming": {
                    "mode": "premiere",
                    "slot_id": "tue-20",
                }
            }
            old_entry = SimpleNamespace(
                path="/old/Test Show S01E01.mkv", realpath=None
            )
            past = dt.datetime.now() - dt.timedelta(weeks=2)
            _, programming = scheduler.select(slot, "show", [old_entry], past)
            scheduler.stage(
                programming, past, past + dt.timedelta(minutes=30)
            )
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())

            renamed = [
                SimpleNamespace(
                    path="/new/Test Show S01E01.mkv", realpath=None
                ),
                SimpleNamespace(
                    path="/new/Test Show S01E02.mkv", realpath=None
                ),
            ]
            entry, programming = scheduler.select(
                slot, "show", renamed, dt.datetime.now()
            )
            self.assertTrue(entry.path.endswith("S01E02.mkv"))
            self.assertEqual(programming["airing_kind"], "premiere")

    def test_future_reset_keeps_aired_history_and_releases_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            past = dt.datetime.now() - dt.timedelta(days=8)
            future = dt.datetime.now() + dt.timedelta(days=6)
            for start in (past, future):
                _, programming = scheduler.select(
                    slot, "show", self._entries(), start
                )
                scheduler.stage(
                    programming, start, start + dt.timedelta(minutes=30)
                )
            scheduler.commit()
            scheduler.history.release_future("Channel", dt.datetime.now())
            rows = scheduler.history.rows("Channel", "test-show")
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][8], "aired")
            self.assertTrue(rows[0][2].endswith("S01E01.mkv"))

    def test_weekly_slot_stays_on_wall_clock_across_dst(self):
        central = ZoneInfo("America/Chicago")
        for start in (
            dt.datetime(2026, 3, 3, 20, tzinfo=central),
            dt.datetime(2026, 10, 27, 20, tzinfo=central),
        ):
            following = start + dt.timedelta(weeks=1)
            self.assertEqual(following.hour, 20)
            self.assertNotEqual(start.utcoffset(), following.utcoffset())
            self.assertNotEqual(
                BroadcastScheduler._occurrence_key(start),
                BroadcastScheduler._occurrence_key(following),
            )

    def test_wait_for_missing_does_not_skip_an_episode_gap(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {
                "programming": {
                    "mode": "premiere",
                    "slot_id": "weekly",
                    "catch_up_policy": "wait_for_missing",
                    "minimum_rerun_gap_days": 365,
                }
            }
            past = dt.datetime.now() - dt.timedelta(days=14)
            first = self._entries()[0]
            _, programming = scheduler.select(slot, "show", [first], past)
            scheduler.stage(programming, past, past + dt.timedelta(minutes=30))
            scheduler.commit()
            scheduler.history.reconcile("Channel", dt.datetime.now())
            gap_entries = [self._entries()[0], self._entries()[2]]
            self.assertIsNone(
                scheduler.select(slot, "show", gap_entries, dt.datetime.now())
            )

    def test_hiatus_window_suppresses_new_premiere(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            today = dt.datetime.now().date()
            slot = {
                "programming": {
                    "mode": "premiere",
                    "slot_id": "weekly",
                    "hiatus_ranges": [{
                        "start": today.isoformat(),
                        "end": today.isoformat(),
                    }],
                }
            }
            self.assertIsNone(
                scheduler.select(slot, "show", self._entries(), dt.datetime.now())
            )

    def test_multiweek_cadence_uses_stable_anchor(self):
        policy = {
            "premiere_interval_weeks": 2,
            "cadence_anchor_date": "2026-08-04",
        }
        self.assertTrue(BroadcastScheduler._premiere_due(
            policy, dt.datetime(2026, 8, 4, 20)
        ))
        self.assertFalse(BroadcastScheduler._premiere_due(
            policy, dt.datetime(2026, 8, 11, 20)
        ))
        self.assertTrue(BroadcastScheduler._premiere_due(
            policy, dt.datetime(2026, 8, 18, 20)
        ))

    def test_specials_and_multipart_episodes_have_deterministic_order(self):
        entries = [
            SimpleNamespace(path="/media/Test Show S01E01B.mkv", realpath=None),
            SimpleNamespace(path="/media/Test Show S01E01A.mkv", realpath=None),
            SimpleNamespace(path="/media/Test Show S00E01.mkv", realpath=None),
        ]

        def metadata(path):
            match = __import__("re").search(r"S(\d+)E(\d+)", path)
            return {
                "type": "episode", "show_title": "Test Show",
                "season": int(match.group(1)), "episode": int(match.group(2)),
            }

        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            chosen = []
            for index in range(3):
                when = dt.datetime(2026, 8, 4, 20) + dt.timedelta(weeks=index)
                entry, programming = scheduler.select(slot, "show", entries, when)
                chosen.append(Path(entry.path).stem)
                scheduler.stage(programming, when, when + dt.timedelta(minutes=30))
            self.assertEqual(
                chosen,
                [
                    "Test Show S00E01",
                    "Test Show S01E01A",
                    "Test Show S01E01B",
                ],
            )

    def test_duplicate_encode_does_not_receive_a_second_premiere(self):
        entries = [
            SimpleNamespace(path="/media/a/Test Show S01E01.mkv", realpath=None),
            SimpleNamespace(path="/media/b/Test Show S01E01.mp4", realpath=None),
            SimpleNamespace(path="/media/Test Show S01E02.mkv", realpath=None),
        ]
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            first = dt.datetime(2026, 8, 4, 20)
            _, programming = scheduler.select(slot, "show", entries, first)
            scheduler.stage(programming, first, first + dt.timedelta(minutes=30))
            entry, _ = scheduler.select(slot, "show", entries, first + dt.timedelta(weeks=1))
            self.assertTrue(entry.path.endswith("S01E02.mkv"))

    def test_newly_discovered_later_episode_appends_after_reservations(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch(
            "fs42.broadcast_scheduler.MetadataIO.read", side_effect=self._metadata
        ):
            scheduler = BroadcastScheduler(
                "Channel", str(Path(temp_dir) / "history.db")
            )
            slot = {"programming": {"mode": "premiere", "slot_id": "weekly"}}
            first = dt.datetime(2026, 8, 4, 20)
            initial = self._entries()[:2]
            for index in range(2):
                when = first + dt.timedelta(weeks=index)
                _, programming = scheduler.select(slot, "show", initial, when)
                scheduler.stage(programming, when, when + dt.timedelta(minutes=30))
            scheduler.commit()
            entry, _ = scheduler.select(
                slot, "show", self._entries()[:3], first + dt.timedelta(weeks=2)
            )
            self.assertTrue(entry.path.endswith("S01E03.mkv"))


class SchedulePromoTests(unittest.TestCase):
    def test_promo_copy_distinguishes_premiere_episode_and_finale(self):
        when = dt.datetime(2026, 8, 6, 18)
        self.assertIn(
            "New season premieres Thursday",
            SchedulePromoAgent._copy({"series": "Show", "season_premiere": True}, when)[1],
        )
        self.assertIn(
            "New episode Thursday",
            SchedulePromoAgent._copy({"series": "Show"}, when)[1],
        )
        self.assertIn(
            "Season finale Thursday",
            SchedulePromoAgent._copy({"series": "Show", "season_finale": True}, when)[1],
        )

    def test_promo_is_spaced_and_attached_before_real_premiere(self):
        now = dt.datetime(2026, 8, 4, 12)

        def block(start, programming=None, buffer=480):
            item = SimpleNamespace(
                start_time=start,
                content=SimpleNamespace(path="/media/show.mkv"),
                programming=programming,
                start_bump=None,
                break_info={},
            )
            item.buffer_duration = lambda: buffer
            return item

        promo_target = block(now)
        premiere = block(
            now + dt.timedelta(hours=6),
            {
                "airing_kind": "premiere",
                "series": "Test Show",
                "series_key": "test-show",
                "media_path": "/media/Test Show S01E02.mkv",
                "episode": 2,
            },
        )
        with patch.object(
            SchedulePromoAgent, "_render", return_value=Path("/promos/test.mp4")
        ):
            count = SchedulePromoAgent.apply(
                {
                    "network_name": "Channel",
                    "schedule_promos": {"intensity": "frequent"},
                },
                [promo_target, premiere],
            )
        self.assertEqual(count, 1)
        self.assertEqual(promo_target.start_bump["duration"], 6)
        self.assertEqual(
            promo_target.break_info["_scheduled_promo"]["series"],
            "Test Show",
        )

    def test_channel_bumper_tree_and_season_windows(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"FS42_CHANNEL_BUMPER_DIR": temp_dir}
        ):
            folders = ensure_channel_folders("Toon Mix")
            self.assertTrue(all(folder.is_dir() for folder in folders.values()))
            self.assertIn(
                "christmas", active_seasons(dt.datetime(2026, 12, 24))
            )
            self.assertIn(
                "new-years", active_seasons(dt.datetime(2026, 12, 31))
            )
            self.assertNotIn(
                "halloween", active_seasons(dt.datetime(2026, 8, 1))
            )


class SubtitleProviderTests(unittest.TestCase):
    def test_file_hash_is_stable_and_size_sensitive(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "episode.mkv"
            media.write_bytes(bytes(range(256)) * 512)
            first = opensubtitles_hash(str(media))
            second = opensubtitles_hash(str(media))
            self.assertEqual(first, second)
            self.assertEqual(first[1], 131072)

    def test_provider_refuses_title_only_non_hash_match(self):
        class Response:
            content = b""
            def raise_for_status(self):
                return None
            def json(self):
                return {
                    "data": [{
                        "id": "feature",
                        "attributes": {
                            "moviehash_match": False,
                            "files": [{"file_id": 42}],
                        },
                    }]
                }

        session = SimpleNamespace(get=lambda *args, **kwargs: Response())
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"FS42_SUBTITLE_CACHE_DIR": str(Path(temp_dir) / "subs")}
        ):
            media = Path(temp_dir) / "episode.mkv"
            media.write_bytes(b"x" * 131072)
            provider = SubtitleProvider(api_key="test-api-key", session=session)
            self.assertIsNone(provider.fetch_english(str(media)))


class ProviderSettingsTests(unittest.TestCase):
    def test_provider_keys_persist_atomically_and_response_is_redacted(self):
        manager = settings_api.StationManager()
        previous = manager.server_conf.get("tmdb_api_key")
        previous_subtitles = manager.server_conf.get("opensubtitles_api_key")
        try:
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                settings_api, "CONFIG_PATH", Path(temp_dir) / "main_config.json"
            ), patch.object(settings_api, "refresh_tmdb_helper"), patch.object(
                settings_api, "_validate_tmdb_key"
            ) as validate:
                response = asyncio.run(settings_api.save_settings(
                    settings_api.ProviderSettings(
                        tmdb_api_key="a" * 32,
                        opensubtitles_api_key="subtitle-key",
                    )
                ))
                saved = json.loads(
                    settings_api.CONFIG_PATH.read_text(encoding="utf-8")
                )
                self.assertEqual(saved["tmdb_api_key"], "a" * 32)
                self.assertNotIn("a" * 32, json.dumps(response))
                self.assertEqual(response["tmdb"]["masked"], "••••aaaa")
                validate.assert_called_once_with("a" * 32)
        finally:
            if previous is None:
                manager.server_conf.pop("tmdb_api_key", None)
            else:
                manager.server_conf["tmdb_api_key"] = previous
            if previous_subtitles is None:
                manager.server_conf.pop("opensubtitles_api_key", None)
            else:
                manager.server_conf["opensubtitles_api_key"] = previous_subtitles

    def test_rejected_tmdb_key_does_not_replace_known_good_settings(self):
        manager = settings_api.StationManager()
        previous = manager.server_conf.get("tmdb_api_key")
        manager.server_conf["tmdb_api_key"] = "b" * 32
        try:
            with tempfile.TemporaryDirectory() as temp_dir, patch.object(
                settings_api, "CONFIG_PATH", Path(temp_dir) / "main_config.json"
            ), patch.object(
                settings_api,
                "_validate_tmdb_key",
                side_effect=HTTPException(400, "TMDB rejected that key"),
            ):
                settings_api.CONFIG_PATH.write_text(
                    json.dumps({"tmdb_api_key": "b" * 32}), encoding="utf-8"
                )
                with self.assertRaises(HTTPException):
                    asyncio.run(settings_api.save_settings(
                        settings_api.ProviderSettings(tmdb_api_key="c" * 32)
                    ))
                saved = json.loads(
                    settings_api.CONFIG_PATH.read_text(encoding="utf-8")
                )
                self.assertEqual(saved["tmdb_api_key"], "b" * 32)
        finally:
            if previous is None:
                manager.server_conf.pop("tmdb_api_key", None)
            else:
                manager.server_conf["tmdb_api_key"] = previous


class MetadataEnrichmentTests(unittest.TestCase):
    def test_canonical_artwork_key_is_shared_by_every_series_episode(self):
        from fs42.artwork_preloader import canonical_key

        first = canonical_key(
            "/tv/SpongeBob/S01E01.mkv",
            {"type": "episode", "show_title": "SpongeBob SquarePants"},
        )
        future = canonical_key(
            "/tv/SpongeBob/S20E10.mkv",
            {"type": "episode", "show_title": "SpongeBob SquarePants"},
        )
        other = canonical_key(
            "/tv/The Blacklist/S01E01.mkv",
            {"type": "episode", "show_title": "The Blacklist"},
        )
        self.assertEqual(first[0], future[0])
        self.assertNotEqual(first[0], other[0])

    def test_named_fallback_is_program_specific_and_persisted(self):
        from fs42 import artwork_preloader

        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            artwork_preloader, "artwork_root", return_value=Path(temp_dir)
        ):
            sponge = artwork_preloader._ensure_named_fallback(
                "series:spongebob-squarepants", "SpongeBob SquarePants"
            )
            blacklist = artwork_preloader._ensure_named_fallback(
                "series:the-blacklist", "The Blacklist"
            )
            self.assertRegex(sponge, r"^[a-f0-9]{64}\.jpg$")
            self.assertNotEqual(sponge, blacklist)
            self.assertGreater((Path(temp_dir) / sponge).stat().st_size, 1000)

    def test_index_resolves_odd_episode_names_without_rereading_metadata(self):
        from fs42 import artwork_preloader

        path = "/tv/Forensic Files/Forensic Files 07x36 All Charged Up.mkv"
        filename = "d" * 64 + ".jpg"
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            artwork_preloader, "artwork_root", return_value=Path(temp_dir)
        ):
            (Path(temp_dir) / filename).write_bytes(b"forensic files artwork")
            old_index = artwork_preloader._INDEX
            artwork_preloader._INDEX = {
                "version": 2,
                "entries": {"series:forensic-files": {"file": filename}},
                "paths": {
                    artwork_preloader._path_digest(path): "series:forensic-files"
                },
            }
            try:
                record = artwork_preloader.artwork_record(path, None)
            finally:
                artwork_preloader._INDEX = old_index
            self.assertEqual(record["file"], filename)

    def test_startup_preloader_caches_each_series_once(self):
        from fs42 import artwork_preloader

        entries = [
            SimpleNamespace(path="/tv/SpongeBob/S01E01.mkv", media_type="video", content_type="feature"),
            SimpleNamespace(path="/tv/SpongeBob/S01E02.mkv", media_type="video", content_type="feature"),
            SimpleNamespace(path="/tv/The Blacklist/S01E01.mkv", media_type="video", content_type="feature"),
        ]
        stations = [{"_has_catalog": True}]
        warmed = []
        def metadata(path):
            return {"type": "episode", "show_title": (
                "SpongeBob SquarePants" if "SpongeBob" in path else "The Blacklist"
            )}
        with tempfile.TemporaryDirectory() as temp_dir:
            def ensure(series, path):
                warmed.append((series, path))
                name = ("a" if "Sponge" in series else "b") * 64 + ".jpg"
                (Path(temp_dir) / name).write_bytes(series.encode())
                return name
            fake_enricher = SimpleNamespace(ensure_series_artwork=ensure)
            with (
                patch.object(artwork_preloader, "artwork_root", return_value=Path(temp_dir)),
                patch.object(artwork_preloader, "StationManager", return_value=SimpleNamespace(stations=stations)),
                patch.object(artwork_preloader.CatalogAPI, "get_entries", return_value=entries),
                patch.object(artwork_preloader.MetadataIO, "read", side_effect=metadata),
                patch.object(artwork_preloader, "MetadataEnricher", return_value=fake_enricher),
            ):
                result = artwork_preloader.prepare_all_series()
        self.assertEqual(result["state"], "ready")
        self.assertEqual(result["total"], 2)
        self.assertEqual(len(warmed), 2)

    def test_series_index_never_crosses_spongebob_and_blacklist_artwork(self):
        class Helper:
            def is_configured(self): return False
        with tempfile.TemporaryDirectory() as temp_dir:
            art_dir = Path(temp_dir) / "art"
            art_dir.mkdir()
            sponge_frame = "a" * 64 + ".jpg"
            blacklist_frame = "b" * 64 + ".jpg"
            (art_dir / sponge_frame).write_bytes(b"spongebob frame")
            (art_dir / blacklist_frame).write_bytes(b"blacklist frame")
            enricher = MetadataEnricher(
                helper=Helper(), fallback_helper=Helper(), db_path=":memory:"
            )
            enricher.art_dir = art_dir
            with patch.object(
                enricher, "ensure_local_artwork",
                side_effect=[sponge_frame, blacklist_frame],
            ):
                sponge = enricher.ensure_series_artwork(
                    "SpongeBob SquarePants", "/media/SpongeBob/S01E01.mkv"
                )
                blacklist = enricher.ensure_series_artwork(
                    "The Blacklist", "/media/The Blacklist/S01E01.mkv"
                )
            self.assertNotEqual(sponge, blacklist)
            self.assertEqual((art_dir / sponge).read_bytes(), b"spongebob frame")
            self.assertEqual((art_dir / blacklist).read_bytes(), b"blacklist frame")
            self.assertEqual(enricher.series_artwork("The Blacklist"), blacklist)

    def test_concurrent_series_index_writes_preserve_both_shows(self):
        from concurrent.futures import ThreadPoolExecutor

        class Helper:
            def is_configured(self): return False
        with tempfile.TemporaryDirectory() as temp_dir:
            art_dir = Path(temp_dir) / "art"
            art_dir.mkdir()
            first = "c" * 64 + ".jpg"
            second = "d" * 64 + ".jpg"
            (art_dir / first).write_bytes(b"first")
            (art_dir / second).write_bytes(b"second")
            enricher = MetadataEnricher(
                helper=Helper(), fallback_helper=Helper(), db_path=":memory:"
            )
            enricher.art_dir = art_dir
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(
                    lambda values: enricher._register_series_artwork(*values),
                    [
                        ("SpongeBob SquarePants", first, "test"),
                        ("The Blacklist", second, "test"),
                    ],
                ))
            self.assertEqual(enricher.series_artwork("SpongeBob SquarePants"), first)
            self.assertEqual(enricher.series_artwork("The Blacklist"), second)

    def test_series_artwork_index_persists_real_series_specific_frames(self):
        class Helper:
            def is_configured(self): return False
        with tempfile.TemporaryDirectory() as temp_dir:
            art_dir = Path(temp_dir) / "art"
            art_dir.mkdir()
            local_name = "b" * 64 + ".jpg"
            (art_dir / local_name).write_bytes(b"real frame")
            enricher = MetadataEnricher(
                helper=Helper(), fallback_helper=Helper(), db_path=":memory:"
            )
            enricher.art_dir = art_dir
            with patch.object(enricher, "ensure_local_artwork", return_value=local_name):
                sponge = enricher.ensure_series_artwork(
                    "SpongeBob SquarePants", "/media/SpongeBob/S01E01.mkv"
                )
            self.assertEqual((art_dir / sponge).read_bytes(), b"real frame")
            self.assertEqual(
                enricher.series_artwork("SpongeBob SquarePants"), sponge
            )
            index = json.loads((art_dir / "series-index.json").read_text())
            self.assertEqual(index["spongebobsquarepants"]["file"], sponge)

    def test_scan_caches_fallback_artwork_without_tmdb(self):
        class Helper:
            def is_configured(self):
                return False

            def search_tv(self, _title):
                return None

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "meta.db")
            media_path = os.path.realpath(Path(temp_dir) / "Show S01E01.mkv")
            with connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE file_meta (path TEXT PRIMARY KEY, meta TEXT, "
                    "media_type TEXT, last_checked TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO file_meta(path, meta, media_type) "
                    "VALUES (?, '{}', 'video')",
                    (media_path,),
                )
            helper = Helper()
            enricher = MetadataEnricher(
                helper=helper, fallback_helper=helper, db_path=db_path
            )
            enricher.art_dir = Path(temp_dir) / "art"
            artwork_name = "a" * 64 + ".jpg"
            with patch.object(
                enricher, "ensure_series_artwork", return_value=artwork_name
            ):
                stats = enricher.scan([media_path])
            with connect(db_path) as connection:
                metadata = json.loads(
                    connection.execute(
                        "SELECT meta FROM file_meta WHERE path=?", (media_path,)
                    ).fetchone()[0]
                )
            self.assertTrue(stats["unconfigured"])
            self.assertEqual(stats["updated"], 1)
            self.assertEqual(metadata["artwork_file"], artwork_name)
            self.assertEqual(metadata["artwork_source"], "representative-series-frame")

    def test_scan_replaces_legacy_episode_still_with_series_artwork(self):
        class Helper:
            def is_configured(self): return False
            def search_tv(self, _title): return None

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "meta.db")
            media_path = os.path.realpath(Path(temp_dir) / "SpongeBob S01E01.mkv")
            legacy = "a" * 64 + ".jpg"
            canonical = "b" * 64 + ".jpg"
            with connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE file_meta (path TEXT PRIMARY KEY, meta TEXT, "
                    "media_type TEXT, last_checked TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO file_meta(path, meta, media_type) VALUES (?, ?, 'video')",
                    (media_path, json.dumps({"type": "episode", "artwork_file": legacy})),
                )
            enricher = MetadataEnricher(
                helper=Helper(), fallback_helper=Helper(), db_path=db_path
            )
            enricher.art_dir = Path(temp_dir) / "art"
            enricher.art_dir.mkdir()
            (enricher.art_dir / legacy).write_bytes(b"old episode still")
            with patch.object(
                enricher, "ensure_series_artwork", return_value=canonical
            ):
                enricher.scan([media_path])
            with connect(db_path) as connection:
                metadata = json.loads(connection.execute(
                    "SELECT meta FROM file_meta WHERE path=?", (media_path,)
                ).fetchone()[0])
            self.assertEqual(metadata["series_artwork_file"], canonical)
            self.assertEqual(metadata["artwork_file"], canonical)

    def test_local_still_is_extracted_once_and_reused_without_tmdb(self):
        class Helper:
            def is_configured(self):
                return False

        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "Show S01E01.mkv"
            media.write_bytes(b"video fixture")
            enricher = MetadataEnricher(
                helper=Helper(), db_path=str(Path(temp_dir) / "meta.db")
            )
            enricher.art_dir = Path(temp_dir) / "art"

            def create_still(command, **_kwargs):
                Path(command[-1]).write_bytes(b"jpeg fixture")
                return SimpleNamespace(returncode=0)

            with patch(
                "fs42.metadata_enrichment.subprocess.run",
                side_effect=create_still,
            ) as run:
                first = enricher.ensure_local_artwork(str(media))
                second = enricher.ensure_local_artwork(str(media))

            self.assertEqual(first, second)
            self.assertRegex(first, r"^[a-f0-9]{64}\.jpg$")
            self.assertEqual((enricher.art_dir / first).read_bytes(), b"jpeg fixture")
            run.assert_called_once()

    def test_episode_scan_uses_english_series_identity_and_episode_details(self):
        class Helper:
            def is_configured(self):
                return True

            def search_tv(self, title):
                self.query = title
                return {
                    "tmdb_id": 1429,
                    "name": "Attack on Titan",
                    "original_name": "進撃の巨人",
                    "overview": "Humanity shelters behind walls.",
                    "first_air_date": "2013-04-07",
                    "genre": ["Animation", "Action & Adventure"],
                    "poster_url": None,
                    "backdrop_url": None,
                }

            def get_tv_episode(self, tmdb_id, season, episode):
                self.episode_request = (tmdb_id, season, episode)
                return {
                    "name": "To You, in 2000 Years",
                    "overview": "The Colossal Titan appears.",
                    "air_date": "2013-04-07",
                    "still_url": None,
                }

        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = str(Path(temp_dir) / "meta.db")
            media_path = str(
                Path(temp_dir)
                / "Shingeki No Kyojin"
                / "Season 01"
                / "Shingeki No Kyojin S01E01.mkv"
            )
            with connect(db_path) as connection:
                connection.execute(
                    "CREATE TABLE file_meta (path TEXT PRIMARY KEY, meta TEXT, "
                    "media_type TEXT, last_checked TIMESTAMP)"
                )
                connection.execute(
                    "INSERT INTO file_meta(path, meta, media_type) VALUES (?, '', 'video')",
                    (os.path.realpath(media_path),),
                )
            helper = Helper()
            enricher = MetadataEnricher(helper=helper, db_path=db_path)
            enricher.art_dir = Path(temp_dir) / "art"
            stats = enricher.scan([media_path])
            with connect(db_path) as connection:
                metadata = json.loads(
                    connection.execute(
                        "SELECT meta FROM file_meta WHERE path=?",
                        (os.path.realpath(media_path),),
                    ).fetchone()[0]
                )
            self.assertEqual(stats["updated"], 1)
            self.assertEqual(metadata["show_title"], "Attack on Titan")
            self.assertEqual(metadata["original_show_title"], "進撃の巨人")
            self.assertEqual(metadata["title"], "To You, in 2000 Years")
            self.assertEqual(metadata["season"], 1)
            self.assertEqual(metadata["episode"], 1)
            self.assertEqual(helper.episode_request, (1429, 1, 1))

    def test_tvmaze_fills_the_blacklist_when_tmdb_misses(self):
        class Primary:
            def is_configured(self): return True
            def search_tv(self, _title): return None

        class Fallback:
            def search_tv(self, title):
                self.query = title
                return {
                    "tvmaze_id": 69, "name": "The Blacklist",
                    "original_name": "The Blacklist",
                    "overview": "A wanted fugitive offers to help the FBI.",
                    "first_air_date": "2013-09-23", "genre": ["Drama"],
                    "poster_url": None, "backdrop_url": None,
                    "metadata_source": "tvmaze",
                }
            def get_tv_episode(self, show_id, season, episode):
                self.episode_request = (show_id, season, episode)
                return {"name": "Pilot", "overview": "Red surrenders.", "air_date": "2013-09-23"}

        fallback = Fallback()
        enricher = MetadataEnricher(
            helper=Primary(), fallback_helper=fallback, db_path=":memory:"
        )
        result = enricher._enrich(
            "/media/The Blacklist/Season 01/The Blacklist S01E01.mkv", {}
        )
        self.assertEqual(result["show_title"], "The Blacklist")
        self.assertEqual(result["title"], "Pilot")
        self.assertEqual(result["metadata_source"], "tvmaze")
        self.assertEqual(fallback.episode_request, (69, 1, 1))

    def test_tvmaze_adapter_prefers_exact_the_blacklist_match(self):
        response = MagicMock()
        response.json.return_value = [
            {"score": .9, "show": {"id": 1, "name": "Blacklist", "genres": [], "image": None}},
            {"score": .8, "show": {"id": 69, "name": "The Blacklist", "genres": ["Drama"], "image": {"original": "https://img.test/blacklist.jpg"}}},
        ]
        session = MagicMock()
        session.headers = {}
        session.get.return_value = response
        result = TVmazeHelper(session=session).search_tv("The Blacklist")
        self.assertEqual(result["tvmaze_id"], 69)
        self.assertEqual(result["backdrop_url"], "https://img.test/blacklist.jpg")


class WatchAPITests(unittest.TestCase):
    def setUp(self):
        manager = SimpleNamespace(
            resolver=SimpleNamespace(
                channels=lambda: [{
                    "channel_number": "42",
                    "channel_name": "Test TV",
                }]
            ),
            create=lambda channel, profile, **_kwargs: (_ for _ in ()).throw(
                ValueError("Unknown client profile")
            ),
        )
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager))
        )

    def test_channel_list_does_not_expose_paths(self):
        response = asyncio.run(channel_endpoint(self.request))
        self.assertNotIn("path", str(response))

    def test_channel_prewarm_releases_lease_but_keeps_broadcast_available(self):
        session = SimpleNamespace(session_id="warm-session")
        manager = SimpleNamespace(
            resolver=SimpleNamespace(
                station=lambda _channel: {"network_type": "scheduled"}
            ),
            create=MagicMock(return_value=(session, SimpleNamespace())),
            delete=MagicMock(return_value=True),
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager))
        )
        result = asyncio.run(prewarm_endpoint("42", request))
        self.assertTrue(result["prewarmed"])
        manager.create.assert_called_once_with(
            "42", "auto", subtitle_mode="auto"
        )
        manager.delete.assert_called_once_with("warm-session")

    def test_artwork_endpoint_serves_only_preindexed_local_art(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "Movie.mkv"
            filename = "e" * 64 + ".jpg"
            poster = Path(temp_dir) / filename
            media.touch()
            poster.write_bytes(b"image")
            airing = SimpleNamespace(identity_path=str(media), media_path=str(media))
            resolver = SimpleNamespace(
                now=lambda _channel, _at: airing,
                station=lambda _channel: {"network_name": "Test TV"},
                _approved_media_path=lambda _station, path: path,
            )
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        hls_sessions=SimpleNamespace(resolver=resolver)
                    )
                )
            )
            with patch(
                "fs42.fs42_server.api.watch.artwork_record",
                return_value={"file": filename},
            ), patch(
                "fs42.fs42_server.api.watch.artwork_root",
                return_value=Path(temp_dir),
            ):
                response = asyncio.run(artwork_endpoint("42", request))
            self.assertEqual(Path(response.path), poster)
            self.assertIn("immutable", response.headers["cache-control"])

    def test_now_payload_uses_series_and_episode_identity(self):
        when = dt.datetime(2026, 7, 31, 12, 0)
        airing = Airing(
            "3",
            "After Hours",
            "S01E10 Keeping Up With Our Joneses",
            "S01E10 Keeping Up With Our Joneses",
            when,
            when + dt.timedelta(minutes=30),
            when,
            when + dt.timedelta(minutes=22),
            "/media/King of the Hill (1997)/Season 01/"
            "S01E10 Keeping Up With Our Joneses.mkv",
            0,
            1800,
            1320,
        )
        with patch(
            "fs42.fs42_server.api.watch.MetadataIO.read", return_value=None
        ):
            payload = _now_payload(airing, when)
        self.assertEqual(payload["channel_name"], "After Hours")
        self.assertEqual(payload["program_title"], "King of the Hill")
        self.assertEqual(
            payload["program_details"],
            "S1E10: Keeping Up with Our Joneses",
        )

    def test_invalid_profile_returns_validation_error(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(create_session_endpoint(
                SessionRequest(channel="42", profile="unsupported"),
                self.request,
            ))
        self.assertEqual(raised.exception.status_code, 422)


class TVGuideAPITests(unittest.TestCase):
    def test_compact_program_keeps_summary_and_timeline_geometry(self):
        start = dt.datetime(2026, 8, 1, 16, 0)
        program = SimpleNamespace(
            start_time=start - dt.timedelta(minutes=10),
            end_time=start + dt.timedelta(minutes=20),
            meta={"plot": "A complete episode summary."},
        )
        compact = _compact_tv_program(program, start)
        self.assertEqual(compact["guide_start_minute"], -10)
        self.assertEqual(compact["guide_end_minute"], 20)
        self.assertEqual(compact["program_description"], "A complete episode summary.")
        self.assertNotIn("meta", compact)

    def test_roku_reference_grid_clips_current_program_at_left_edge(self):
        self.assertEqual(guide_card_rect(-900, 900, 24), (0, 114))

    def test_roku_reference_grid_sizes_short_episode_and_long_movie(self):
        short = guide_card_rect(0, 900, 0)
        movie = guide_card_rect(-7200, 3600, 0)
        self.assertEqual(short, (0, 118))
        self.assertEqual(movie, (0, 490))

    def test_roku_reference_grid_rejects_past_and_future_offscreen_cards(self):
        self.assertIsNone(guide_card_rect(-3600, -1, 0))
        self.assertIsNone(guide_card_rect(10_801, 12_000, 0))

    def test_roku_reference_grid_moves_future_window_deterministically(self):
        self.assertEqual(guide_card_rect(12_000, 13_800, 10_200), (248, 242))

    def test_live_news_row_fills_window_and_keeps_guide_description(self):
        start = dt.datetime(2026, 8, 1, 16, 0)
        program = {
            "start_time": start,
            "end_time": start + dt.timedelta(hours=6),
            "title": "ABC News Live",
            "program_details": "Live news coverage",
            "artwork_url": "/static/news/abc.jpg",
        }
        compact = _compact_tv_program(program, start)
        self.assertEqual(compact["program_description"], "Live news coverage")
        self.assertEqual(compact["guide_start_second"], 0)
        self.assertEqual(compact["guide_end_second"], 21_600)
        self.assertEqual(guide_card_rect(0, 21_600, 0), (0, 1_484))

    def test_roku_payload_precomputes_pixels_relative_to_now(self):
        start = dt.datetime(2026, 8, 1, 16, 44)
        program = {
            "start_time": start - dt.timedelta(minutes=2),
            "end_time": start + dt.timedelta(minutes=5),
            "title": "Schoolhouse Rock",
        }
        compact = _compact_tv_program(program, start, current_offset_second=45)
        self.assertEqual(compact["roku_start_pixel"], -22)
        self.assertEqual(compact["roku_end_pixel"], 35)
        self.assertEqual(roku_card_rect(-22, 35), (0, 72))

    def test_roku_runtime_fixture_has_visible_card_on_every_guide_row(self):
        # Mirrors the reported TV at 4:45: current episodes begin before NOW,
        # upcoming episodes and a long movie extend beyond it, and news is live.
        rows = [
            [(-22, 35), (35, 242), (242, 490)],
            [(-124, 83), (83, 331)],
            [(-455, 372), (372, 869)],
            [(-620, 124), (124, 621)],
            [(-41, 207), (207, 455)],
            [(-1_430, 91), (91, 1_490)],
            [(-4_000, 4_000)],
        ]
        visible = [[roku_card_rect(*card) for card in row] for row in rows]
        self.assertTrue(all(any(card is not None for card in row) for row in visible))
        self.assertEqual(visible[-1][0], (0, 1_484))


class BuildOperationTests(unittest.TestCase):
    def tearDown(self):
        if build_api.operation_lock.locked():
            build_api.operation_lock.release()

    def test_overlapping_operation_is_rejected(self):
        build_api.operation_lock.acquire()
        with self.assertRaises(HTTPException) as raised:
            build_api._begin_operation()
        self.assertEqual(raised.exception.status_code, 409)

    def test_failed_rebuild_reaches_error_and_releases_lock(self):
        station = {
            "network_name": "Test TV",
            "_has_catalog": True,
            "_has_schedule": False,
        }
        manager = SimpleNamespace(
            stations=[station],
            station_by_name=lambda name: station if name == "Test TV" else None,
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(player_command_queue=None)
            )
        )
        with (
            patch("fs42.fs42_server.api.build.StationManager", return_value=manager),
            patch("fs42.fs42_server.api.build.CatalogAPI.delete_catalog"),
            patch(
                "fs42.fs42_server.api.build.ShowCatalog",
                side_effect=RuntimeError("forced rebuild failure"),
            ),
        ):
            response = asyncio.run(
                build_api.quick_action("rebuild", "Test TV", request)
            )
            task_id = response["task_id"]
            for _ in range(100):
                task = asyncio.run(build_api.quick_action_status(task_id))
                if task["status"] == "error":
                    break
                time.sleep(0.01)
        self.assertEqual(task["status"], "error")
        self.assertIn("forced rebuild failure", task["log"])
        self.assertTrue(build_api.operation_lock.acquire(blocking=False))

    def test_rebuild_and_week_generates_initial_schedule(self):
        station = {
            "network_name": "Test TV",
            "_has_catalog": True,
            "_has_schedule": False,
        }
        manager = SimpleNamespace(
            stations=[station],
            station_by_name=lambda name: station if name == "Test TV" else None,
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(player_command_queue=None)
            )
        )
        with (
            patch("fs42.fs42_server.api.build.StationManager", return_value=manager),
            patch("fs42.fs42_server.api.build.CatalogAPI.delete_catalog"),
            patch("fs42.fs42_server.api.build.ShowCatalog"),
            patch("fs42.fs42_server.api.build._scan_metadata"),
            patch("fs42.fs42_server.api.build.prepare_all_series", return_value={"state": "ready", "indexed": 1}),
            patch("fs42.fs42_server.api.build.LiquidManager.reload_schedules"),
            patch("fs42.fs42_server.api.build.LiquidSchedule") as schedule,
        ):
            response = asyncio.run(
                build_api.quick_action("rebuild_and_week", "Test TV", request)
            )
            for _ in range(100):
                task = asyncio.run(
                    build_api.quick_action_status(response["task_id"])
                )
                if task["status"] in {"done", "error"}:
                    break
                time.sleep(0.01)
        self.assertEqual(task["status"], "done")
        schedule.return_value.add_amount.assert_called_once_with("week")


if __name__ == "__main__":
    unittest.main()
