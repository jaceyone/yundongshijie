"""Offline tests for the official LocJsonEntity / MyLocation schema."""
import json
import time
import unittest

from running.models import GpsPoint
from running.schema import (
    MY_LOCATION_FIELDS,
    build_loc_json,
    decode_run_data,
    encode_run_data,
    parse_loc_json,
    validate_loc_json,
)


def points():
    start = 1700000000000
    return [
        GpsPoint(30.0, 120.0, start, 2.5, 3.0),
        GpsPoint(30.0002, 120.0003, start + 3000, 2.5, 3.0),
        GpsPoint(30.0004, 120.0001, start + 6000, 2.5, 3.0),
    ]


class OfficialSchemaTests(unittest.TestCase):
    def test_loc_json_is_official_envelope_with_string_payload(self):
        loc_json = build_loc_json(points(), 1700000000000)
        outer = json.loads(loc_json)
        self.assertEqual(set(outer), {"useZip", "allLocJson"})
        self.assertIs(outer["useZip"], False)
        self.assertIsInstance(outer["allLocJson"], str)

        inner = json.loads(outer["allLocJson"])
        self.assertEqual(len(inner), 3)
        self.assertEqual(list(inner[0]), list(MY_LOCATION_FIELDS))
        # Official records keep lat/lng at -1 and put GCJ-02 in gLat/gLng.
        self.assertEqual(inner[0]["lat"], -1.0)
        self.assertEqual(inner[0]["lng"], -1.0)
        self.assertEqual(inner[0]["gLat"], 30.0)
        self.assertEqual(inner[0]["gLng"], 120.0)
        self.assertEqual(
            inner[0]["gainTime"],
            time.strftime("%Y-%m-%d %H:%M:%S",
                          time.localtime(1700000000000 / 1000.0)))
        self.assertEqual(inner[0]["totalTime"], 0)
        self.assertEqual(inner[0]["totalDis"], 0.0)
        self.assertEqual(inner[0]["flag"], 1700000000000)
        self.assertEqual(inner[0]["count"], 1)
        self.assertEqual(inner[0]["id"], 1)
        self.assertEqual(inner[0]["coorType"], "gcj02")
        self.assertEqual(inner[0]["state"], 1)
        self.assertEqual(inner[0]["locType"], 1)
        self.assertEqual(inner[0]["bdG"], 1)
        self.assertEqual(inner[0]["type"], 5)
        self.assertEqual(inner[2]["type"], 6)
        self.assertEqual(inner[1]["type"], 0)
        self.assertEqual(inner[0]["avgSpeed"], 2.5)
        self.assertEqual(inner[0]["speed"], 6.67)
        self.assertGreater(inner[1]["totalDis"], 0.0)
        self.assertEqual(inner[1]["totalTime"], 3)
        self.assertEqual(inner[2]["totalTime"], 6)

    def test_run_data_round_trip(self):
        loc_json = build_loc_json(points(), 1700000000000)
        encoded = encode_run_data(loc_json)
        self.assertNotIn("\n", encoded)
        self.assertEqual(decode_run_data(encoded), loc_json)

    def test_parse_legacy_array_and_official_envelope(self):
        legacy = json.dumps([{"lat": 30.0, "lon": 120.0},
                             {"lat": 30.1, "lon": 120.1}])
        parsed = parse_loc_json(legacy)
        self.assertEqual(parsed["kind"], "legacy")
        self.assertEqual(len(parsed["points"]), 2)

        official = build_loc_json(points(), 1700000000000)
        parsed = parse_loc_json(official)
        self.assertEqual(parsed["kind"], "locjsonentity")
        self.assertEqual(len(parsed["points"]), 3)

    def test_validate_requires_official_and_monotonic_totals(self):
        legacy = [{"lat": 30.0, "lng": 120.0, "gLat": 30.0, "gLng": 120.0,
                   "gainTime": "1", "totalTime": 0, "totalDis": 0.0},
                  {"lat": 30.1, "lng": 120.1, "gLat": 30.1, "gLng": 120.1,
                   "gainTime": "2", "totalTime": 1000, "totalDis": 10.0}]
        with self.assertRaises(ValueError):
            validate_loc_json(legacy)
        validate_loc_json(legacy, require_official=False)

        bad = [dict(legacy[0]), dict(legacy[1])]
        bad[1]["totalTime"] = -1
        validate_loc_json(bad, require_official=False)
        with self.assertRaises(ValueError):
            validate_loc_json(bad, require_official=False, strict_order=True)

    def test_validate_allows_official_out_of_order_points_unless_strict(self):
        # Real fixture rrid 1333592274 has adjacent totalDis 12.1 then 0.0.
        points = [
            {"lat": -1.0, "lng": -1.0, "gLat": 34.04, "gLng": 113.86,
             "gainTime": "2026-10-13 15:59:00", "totalTime": 0, "totalDis": 12.1},
            {"lat": -1.0, "lng": -1.0, "gLat": 34.05, "gLng": 113.87,
             "gainTime": "2026-10-13 15:58:56", "totalTime": 0, "totalDis": 0.0},
        ]
        loc = {"useZip": False, "allLocJson": json.dumps(points)}
        self.assertEqual(len(validate_loc_json(loc)), 2)
        with self.assertRaises(ValueError):
            validate_loc_json(loc, strict_order=True)

    def test_build_rejects_unsorted_or_short_tracks(self):
        pts = points()
        with self.assertRaises(ValueError):
            build_loc_json(pts[:1], 1700000000000)
        bad = [pts[0], GpsPoint(30.0, 120.0, 1700000000000, 2.5, 3.0)]
        with self.assertRaises(ValueError):
            build_loc_json(bad, 1700000000000)


if __name__ == "__main__":
    unittest.main()
