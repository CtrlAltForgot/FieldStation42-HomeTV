import datetime as dt
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fs42.hometv import ScheduleResolver
from fs42.live_news import (
    DISCOVERY_CACHE, HLS_CACHE, SOURCES, artwork_svg, discover_live_video,
    discover_official_hls, discover_youtube_hls, now_payload, schedule_blocks, station_config,
    station_source,
)
from fs42.fs42_server.api.live_news import InstallRequest, install
from fs42.fs42_server.api.schedules import get_schedule_by_query
from fs42.fs42_server.api.tv import guide as tv_guide
from fs42.fs42_server.api.watch import _station_artwork_svg
from fs42.fs42_server.api.watch import SessionRequest, create_session, prewarm


class FakeStationManager:
    def __init__(self, stations=None):
        self.stations = stations or []

    def station_by_name(self, name):
        return next(
            (station for station in self.stations if station.get("network_name") == name),
            None,
        )


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
        ), patch(
            "fs42.fs42_server.api.watch.discover_direct_hls",
            return_value=None,
        ):
            result = await create_session(SessionRequest(channel="20"), request)
        self.assertEqual(result["playback_kind"], "embed")
        self.assertIsNone(result["session_id"])
        self.assertIn("ZvdiJUYGBis", result["embed_url"])

    async def test_live_news_prewarm_populates_direct_stream_cache(self):
        station = self.station()
        manager = SimpleNamespace(
            resolver=ScheduleResolver(FakeStationManager([station]))
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager))
        )
        with patch(
            "fs42.fs42_server.api.watch.discover_direct_hls",
            return_value="https://news.test/live.m3u8",
        ) as discover:
            result = await prewarm("20", request)
        self.assertTrue(result["prewarmed"])
        discover.assert_called_once_with(station, False)

    async def test_session_uses_official_hls_when_youtube_is_not_live(self):
        station = self.station(SOURCES[1], 21)
        resolver = ScheduleResolver(FakeStationManager([station]))
        manager = SimpleNamespace(resolver=resolver)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager)))
        with (
            patch(
                "fs42.fs42_server.api.watch.discover_direct_hls",
                return_value="https://news.example.cbsivideo.com/index.m3u8",
            ),
        ):
            result = await create_session(SessionRequest(channel="21"), request)
        self.assertEqual(result["playback_kind"], "external_hls")
        self.assertIsNone(result["session_id"])
        self.assertTrue(result["playlist_url"].endswith("index.m3u8"))

    async def test_roku_live_news_requires_native_hls_never_embed(self):
        station = self.station(SOURCES[0], 20)
        resolver = ScheduleResolver(FakeStationManager([station]))
        manager = SimpleNamespace(resolver=resolver)
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(hls_sessions=manager)))
        with (
            patch("fs42.fs42_server.api.watch.discover_direct_hls", return_value=None),
            patch("fs42.fs42_server.api.watch.discover_live_video") as embed,
        ):
            with self.assertRaises(Exception):
                await create_session(SessionRequest(channel="20", client="roku"), request)
        embed.assert_not_called()

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

    def test_direct_youtube_resolver_selects_highest_hls_quality(self):
        station = self.station(SOURCES[0], 20)
        resolver = MagicMock()
        resolver.__enter__.return_value.extract_info.return_value = {
            "formats": [
                {"protocol": "m3u8_native", "height": 720, "tbr": 1200, "url": "https://video.example/720.m3u8"},
                {"protocol": "m3u8_native", "height": 1080, "tbr": 2400, "url": "https://video.example/1080.m3u8"},
            ]
        }
        HLS_CACHE.clear()
        with patch("yt_dlp.YoutubeDL", return_value=resolver):
            self.assertEqual(
                discover_youtube_hls(station),
                "https://video.example/1080.m3u8",
            )

    async def test_cbs_slash_name_works_through_query_schedule_route(self):
        station = self.station(SOURCES[1], 21)
        with patch(
            "fs42.fs42_server.api.schedules.StationManager"
        ) as manager:
            manager.return_value.station_by_name.return_value = station
            result = await get_schedule_by_query(
                "CBS News 24/7",
                "2026-08-01T10:00:00",
                "2026-08-01T14:00:00",
            )
        self.assertEqual(result["network_name"], "CBS News 24/7")
        self.assertEqual(result["schedule_blocks"][0]["title"], "CBS News 24/7")

    async def test_tv_guide_includes_live_station_without_database_schedule(self):
        station = self.station(SOURCES[1], 21)
        manager = FakeStationManager([station])
        with patch("fs42.fs42_server.api.tv.StationManager", return_value=manager), patch(
            "fs42.fs42_server.api.schedules.StationManager", return_value=manager
        ):
            result = await tv_guide(6)
        self.assertEqual(result["channels"][0]["channel_name"], "CBS News 24/7")
        self.assertEqual(result["channels"][0]["programs"][0]["airing_kind"], "live")

    async def test_tv_guide_includes_every_visible_scheduleless_station_type(self):
        stations = [
            {
                "network_name": f"Test {kind}",
                "channel_number": channel,
                "network_type": kind,
                "_has_schedule": False,
                "_has_catalog": False,
            }
            for channel, kind in enumerate(
                ("guide", "web", "streaming"), start=30
            )
        ]
        manager = FakeStationManager(stations)
        with patch("fs42.fs42_server.api.tv.StationManager", return_value=manager):
            result = await tv_guide(6)
        self.assertEqual(
            [row["network_type"] for row in result["channels"]],
            ["guide", "web", "streaming"],
        )
        self.assertTrue(all(len(row["programs"]) == 1 for row in result["channels"]))
        self.assertTrue(all(not row["is_tunable"] for row in result["channels"]))
        self.assertEqual(
            [row["next_tunable_index"] for row in result["channels"]],
            [0, 1, 2],
        )
        self.assertEqual(
            result["channels"][0]["programs"][0]["program_details"],
            "Program guide",
        )

    async def test_tv_guide_links_playable_neighbors_around_guide_station(self):
        first = self.station(SOURCES[0], 20)
        guide_station = {
            "network_name": "Grandma Guide",
            "channel_number": 30,
            "network_type": "guide",
            "_has_schedule": False,
            "_has_catalog": False,
        }
        last = self.station(SOURCES[1], 40)
        manager = FakeStationManager([first, guide_station, last])
        with patch("fs42.fs42_server.api.tv.StationManager", return_value=manager), patch(
            "fs42.fs42_server.api.schedules.StationManager", return_value=manager
        ):
            result = await tv_guide(6)
        rows = result["channels"]
        self.assertEqual([row["is_tunable"] for row in rows], [True, False, True])
        self.assertEqual([row["next_tunable_index"] for row in rows], [2, 2, 0])
        self.assertEqual([row["previous_tunable_index"] for row in rows], [2, 0, 0])

    def test_resolver_lists_scheduleless_channels_and_artwork_is_channel_specific(self):
        station = {
            "network_name": "Grandma's Guide",
            "channel_number": 30,
            "network_type": "guide",
            "_has_schedule": False,
        }
        resolver = ScheduleResolver(FakeStationManager([station]))
        self.assertEqual(resolver.channels()[0]["network_type"], "guide")
        self.assertFalse(resolver.channels()[0]["is_tunable"])
        self.assertEqual(resolver.station("30"), station)
        artwork = _station_artwork_svg(station)
        self.assertIn("Grandma&#x27;s Guide", artwork)
        self.assertIn("CH 30", artwork)

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

    def test_guide_renders_without_catalog_wide_artwork_wait(self):
        guide = open("fs42/fs42_server/static/guide.js", encoding="utf-8").read()
        self.assertNotIn("prewarmArtwork", guide)
        self.assertNotIn("await prewarm", guide)
        self.assertIn("prefetchNearbyArtwork", guide)
        self.assertIn("block.artwork_url", guide)
        self.assertIn("state.artworkCache.set(key, blobUrl)", guide)
        self.assertNotIn('textContent = "Artwork loading"', guide)

    def test_tv_clients_use_program_artwork_and_bound_network_waits(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        request = open("clients/roku/components/RequestTask.brs", encoding="utf-8").read()
        manifest = open("clients/roku/manifest", encoding="utf-8").read()
        webos = open("clients/webos/app.js", encoding="utf-8").read()
        self.assertIn("requires_network=1", manifest)
        self.assertIn("Wait(15000, port)", request)
        self.assertIn("AsyncGetToString", request)
        self.assertIn("program.artwork_url", roku)
        self.assertIn("item.artwork_url", webos)
        self.assertNotIn('row.artwork_url + "?at="', roku)
        self.assertNotIn('channel.artwork_url +', webos)

    def test_tv_clients_render_real_proportional_timeline_grids(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        webos = open("clients/webos/app.js", encoding="utf-8").read()
        tv_api = open("fs42/fs42_server/api/tv.py", encoding="utf-8").read()
        self.assertIn("roku_start_pixel", roku)
        self.assertIn("roku_end_pixel", roku)
        self.assertNotIn("m.trackWidth / m.windowSeconds", roku)
        self.assertIn("program.program_description", roku)
        self.assertIn("guide_start_minute", webos)
        self.assertIn("windowMinutes", webos)
        self.assertIn("item.program_description", webos)
        self.assertIn('"timeline"', tv_api)

    def test_roku_guide_uses_integer_geometry_marquees_and_aspect_safe_art(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        scene = open("clients/roku/components/MainScene.xml", encoding="utf-8").read()
        self.assertIn("roku_start_pixel", roku)
        self.assertIn("m.windowOffsetPixels", roku)
        self.assertNotIn("goto nextProgram", roku)
        self.assertIn('CreateObject("roSGNode", "ScrollingLabel")', roku)
        self.assertIn('scrollSpeed = 46', roku)
        self.assertIn('loadDisplayMode="scaleToZoom"', scene)
        self.assertGreaterEqual(scene.count("<ScrollingLabel"), 3)
        self.assertNotIn('.text = "Ready"', roku)

    def test_live_news_embed_cannot_capture_controls_or_pause(self):
        player = open(
            "fs42/fs42_server/static/live_news_player.html", encoding="utf-8"
        ).read()
        self.assertIn("pointer-events:none", player)
        self.assertIn("controls:0", player)
        self.assertIn("disablekb:1", player)
        self.assertIn("YT.PlayerState.PAUSED", player)
        self.assertNotIn("player.pauseVideo()", player)

    def test_scheduler_exposes_confirmed_history_repairs(self):
        scheduler = open(
            "fs42/fs42_server/static/scheduler.html", encoding="utf-8"
        ).read()
        self.assertIn("Edit history", scheduler)
        self.assertIn("Set next episode", scheduler)
        self.assertIn("Remove future reservations", scheduler)
        self.assertIn("Remove history and start over", scheduler)
        self.assertIn("confirm(confirmation)", scheduler)

    def test_outer_pages_bust_stale_guide_frame_cache(self):
        for path in (
            "fs42/fs42_server/static/index.html",
            "fs42/fs42_server/static/watch.html",
            "fs42/fs42_server/static/guide.html",
        ):
            page = open(path, encoding="utf-8").read()
            self.assertIn("guide_frame.html?", page)
            self.assertIn("v=myhometv-20260801-channel-nav-1", page)

    def test_live_news_cannot_trap_pc_navigation(self):
        watch_html = open(
            "fs42/fs42_server/static/watch.html", encoding="utf-8"
        ).read()
        watch_css = open(
            "fs42/fs42_server/static/watch.css", encoding="utf-8"
        ).read()
        watch_js = open(
            "fs42/fs42_server/static/watch.js", encoding="utf-8"
        ).read()
        self.assertIn('id="live-news"', watch_html)
        self.assertIn('tabindex="-1"', watch_html)
        self.assertIn("#live-news { z-index: 0", watch_css)
        self.assertIn("pointer-events: none", watch_css)
        self.assertIn('event.key === "Escape"', watch_js)
        self.assertIn("setGuideVisible(guide.hidden)", watch_js)

    def test_tv_clients_are_remote_only_and_back_always_restores_guide(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        webos = open("clients/webos/app.js", encoding="utf-8").read()
        self.assertIn('if key = "back"', roku)
        self.assertIn("stopPlayback()", roku)
        self.assertIn('state = "finished"', roku)
        self.assertIn('client: "roku"', roku)
        self.assertIn("key === 461", webos)
        self.assertIn('client:"webos"', webos)
        self.assertIn("changeChannel", webos)
        self.assertIn('addEventListener("ended"', webos)

    def test_roku_guide_has_one_focus_authority_and_clears_tune_status(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        scene = open("clients/roku/components/MainScene.xml", encoding="utf-8").read()
        self.assertNotIn('ObserveField("itemFocused"', roku)
        self.assertNotIn("<LabelList", scene)
        self.assertIn("refreshGuideSelection()", roku)
        self.assertIn('/prewarm"', roku)
        self.assertIn("cancelPendingTune()", roku)
        self.assertIn("event.GetRoSGNode() <> m.playTask", roku)
        self.assertGreaterEqual(
            roku.count('FindNode("status").text = ""'), 3
        )

    def test_roku_guide_overlays_video_and_hud_auto_hides_in_blue(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        scene = open("clients/roku/components/MainScene.xml", encoding="utf-8").read()
        self.assertLess(scene.index('id="videoA"'), scene.index('id="guideLayer"'))
        self.assertIn('color="#07111DEB"', scene)
        self.assertIn('color="#07111DE8"', scene)
        self.assertIn("m.hudTimer.duration = 6", roku)
        self.assertIn("showGuideOverlay()", roku)
        self.assertIn("closeGuideOverlay()", roku)
        self.assertIn('key = "fastforward"', roku)
        self.assertIn('key = "rewind"', roku)
        self.assertIn('key = "channelup"', roku)
        self.assertIn('key = "channeldown"', roku)
        self.assertNotIn("m.video.SetFocus(true)", roku)
        self.assertIn("m.currentProgram = closestProgramIndex(m.currentChannel, m.nowPixel)", roku)
        self.assertIn('m.guideLayer.visible = true', roku)
        self.assertIn('m.guideLayer.visible = false', roku)

    def test_roku_channel_changes_are_double_buffered_and_guide_wraps(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        scene = open("clients/roku/components/MainScene.xml", encoding="utf-8").read()
        self.assertIn('id="videoA"', scene)
        self.assertIn('id="videoB"', scene)
        self.assertIn('if state = "playing"', roku)
        self.assertIn("finishBufferedTune(which, node)", roku)
        tune = roku[roku.index("sub tuneCurrentChannel()"):
                    roku.index("sub onPlaybackReady")]
        self.assertNotIn("stopPlayback()", tune)
        self.assertIn("if m.currentChannel < 0 then m.currentChannel = m.rows.Count() - 1", roku)
        self.assertIn("if m.currentChannel >= m.rows.Count() then m.currentChannel = 0", roku)

    def test_roku_tune_feedback_is_immediate_and_adjacent_channels_stay_warm(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        scene = open("clients/roku/components/MainScene.xml", encoding="utf-8").read()
        self.assertIn('id="hudStatus"', scene)
        self.assertIn('id="hudArtwork"', scene)
        tune = roku[roku.index("sub tuneCurrentChannel()"):
                    roku.index("sub onPlaybackReady")]
        self.assertLess(tune.index("showTuningHud()"), tune.index('m.playTask.control = "run"'))
        self.assertIn('FindNode("hudStatus").text = "TUNING"', roku)
        self.assertIn("prewarmAdjacentChannels()", roku)
        self.assertIn("m.adjacentTimer.duration = 20", roku)
        self.assertIn("m.adjacentTimer.repeat = true", roku)

    def test_roku_channel_surfing_uses_server_neighbors_and_rejects_stale_tunes(self):
        roku = open("clients/roku/components/MainScene.brs", encoding="utf-8").read()
        self.assertIn("function playableNeighbor", roku)
        self.assertIn("row.next_tunable_index", roku)
        self.assertIn("row.previous_tunable_index", roku)
        self.assertIn("m.activeChannel", roku)
        self.assertIn("m.pendingChannel", roku)
        self.assertIn("event.GetRoSGNode() <> m.playTask", roku)
        self.assertIn('m.playTask.ObserveField("error", "onTuneError")', roku)
        self.assertIn("m.currentChannel = m.activeChannel", roku)
        self.assertIn("This channel is the myHomeTV program guide", roku)

    def test_web_and_roku_guides_share_one_complete_lineup(self):
        guide = open("fs42/fs42_server/static/guide.js", encoding="utf-8").read()
        self.assertIn("/api/tv/guide?hours=", guide)
        self.assertIn("station.programs || []", guide)
        self.assertNotIn("item._has_schedule || item.is_live_source", guide)
        self.assertIn("station.is_tunable === false", guide)

    def test_tv_client_packages_have_required_manifests(self):
        manifest = open("clients/roku/manifest", encoding="utf-8").read()
        appinfo = json.load(open("clients/webos/appinfo.json", encoding="utf-8"))
        self.assertIn("title=myHomeTV", manifest)
        self.assertEqual(appinfo["id"], "com.myhometv.client")
        self.assertTrue(__import__("pathlib").Path(
            "clients/releases/myhometv-roku.zip"
        ).is_file())
        self.assertTrue(__import__("pathlib").Path(
            "clients/releases/myhometv-webos.ipk"
        ).is_file())

    def test_schedule_names_are_query_parameters_not_path_segments(self):
        common = open(
            "fs42/fs42_server/static/common.js", encoding="utf-8"
        ).read()
        self.assertIn("network_name=${encodeURIComponent(networkId)}", common)
        self.assertNotIn("`schedules/${networkId}`", common)


if __name__ == "__main__":
    unittest.main()
