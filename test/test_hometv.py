import datetime as dt
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
from fs42.fs42_server.api.watch import (
    PLAYLIST_STARTUP_ATTEMPTS,
    PLAYLIST_STARTUP_INTERVAL,
    PLAYLIST_READY_SEGMENTS,
    SessionRequest,
    _now_payload,
    artwork as artwork_endpoint,
    channels as channel_endpoint,
    create_session as create_session_endpoint,
)
from fs42.fs42_server.api import build as build_api
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


class WatchAPITests(unittest.TestCase):
    def setUp(self):
        manager = SimpleNamespace(
            resolver=SimpleNamespace(
                channels=lambda: [{
                    "channel_number": "42",
                    "channel_name": "Test TV",
                }]
            ),
            create=lambda channel, profile: (_ for _ in ()).throw(
                ValueError("Unknown client profile")
            ),
        )
        self.request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager))
        )

    def test_channel_list_does_not_expose_paths(self):
        response = asyncio.run(channel_endpoint(self.request))
        self.assertNotIn("path", str(response))

    def test_artwork_endpoint_serves_only_discovered_local_art(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "Movie.mkv"
            poster = Path(temp_dir) / "Movie.jpg"
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
            response = asyncio.run(artwork_endpoint("42", request))
            self.assertEqual(Path(response.path), poster)

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
