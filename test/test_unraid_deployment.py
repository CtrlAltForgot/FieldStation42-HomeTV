import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class UnraidDeploymentTests(unittest.TestCase):
    def test_compose_is_isolated_and_headless(self):
        compose = (ROOT / "docker/docker-compose.unraid-staging.yml").read_text()
        self.assertIn('container_name: myhometv', compose)
        self.assertIn('image: myhometv:staging', compose)
        self.assertIn('"4243:4242"', compose)
        self.assertIn('"/mnt/user/Media:/media:ro"', compose)
        self.assertIn(
            '"/mnt/user/appdata/fieldstation42-hometv/runtime:/app/runtime"',
            compose,
        )
        self.assertIn("restart: unless-stopped", compose)
        self.assertIn("healthcheck:", compose)
        self.assertNotIn("network_mode: host", compose)
        self.assertNotIn("X11", compose)
        self.assertNotIn("PULSE", compose.upper())

    def test_rebrand_keeps_legacy_container_migration_safe(self):
        deploy = (ROOT / "deploy-unraid.sh").read_text()
        self.assertIn('readonly CONTAINER="myhometv"', deploy)
        self.assertIn('readonly LEGACY_CONTAINER="fieldstation42-hometv"', deploy)
        self.assertIn('docker rename "$LEGACY_CONTAINER" "$LEGACY_BACKUP"', deploy)
        self.assertIn('docker rm "$LEGACY_BACKUP"', deploy)

    def test_primary_web_surfaces_use_myhometv_brand(self):
        expected = {
            "fs42/fs42_server/static/index.html": "<title>myHomeTV</title>",
            "fs42/fs42_server/static/guide_frame.html": "myHomeTV Guide",
            "fs42/fs42_server/static/watch.html": "myHomeTV — Watch",
            "fs42/fs42_server/static/remote.html": "remote-brand\">myHomeTV",
            "fs42/fs42_server/static/common.js": ">myHomeTV</a>",
        }
        for relative, brand in expected.items():
            with self.subTest(relative=relative):
                self.assertIn(brand, (ROOT / relative).read_text())

    def test_migration_copies_only_toon_mix_json(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "existing/confs"
            staging = root / "staging"
            source.mkdir(parents=True)
            toon = json.loads(
                (ROOT / "confs/examples/traditional_network.json").read_text()
            )
            toon["station_conf"]["network_name"] = "Toon Mix"
            other = json.loads(json.dumps(toon))
            other["station_conf"]["network_name"] = "Other Channel"
            (source / "toon-mix.json").write_text(json.dumps(toon))
            (source / "other.json").write_text(json.dumps(other))
            (source / "active.db-wal").write_text("transient")

            env = dict(
                os.environ,
                FS42_STAGING_ROOT=str(staging),
                FS42_ALLOW_TEST_PATHS="1",
            )
            result = subprocess.run(
                [
                    "bash",
                    str(ROOT / "migrate-test-state.sh"),
                    "--source",
                    str(source),
                ],
                cwd=ROOT,
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((staging / "confs/toon-mix.json").is_file())
            self.assertFalse((staging / "confs/other.json").exists())
            self.assertFalse((staging / "confs/active.db-wal").exists())


if __name__ == "__main__":
    unittest.main()
