import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fs42.hometv import ScheduleResolver
from fs42.live_news import (
    DISCOVERY_CACHE, SOURCES, artwork_svg, discover_live_video,
    discover_official_hls, now_payload, schedule_blocks, station_config,
    station_source,
)
from fs42.fs42_server.api.live_news import InstallRequest, install
from fs42.fs42_server.api.watch import SessionRequest, create_session


class FakeStationManager:
    def __init__(self, stations=None):
        self.stations = stations or []


class LiveNewsTests(unittest.IsolatedAsyncioTestCase):
    def station(self, item=None, channel=20):
        config = station_config(item or SOURCES[0], channel)["station_conf"]
        config.update(_has_schedule=False, _has_catalog=False)
        return config

    def test_curated_sources_are_unique_and_valid(self):
        self.assertEqual(len({item["id"] for item in SOURCES}), len(SOURCES))
        self.assertEqual(len({item["youtube_channel_id"] for item in SOURCES}), len(SOURCES))
        for item in SOURCES:
            self.assertEqual(station_source(self.station(item))["id"], item["id"])
            self.assertTrue(item["official_url"].startswith("https://"))

    def test_resolver_exposes_live_news_without_database_schedule(self):
        station = self.station()
        resolver = ScheduleResolver(FakeStationManager([station]))
        self.assertEqual(resolver.channels()[0]["channel_number"], "20")
        self.assertEqual(resolver.station("20")["network_type"], "live_news")

    def test_now_guide_and_artwork_are_immediate(self):
        station = self.station()
        moment = dt.datetime(2026, 8, 1, 12, 30)
        payload = now_payload(station, moment)
        self.assertTrue(payload["is_live"])
        self.assertEqual(payload["progress"], 0.5208333333333334)
        blocks = schedule_blocks(station, moment, moment + dt.timedelta(hours=4))
        self.assertEqual(blocks[0]["airing_kind"], "live")
        self.assertIn("ABC News Live", artwork_svg(station))

    async def test_session_returns_safe_embed_without_starting_ffmpeg(self):
        station = self.station()
        resolver = ScheduleResolver(FakeStationManager([station]))
        manager = SimpleNamespace(resolver=resolver)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager)))
        with patch(
            "fs42.fs42_server.api.watch.discover_live_video",
            return_value="ZvdiJUYGBis",
        ):
            result = await create_session(SessionRequest(channel="20"), request)
        self.assertEqual(result["playback_kind"], "embed")
        self.assertIsNone(result["session_id"])
        self.assertIn("ZvdiJUYGBis", result["embed_url"])

    async def test_session_uses_official_hls_when_youtube_is_not_live(self):
        station = self.station(SOURCES[1], 21)
        resolver = ScheduleResolver(FakeStationManager([station]))
        manager = SimpleNamespace(resolver=resolver)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager)))
        with (
            patch("fs42.fs42_server.api.watch.discover_live_video", return_value=None),
            patch(
                "fs42.fs42_server.api.watch.discover_official_hls",
                return_value="https://news.example.cbsivideo.com/index.m3u8",
            ),
        ):
            result = await create_session(SessionRequest(channel="21"), request)
        self.assertEqual(result["playback_kind"], "external_hls")
        self.assertIsNone(result["session_id"])
        self.assertTrue(result["playlist_url"].endswith("index.m3u8"))

    def test_discovers_concrete_video_from_official_live_command(self):
        station = self.station(SOURCES[-1], 23)
        response = MagicMock()
        response.text = (
            '<script>window[\'ytCommand\'] = {"watchEndpoint":'
            '{"videoId":"ZvdiJUYGBis"}};</script>'
        )
        response.raise_for_status.return_value = None
        DISCOVERY_CACHE.clear()
        with (
            patch("fs42.live_news.requests.get", return_value=response),
            patch("fs42.live_news._persist_video"),
        ):
            self.assertEqual(discover_live_video(station), "ZvdiJUYGBis")

    def test_discovers_only_approved_official_hls_host(self):
        station = self.station(SOURCES[1], 21)
        response = MagicMock()
        response.text = (
            'https://tracker.example/steal.m3u8 '
            'https://news.example.cbsivideo.com/index.m3u8'
        )
        response.raise_for_status.return_value = None
        with patch("fs42.live_news.requests.get", return_value=response):
            self.assertEqual(
                discover_official_hls(station),
                "https://news.example.cbsivideo.com/index.m3u8",
            )

    async def test_installer_is_collision_safe_and_idempotent(self):
        ordinary = {"network_name": "Existing", "channel_number": 20, "network_type": "standard"}
        manager = FakeStationManager([ordinary])
        writes = []
        def write(name, config, is_update=False):
            conf = config["station_conf"]
            writes.append((name, conf["channel_number"], is_update))
            manager.stations.append({**conf, "_has_schedule": False, "_has_catalog": False})
            return True, "ok", "unused"
        manager.write_station_config = write
        with patch("fs42.fs42_server.api.live_news.StationManager", return_value=manager):
            result = await install(InstallRequest(source_ids=[SOURCES[0]["id"]]))
        self.assertEqual(result["installed"][0]["channel"], 21)
        self.assertEqual(writes, [(SOURCES[0]["name"], 21, False)])

        writes.clear()
        with patch("fs42.fs42_server.api.live_news.StationManager", return_value=manager):
            result = await install(InstallRequest(source_ids=[SOURCES[0]["id"]]))
        self.assertEqual(result["installed"][0]["channel"], 21)
        self.assertEqual(writes[0][2], True)


class LiveNewsStaticContractTests(unittest.TestCase):
    def test_schema_accepts_live_news_configuration(self):
        import jsonschema
        with open("fs42/station_config_schema.json", encoding="utf-8") as handle:
            schema = json.load(handle)
        jsonschema.validate(station_config(SOURCES[0], 20), schema)

    def test_guide_micro_scrolls_without_red_marker_or_minute_reload(self):
        guide = open("fs42/fs42_server/static/guide.js", encoding="utf-8").read()
        frame = open("fs42/fs42_server/static/guide_frame.html", encoding="utf-8").read()
        self.assertIn("scroll.scrollLeft", guide)
        self.assertNotIn("setInterval(() => {\n    const expected", guide)
        self.assertNotIn('id="time-marker"', frame)
        self.assertNotIn('id="preview-fallback"', frame)
        self.assertNotIn("_series_placeholder", open(
            "fs42/fs42_server/api/watch.py", encoding="utf-8"
        ).read())
        self.assertIn('id="artwork-loading"', frame)

    def test_ad_prefetch_waits_for_media_not_just_manifest(self):
        watch = open("fs42/fs42_server/static/watch.js", encoding="utf-8").read()
        self.assertIn("Hls.Events.FRAG_BUFFERED", watch)
        self.assertIn("- 20_000", watch)


if __name__ == "__main__":
    unittest.main()
