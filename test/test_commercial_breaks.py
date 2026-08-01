import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fs42.block_plan import BlockPlanEntry
from fs42.catalog import ShowCatalog
from fs42.catalog_entry import CatalogEntry
from fs42.fluid_builder import FluidBuilder
from fs42.fluid_statements import FluidStatements
from fs42.database import connect
from fs42.media_processor import MediaProcessor
from fs42.reel_cutter import ReelCutter
from fs42.liquid_blocks import LiquidBlock
from fs42.liquid_schedule import LiquidSchedule


class CommercialBreakSelectionTests(unittest.TestCase):
    def test_movie_library_path_is_identified_without_metadata(self):
        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"schedule_increment": 30}
        movie = SimpleNamespace(
            path="/media/Movies/The Feature (2026).mkv",
            realpath="/media/Movies/The Feature (2026).mkv",
            duration=7_500,
        )
        with patch("fs42.liquid_schedule.MetadataIO.read", return_value=None):
            self.assertTrue(schedule._candidate_is_movie(movie))

    def test_movie_metadata_disables_padding_outside_movies_folder(self):
        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"schedule_increment": 30}
        movie = SimpleNamespace(
            path="/media/Specials/The Feature.mkv",
            realpath="/media/Specials/The Feature.mkv",
            duration=7_500,
        )
        with patch(
            "fs42.liquid_schedule.MetadataIO.read",
            return_value={"type": "movie"},
        ):
            self.assertTrue(schedule._candidate_is_movie(movie))

    def test_movie_post_credit_padding_is_capped_at_ninety_seconds(self):
        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {"schedule_increment": 30}
        duration = 7_500
        self.assertEqual(schedule._movie_target_duration(duration), 7_590)
        self.assertEqual(schedule._calc_target_duration(duration), 9_000)

    def test_movie_padding_cap_is_configurable_but_bounded(self):
        schedule = LiquidSchedule.__new__(LiquidSchedule)
        schedule.conf = {
            "schedule_increment": 30,
            "movie_padding_seconds": 45,
        }
        self.assertEqual(schedule._movie_target_duration(7_500), 7_545)
        schedule.conf["movie_padding_seconds"] = 301
        with self.assertRaisesRegex(ValueError, "between 0 and 300"):
            schedule._movie_target_duration(7_500)

    def test_end_strategy_never_interrupts_movie_for_commercials(self):
        movie = SimpleNamespace(
            path="/media/Movies/The Feature.mkv",
            duration=7_500,
            content_type="feature",
            media_type="video",
        )
        commercial = SimpleNamespace(
            make_plan=lambda: [
                BlockPlanEntry(
                    "/ads/ad.mkv", 0, 30, content_type="commercial"
                )
            ]
        )
        plan = ReelCutter.cut_reels_into_base(
            base_clip=movie,
            reel_blocks=[commercial],
            base_offset=0,
            base_duration=movie.duration,
            break_strategy="end",
            start_bump=None,
            end_bump=None,
            break_points=[
                {"chapter_start": 0, "chapter_end": 3_600},
                {"chapter_start": 3_600, "chapter_end": 7_500},
            ],
        )
        self.assertEqual(
            [(item.path, item.content_type) for item in plan],
            [
                (movie.path, "feature"),
                ("/ads/ad.mkv", "commercial"),
            ],
        )
    def test_exact_length_commercial_fills_gap_without_dead_air(self):
        catalog = ShowCatalog.__new__(ShowCatalog)
        exact = CatalogEntry("/ads/exact.mkv", 30, "commercials")
        catalog.clip_index = {"commercials": [exact]}
        catalog.config = {}

        selected = catalog.find_candidate(
            "commercials", 30, __import__("datetime").datetime.now()
        )

        self.assertIs(selected, exact)

    def test_exact_length_bumper_fills_gap_without_dead_air(self):
        catalog = ShowCatalog.__new__(ShowCatalog)
        exact = CatalogEntry("/bumps/exact.mkv", 10, "bumps")
        catalog.clip_index = {"bumps": [exact]}
        catalog.config = {}

        selected = catalog.find_bump(
            10, __import__("datetime").datetime.now(), bump_tag="bumps"
        )

        self.assertIs(selected, exact)

    def test_current_detector_invalidates_pre_optimization_cache(self):
        self.assertEqual(FluidBuilder.COMMERCIAL_BREAK_DETECTOR_VERSION, 8)

    def test_breaks_are_rejected_within_first_or_last_three_minutes(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 150, "title": "Act 1"},
            {"chapter_start": 150, "chapter_end": 600, "title": "Act 2"},
            {"chapter_start": 600, "chapter_end": 1170, "title": "Act 3"},
            {"chapter_start": 1170, "chapter_end": 1320, "title": "Credits"},
        ]
        segments = MediaProcessor.safe_commercial_segments(
            chapters, 1320, chapters
        )
        self.assertEqual(
            [(item["chapter_start"], item["chapter_end"]) for item in segments],
            [(0.0, 600.0), (600.0, 1320.0)],
        )

    def test_commercial_scan_cache_validates_file_identity_and_version(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            connection = connect(str(Path(temp_dir) / "cache.db"))
            try:
                FluidStatements.init_db(connection)
                connection.commit()
                points = [{"chapter_start": 0, "chapter_end": 1320}]
                FluidStatements.add_commercial_break_scan(
                    connection, "/media/show.mkv", 2, 1234, 5678, points
                )
                connection.commit()
                self.assertEqual(
                    FluidStatements.get_commercial_break_scan(
                        connection, "/media/show.mkv", 2, 1234, 5678
                    ),
                    points,
                )
                self.assertIsNone(
                    FluidStatements.get_commercial_break_scan(
                        connection, "/media/show.mkv", 2, 1235, 5678
                    )
                )
                self.assertIsNone(
                    FluidStatements.get_commercial_break_scan(
                        connection, "/media/show.mkv", 3, 1234, 5678
                    )
                )
            finally:
                connection.close()

    def test_black_detection_decodes_only_chapter_windows(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 700},
            {"chapter_start": 700, "chapter_end": 1320},
        ]
        result = SimpleNamespace(
            stderr=(
                "[blackdetect] black_start:1.5 black_end:2.5 "
                "black_duration:1.0\n"
            )
        )
        with patch(
            "fs42.media_processor.subprocess.run", return_value=result
        ) as run:
            segments = MediaProcessor.black_detect_at_chapters(
                "/media/show.mkv", 1320, chapters
            )

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            command = call.args[0]
            self.assertEqual(command[command.index("-t") + 1], "4.000")
            self.assertEqual(command[command.index("-threads") + 1], "1")
            self.assertEqual(command[command.index("-filter_threads") + 1], "1")
        self.assertEqual(
            [(item["chapter_start"], item["chapter_end"]) for item in segments],
            [(0.0, 300.0), (300.0, 700.0), (700.0, 1320.0)],
        )

    def test_scan_thread_limit_is_validated(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 1320},
        ]
        with (
            patch.dict("os.environ", {"FS42_SCAN_THREADS": "12"}),
            self.assertRaisesRegex(ValueError, "between 1 and 4"),
        ):
            MediaProcessor.black_detect_at_chapters(
                "/media/show.mkv", 1320, chapters
            )

    def test_scan_concurrency_limit_is_validated(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 1320},
        ]
        with (
            patch.dict("os.environ", {"FS42_SCAN_CONCURRENCY": "12"}),
            self.assertRaisesRegex(ValueError, "between 1 and 4"),
        ):
            MediaProcessor.black_detect_at_chapters(
                "/media/show.mkv", 1320, chapters
            )

    def test_chapter_window_scan_has_a_hard_timeout(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 1320},
        ]
        with patch(
            "fs42.media_processor.subprocess.run",
            return_value=SimpleNamespace(stderr=""),
        ) as run:
            MediaProcessor.black_detect_at_chapters(
                "/media/show.mkv", 1320, chapters
            )
        self.assertEqual(run.call_args.kwargs["timeout"], 30)

    def test_safe_breaks_ignore_opening_credits_and_nearby_black_frames(self):
        detected = [
            {"chapter_start": 0, "chapter_end": 30},
            {"chapter_start": 30, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 320},
            {"chapter_start": 320, "chapter_end": 700},
            {"chapter_start": 700, "chapter_end": 1250},
            {"chapter_start": 1250, "chapter_end": 1320},
        ]
        chapters = [
            {"chapter_start": 0, "chapter_end": 300, "title": "Act 1"},
            {"chapter_start": 300, "chapter_end": 700, "title": "Act 2"},
            {"chapter_start": 700, "chapter_end": 1100, "title": "Act 3"},
            {"chapter_start": 1100, "chapter_end": 1320, "title": "Credits"},
        ]

        segments = MediaProcessor.safe_commercial_segments(
            detected, 1320, chapters
        )

        self.assertEqual(
            [(item["chapter_start"], item["chapter_end"]) for item in segments],
            [(0.0, 300.0), (300.0, 700.0), (700.0, 1320.0)],
        )
        self.assertEqual(sum(item["segment_duration"] for item in segments), 1320)

    def test_black_frame_without_chapter_evidence_is_not_a_break(self):
        black = [
            {"chapter_start": 0, "chapter_end": 420},
            {"chapter_start": 420, "chapter_end": 1320},
        ]
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 1320},
        ]
        self.assertEqual(
            MediaProcessor.safe_commercial_segments(black, 1320, chapters),
            [],
        )

    def test_all_independently_verified_boundaries_are_kept(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 300},
            {"chapter_start": 300, "chapter_end": 600},
            {"chapter_start": 600, "chapter_end": 900},
            {"chapter_start": 900, "chapter_end": 1320},
        ]
        segments = MediaProcessor.safe_commercial_segments(
            chapters, 1320, chapters
        )
        self.assertEqual(
            [item["chapter_end"] for item in segments[:-1]],
            [300.0, 600.0, 900.0],
        )

    def test_half_hour_breaks_are_capped_at_three(self):
        boundaries = [0, 180, 360, 540, 720, 900, 1080, 1320]
        segments = [
            {
                "chapter_start": boundaries[index],
                "chapter_end": boundaries[index + 1],
            }
            for index in range(len(boundaries) - 1)
        ]
        capped = MediaProcessor.cap_commercial_segments(segments, 1320)
        self.assertEqual(len(capped) - 1, 3)
        self.assertEqual(capped[0]["chapter_start"], 0.0)
        self.assertEqual(capped[-1]["chapter_end"], 1320.0)

    def test_cap_does_not_require_or_invent_breaks(self):
        segments = [
            {"chapter_start": 0, "chapter_end": 600},
            {"chapter_start": 600, "chapter_end": 1320},
        ]
        capped = MediaProcessor.cap_commercial_segments(segments, 1320)
        self.assertEqual(
            [(item["chapter_start"], item["chapter_end"]) for item in capped],
            [(0, 600), (600, 1320)],
        )

    def test_action_chapter_is_not_mistaken_for_explicit_act_marker(self):
        chapters = [
            {"chapter_start": 0, "chapter_end": 400, "title": "Action Scene"},
            {"chapter_start": 400, "chapter_end": 1320, "title": "Characters"},
        ]
        self.assertEqual(
            MediaProcessor.safe_commercial_segments([], 1320, chapters),
            [],
        )

    def test_credits_chapter_is_not_a_break_even_when_black(self):
        black = [
            {"chapter_start": 0, "chapter_end": 1000},
            {"chapter_start": 1000, "chapter_end": 1500},
        ]
        chapters = [
            {"chapter_start": 0, "chapter_end": 1000, "title": "Final Act"},
            {"chapter_start": 1000, "chapter_end": 1500, "title": "End Credits"},
        ]
        self.assertEqual(
            MediaProcessor.safe_commercial_segments(black, 1500, chapters),
            [],
        )

    def test_no_safe_boundary_does_not_cut_the_feature(self):
        base = SimpleNamespace(
            path="/media/show.mkv",
            duration=1320,
            content_type="feature",
            media_type="video",
        )

        def reel(path):
            return SimpleNamespace(
                make_plan=lambda: [
                    BlockPlanEntry(
                        path,
                        0,
                        15,
                        content_type="bump",
                        media_type="video",
                    )
                ]
            )

        plan = ReelCutter.cut_reels_into_base(
            base_clip=base,
            reel_blocks=[reel("/bumps/black-1.mkv"), reel("/bumps/black-2.mkv")],
            base_offset=0,
            base_duration=1320,
            break_strategy="standard",
            start_bump=None,
            end_bump=None,
            break_points=[],
        )

        self.assertEqual(
            [(item.path, item.skip, item.duration) for item in plan],
            [
                ("/media/show.mkv", 0, 1320),
                ("/bumps/black-1.mkv", 0, 15),
                ("/bumps/black-2.mkv", 0, 15),
            ],
        )

    def test_schedule_does_not_fall_back_to_legacy_black_frames(self):
        content = SimpleNamespace(
            realpath="/media/show.mkv",
            path="/media/show.mkv",
            title="Show",
            duration=1320,
            content_type="feature",
            media_type="video",
        )
        start = __import__("datetime").datetime(2026, 7, 31, 12, 0)
        block = LiquidBlock(
            content,
            start,
            start + __import__("datetime").timedelta(seconds=1380),
            break_info={"break_duration": 30},
        )
        catalog = MagicMock()
        catalog.config = {"break_duration": 30}
        catalog.make_reel_fill.return_value = []
        fluid = MagicMock()
        fluid.get_chapters.return_value = []
        with (
            patch("fs42.liquid_blocks.FluidBuilder", return_value=fluid),
        ):
            block.make_plan(catalog)
        fluid.get_breaks.assert_not_called()
        feature_entries = [
            entry for entry in block.plan if entry.path == content.path
        ]
        self.assertEqual(len(feature_entries), 1)
        self.assertEqual(feature_entries[0].duration, 1320)

    def test_black_bumpers_are_not_analyzed_as_feature_breaks(self):
        builder = FluidBuilder.__new__(FluidBuilder)
        builder.db_path = "unused.db"
        builder._l = logging.getLogger("test")
        connection = MagicMock()
        connection.cursor.return_value.fetchone.return_value = None
        bumper = SimpleNamespace(
            realpath="/bumps/adult-swim-black.mkv",
            duration=15,
            content_type="bump",
        )

        with (
            patch("fs42.fluid_builder.connect", return_value=connection),
            patch.object(MediaProcessor, "black_detect") as black_detect,
            patch.object(MediaProcessor, "chapter_detect") as chapter_detect,
            patch(
                "fs42.fluid_builder.FluidStatements.add_chapter_points"
            ) as store,
        ):
            builder.scan_chapters_for_entries([bumper])

        black_detect.assert_not_called()
        chapter_detect.assert_not_called()
        store.assert_called_once_with(connection, bumper.realpath, [])

    def test_unchanged_feature_reuses_cached_commercial_boundaries(self):
        builder = FluidBuilder.__new__(FluidBuilder)
        builder.db_path = "unused.db"
        builder._l = logging.getLogger("test")
        connection = MagicMock()
        cached = [
            {"chapter_start": 0, "chapter_end": 600},
            {"chapter_start": 600, "chapter_end": 1320},
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            feature = SimpleNamespace(
                realpath=str(media),
                duration=1320,
                content_type="feature",
            )
            with (
                patch("fs42.fluid_builder.connect", return_value=connection),
                patch(
                    "fs42.fluid_builder.FluidStatements."
                    "get_commercial_break_scan",
                    return_value=cached,
                ),
                patch.object(MediaProcessor, "black_detect_at_chapters") as black,
                patch.object(MediaProcessor, "chapter_detect") as chapters,
                patch(
                    "fs42.fluid_builder.FluidStatements.add_chapter_points"
                ) as store,
            ):
                builder.scan_chapters_for_entries([feature])

        black.assert_not_called()
        chapters.assert_not_called()
        store.assert_called_once_with(connection, feature.realpath, cached)
        connection.commit.assert_called()

    def test_authored_act_chapters_skip_ffmpeg_analysis(self):
        builder = FluidBuilder.__new__(FluidBuilder)
        builder.db_path = "unused.db"
        builder._l = logging.getLogger("test")
        connection = MagicMock()
        connection.cursor.return_value.fetchone.return_value = None
        with tempfile.TemporaryDirectory() as temp_dir:
            media = Path(temp_dir) / "show.mkv"
            media.touch()
            feature = SimpleNamespace(
                realpath=str(media),
                duration=1320,
                content_type="feature",
            )
            chapters = [
                {"chapter_start": 0, "chapter_end": 300, "title": "Act 1"},
                {"chapter_start": 300, "chapter_end": 700, "title": "Act 2"},
                {"chapter_start": 700, "chapter_end": 1320, "title": "Act 3"},
            ]
            with (
                patch("fs42.fluid_builder.connect", return_value=connection),
                patch(
                    "fs42.fluid_builder.FluidStatements."
                    "get_commercial_break_scan",
                    return_value=None,
                ),
                patch.object(
                    MediaProcessor, "chapter_detect", return_value=chapters
                ),
                patch.object(
                    MediaProcessor, "black_detect_at_chapters"
                ) as black,
                patch(
                    "fs42.fluid_builder.FluidStatements."
                    "add_commercial_break_scan"
                ) as store_scan,
            ):
                builder.scan_chapters_for_entries([feature])

        black.assert_not_called()
        stored = store_scan.call_args.args[-1]
        self.assertEqual(
            [(item["chapter_start"], item["chapter_end"]) for item in stored],
            [(0.0, 300.0), (300.0, 700.0), (700.0, 1320.0)],
        )


if __name__ == "__main__":
    unittest.main()
