from pathlib import Path
import unittest


STATIC_ROOT = Path("fs42/fs42_server/static")


class StationManagerUITests(unittest.TestCase):
    def test_station_manager_exposes_confirmed_delete_action(self):
        page = (STATIC_ROOT / "stations.html").read_text(encoding="utf-8")

        self.assertIn('onclick="deleteStation(this.dataset.name, this)"', page)
        self.assertIn("async function deleteStation(networkName, button)", page)
        self.assertIn(
            "window.fs42Api.delete(`stations/${encodeURIComponent(networkName)}`)",
            page,
        )
        self.assertIn("Source media will not be deleted", page)
        self.assertIn("await loadStations()", page)

    def test_station_delete_button_has_destructive_styling(self):
        styles = (STATIC_ROOT / "station_ui.css").read_text(encoding="utf-8")

        self.assertIn(".station-card-actions .button-delete", styles)
        self.assertIn(".station-card-actions .button-delete:disabled", styles)


if __name__ == "__main__":
    unittest.main()
