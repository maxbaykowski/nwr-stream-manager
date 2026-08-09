from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

from tools import update_same_data


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PATH = REPO_ROOT / "src" / "nwr-stream-manager"


def load_same_data_module():
    package = types.ModuleType("nwr_stream_manager")
    package.__path__ = [str(PACKAGE_PATH)]  # type: ignore[attr-defined]
    sys.modules.setdefault("nwr_stream_manager", package)
    spec = importlib.util.spec_from_file_location(
        "nwr_stream_manager.same_data",
        PACKAGE_PATH / "same_data.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["nwr_stream_manager.same_data"] = module
    spec.loader.exec_module(module)
    return module


class SameDataGeneratorTests(unittest.TestCase):
    def test_parse_event_codes(self) -> None:
        html = """
        <table id="eas_codes">
          <tr><th>Event</th><th>Code</th><th>Status</th></tr>
          <tr><td>Tornado Warning</td><td>TOR</td><td>Operational</td></tr>
          <tr><td>Old Event</td><td>OLD</td><td>Retired</td></tr>
        </table>
        """
        events = update_same_data.parse_event_codes(html, minimum_count=1)
        self.assertEqual(events["TOR"], {"code": "TOR", "name": "Tornado Warning"})
        self.assertNotIn("OLD", events)

    def test_county_same_code_normalization(self) -> None:
        locations = update_same_data.build_county_locations(
            [
                {
                    "STATE": "MI",
                    "COUNTYNAME": "Ottawa",
                    "FIPS": "26139",
                }
            ]
        )
        self.assertEqual(locations["026139"]["same"], "026139")
        self.assertEqual(locations["026139"]["fips"], "26139")
        self.assertEqual(locations["026139"]["state"], "MI")
        self.assertEqual(locations["026139"]["name"], "Ottawa")

    def test_county_duplicate_names_are_preserved_as_aliases(self) -> None:
        locations = update_same_data.build_county_locations(
            [
                {"STATE": "FL", "COUNTYNAME": "Upper Keys in Monroe", "FIPS": "12087"},
                {"STATE": "FL", "COUNTYNAME": "Lower Keys in Monroe", "FIPS": "12087"},
            ]
        )
        self.assertEqual(locations["012087"]["aliases"], ["Lower Keys in Monroe", "Upper Keys in Monroe"])

    def test_marine_zone_mapping(self) -> None:
        locations = update_same_data.build_marine_locations(
            "LM|92846|Holland to Grand Haven MI|42.915|-86.2624\n",
            "LM|92|LAKE MICHIGAN|Lake Michigan\n",
            [{"ID": "LMZ846", "NAME": "Holland to Grand Haven MI"}],
        )
        self.assertEqual(locations["092846"]["same"], "092846")
        self.assertEqual(locations["092846"]["ugc"], "LMZ846")
        self.assertEqual(locations["092846"]["name"], "Holland to Grand Haven MI")
        self.assertEqual(locations["092846"]["location_type"], "marine")

    def test_duplicate_location_codes_are_rejected_on_merge(self) -> None:
        with self.assertRaises(update_same_data.SameDataError):
            update_same_data.merge_locations(
                {"001001": {"same": "001001", "name": "A", "location_type": "county", "fips": "01001"}},
                {"001001": {"same": "001001", "name": "B", "location_type": "marine"}},
            )


class SameDataLookupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.same_data = load_same_data_module()

    def test_known_event_lookup(self) -> None:
        event = self.same_data.lookup_event("tor")
        self.assertTrue(event.known)
        self.assertEqual(event.code, "TOR")
        self.assertEqual(event.name, "Tornado Warning")

    def test_unknown_event_lookup(self) -> None:
        event = self.same_data.lookup_event("XYZ")
        self.assertFalse(event.known)
        self.assertEqual(event.display_name, "Unknown event (XYZ)")

    def test_known_location_lookup(self) -> None:
        location = self.same_data.lookup_location("026139")
        self.assertTrue(location.known)
        self.assertEqual(location.fips, "26139")
        self.assertEqual(location.state, "MI")
        self.assertEqual(location.name, "Ottawa")

    def test_known_marine_location_lookup(self) -> None:
        location = self.same_data.lookup_location("092846")
        self.assertTrue(location.known)
        self.assertEqual(location.location_type, "marine")
        self.assertEqual(location.ugc, "LMZ846")

    def test_unknown_location_lookup(self) -> None:
        location = self.same_data.lookup_location("099999")
        self.assertFalse(location.known)
        self.assertEqual(location.display_name, "Unknown location (099999)")


if __name__ == "__main__":
    unittest.main()
