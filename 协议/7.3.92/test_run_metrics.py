"""Offline tests for official detail metrics: bdA, totalAscent, laps, stepDistance."""
import base64
import gzip
import json
import math
import socket
import unittest
from unittest.mock import patch

from running.builder import RunningTrackBuilder
from running.metrics import (
    LAP_FIELDS,
    build_laps,
    elevation_profile,
    total_ascent,
)
from running.models import GpsPoint
from running.schema import build_loc_json

START = 1700000000000
UUID = "metrics-test"
DISTANCE = 2500
SECONDS = 1100
STEPS = 3500


def decode_obs(value):
    return gzip.decompress(base64.b64decode(value)).decode("utf-8")


def inner_points(body):
    return json.loads(json.loads(body["allLocJson"])["allLocJson"])


def build_payload(uuid=UUID, altitude_seed=None):
    builder = RunningTrackBuilder(1, 2, 1, uuid_str=uuid)
    points, fixed = builder.generate_track_around_points(
        30, 120, None, DISTANCE, SECONDS, START, altitude_seed=altitude_seed)
    body, obs, key = builder.build_submission_payload(
        DISTANCE, SECONDS, STEPS, START, START + SECONDS * 1000, points, fixed)
    return builder, points, fixed, body, obs


def median3(first, middle, last):
    return sorted((first, middle, last))[1]


class RunMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (cls.builder, cls.points, cls.fixed,
         cls.body, cls.obs) = build_payload()
        cls.inner = inner_points(cls.body)
        cls.laps = json.loads(decode_obs(cls.obs["laps_json"]))

    def setUp(self):
        guard = patch.object(socket.socket, "connect",
                             side_effect=AssertionError("network forbidden"))
        guard.start()
        self.addCleanup(guard.stop)

    # ---------- bdA ----------

    def test_bda_present_and_plausible(self):
        altitudes = [point["bdA"] for point in self.inner]
        self.assertTrue(all(35.0 <= value <= 80.0 for value in altitudes))
        self.assertGreater(max(altitudes) - min(altitudes), 2.0)
        self.assertGreater(len({round(value, 2) for value in altitudes}), 10)
        self.assertTrue(all(35.0 <= point.altitude <= 80.0
                            for point in self.points))

    def test_altitude_seed_is_reproducible_and_injectable(self):
        self.assertEqual(elevation_profile(40, 12345),
                         elevation_profile(40, 12345))
        self.assertNotEqual(elevation_profile(40, 12345),
                            elevation_profile(40, 54321))

        first = build_payload(uuid="repro-seed", altitude_seed=777)[1]
        second = build_payload(uuid="repro-seed", altitude_seed=777)[1]
        other = build_payload(uuid="repro-seed", altitude_seed=778)[1]
        self.assertEqual([point.altitude for point in first],
                         [point.altitude for point in second])
        self.assertNotEqual([point.altitude for point in first],
                            [point.altitude for point in other])

    # ---------- totalAscent ----------

    def test_total_ascent_matches_official_median_smoothing(self):
        altitudes = [point["bdA"] for point in self.inner]
        smoothed = []
        for index, value in enumerate(altitudes):
            first = altitudes[index - 1] if index > 0 else value
            last = altitudes[index + 1] if index + 1 < len(altitudes) else value
            smoothed.append(median3(first, value, last))
        expected = int(max(0.0, max(smoothed) - min(smoothed)))

        self.assertEqual(self.body["totalAscent"], expected)
        self.assertGreaterEqual(expected, 0)
        self.assertEqual(total_ascent(altitudes), expected)
        # 签名参数与 save_record 必须同源，否则服务端签名不一致。
        self.assertIn(f"totalAscent={expected}", self.body["originalSign"])

    # ---------- laps ----------

    def test_laps_cover_every_kilometer_and_partial_tail(self):
        self.assertEqual(len(self.laps), math.ceil(DISTANCE / 1000.0))
        self.assertEqual([lap["lapIndex"] for lap in self.laps], [1, 2, 3])
        self.assertEqual([lap["distance"] for lap in self.laps],
                         [1000.0, 1000.0, 500.0])
        self.assertTrue(self.laps[0]["isFullLap"])
        self.assertTrue(self.laps[1]["isFullLap"])
        self.assertFalse(self.laps[-1]["isFullLap"])
        self.assertAlmostEqual(sum(lap["distance"] for lap in self.laps),
                               DISTANCE, places=6)
        self.assertEqual(sum(lap["step"] for lap in self.laps), STEPS)
        self.assertEqual(sum(lap["duration"] for lap in self.laps), SECONDS)
        self.assertEqual(self.laps[-1]["cumulativeDuration"], SECONDS)
        self.assertEqual([lap["cumulativeDuration"] for lap in self.laps],
                         sorted(lap["cumulativeDuration"] for lap in self.laps))

    def test_lap_formulas_use_official_units(self):
        for lap in self.laps:
            self.assertGreater(lap["duration"], 0)
            self.assertGreater(lap["step"], 0)
            self.assertAlmostEqual(
                lap["avgCadence"], lap["step"] * 60.0 / lap["duration"],
                places=3)
            if lap["isFullLap"]:
                self.assertEqual(lap["distance"], 1000.0)
                self.assertAlmostEqual(lap["avgPace"],
                                       lap["duration"] / 60.0, places=3)
                self.assertAlmostEqual(lap["avgStride"],
                                       100000.0 / lap["step"], places=3)
            else:
                self.assertAlmostEqual(
                    lap["avgPace"],
                    lap["duration"] * 1000.0 / (lap["distance"] * 60.0),
                    places=3)
                self.assertAlmostEqual(
                    lap["avgStride"],
                    lap["distance"] * 100.0 / lap["step"], places=3)

    def test_lap_elevation_fields_are_consistent(self):
        base_altitude = self.inner[0]["bdA"]
        raw_gain = sum(max(0.0, self.inner[index + 1]["bdA"]
                           - self.inner[index]["bdA"])
                       for index in range(len(self.inner) - 1))
        track_min = min(point["bdA"] for point in self.inner)
        track_max = max(point["bdA"] for point in self.inner)
        for lap in self.laps:
            self.assertGreaterEqual(lap["elevationGain"], 0.0)
            self.assertGreaterEqual(lap["endAltAbs"], track_min)
            self.assertLessEqual(lap["endAltAbs"], track_max)
            self.assertAlmostEqual(lap["endAltRel"],
                                   lap["endAltAbs"] - base_altitude, delta=0.0011)
        # 各段正爬升之和应等于整条轨迹的正增量累加。
        self.assertAlmostEqual(
            sum(lap["elevationGain"] for lap in self.laps),
            raw_gain, delta=0.002 * len(self.laps))

    def test_laps_json_is_gzip_base64_and_has_all_fields(self):
        raw = decode_obs(self.obs["laps_json"])
        self.assertTrue(raw)
        laps = json.loads(raw)
        self.assertEqual(len(laps), len(self.laps))
        self.assertEqual(list(laps[0]), list(LAP_FIELDS))
        for lap in laps:
            for field in ("lapIndex", "duration", "cumulativeDuration",
                          "step", "flag", "id"):
                self.assertIsInstance(lap[field], int)
            for field in ("distance", "avgPace", "avgCadence", "avgStride",
                          "endAltAbs", "endAltRel", "elevationGain"):
                self.assertIsInstance(lap[field], float)
            self.assertIsInstance(lap["isFullLap"], bool)

    def test_laps_can_be_built_directly_from_points_and_altitudes(self):
        points = [
            GpsPoint(30.0, 120.0, START, altitude=50.0),
            GpsPoint(30.0, 120.01, START + 600 * 1000, altitude=52.0),
            GpsPoint(30.0, 120.02, START + 1200 * 1000, altitude=48.0),
        ]
        laps = build_laps(points, 2000, 1200, 3000, START)
        self.assertEqual(len(laps), 2)
        self.assertTrue(all(lap["isFullLap"] for lap in laps))
        self.assertEqual(sum(lap["step"] for lap in laps), 3000)
        self.assertEqual(sum(lap["duration"] for lap in laps), 1200)
        self.assertEqual(laps[-1]["endAltAbs"], 48.0)

    # ---------- stepDistance ----------

    def test_step_distance_is_monotonic_and_tracks_bucket_distance(self):
        values = [point["stepDistance"] for point in self.inner]
        self.assertEqual(values[0], 0.0)
        self.assertTrue(all(second > first
                            for first, second in zip(values, values[1:])))
        # stepDistance 现在与 speedPerTenSec 同源，末点等于总距离。
        self.assertAlmostEqual(values[-1], DISTANCE, places=3)
        self.assertEqual([point.step_distance for point in self.points],
                         [round(value, 3) for value in values])
        speeds = self.body["speedPerTenSec"]
        steps = self.body["stepsPerTenSec"]
        strides = [row["distance"] / step["stepsNum"] * 100.0
                   for row, step in zip(speeds, steps) if step["stepsNum"]]
        # 详情页步幅图的上下浮动必须超过 35 cm。
        self.assertGreater(max(strides) - min(strides), 35.0)

    def test_visible_fluctuations_meet_targets(self):
        """3000 m / 1060 s 场景：步频 >60、步幅 >35 cm、每公里用时 >20 s。"""
        builder = RunningTrackBuilder(1, 2, 1, uuid_str="fluctuation-target")
        points, fixed = builder.generate_track_around_points(
            30, 120, None, 3000, 1060, START)
        body, obs, _ = builder.build_submission_payload(
            3000, 1060, 4121, START, START + 1060 * 1000, points, fixed)

        speeds = body["speedPerTenSec"]
        steps = body["stepsPerTenSec"]
        cadence = [row["stepsNum"] * 6 for row in steps]
        strides = [row["distance"] / step["stepsNum"] * 100.0
                   for row, step in zip(speeds, steps) if step["stepsNum"]]
        self.assertGreater(max(cadence) - min(cadence), 60)
        self.assertGreater(max(strides) - min(strides), 35.0)
        self.assertEqual(sum(row["stepsNum"] for row in steps), 4121)
        self.assertAlmostEqual(sum(row["distance"] for row in speeds), 3000, places=6)

        laps = json.loads(decode_obs(obs["laps_json"]))
        durations = [lap["duration"] for lap in laps]
        self.assertGreater(max(durations) - min(durations), 20)
        self.assertEqual(sum(durations), 1060)
        for lap in laps:
            self.assertGreaterEqual(lap["avgPace"], 4.0)
            self.assertLessEqual(lap["avgPace"], 12.0)

    def test_default_gps_point_keeps_zero_metrics(self):
        point = GpsPoint(30.0, 120.0, START)
        self.assertEqual(point.altitude, 0.0)
        self.assertEqual(point.step_distance, 0.0)
        loc_json = build_loc_json(
            [point, GpsPoint(30.0, 120.001, START + 3000)], START)
        inner = json.loads(json.loads(loc_json)["allLocJson"])
        self.assertEqual(inner[0]["bdA"], 0.0)
        self.assertEqual(inner[0]["stepDistance"], 0.0)

    # ---------- 10 秒波动 ----------

    def test_speed_and_step_buckets_are_lossless_and_fluctuating(self):
        speed_rows = self.body["speedPerTenSec"]
        step_rows = self.body["stepsPerTenSec"]
        self.assertAlmostEqual(
            sum(row["distance"] for row in speed_rows), DISTANCE, places=6)
        self.assertEqual(sum(row["stepsNum"] for row in step_rows), STEPS)
        # OBS 与 save_record 同源。
        self.assertEqual(speed_rows, json.loads(decode_obs(self.obs["speed_json"])))
        self.assertEqual(step_rows,
                         json.loads(decode_obs(self.obs["step_freq_json"])))

        distances = [row["distance"] for row in speed_rows]
        middle = sum(distances[30:36])
        self.assertGreater(max(distances) - min(distances), 0.5)
        self.assertLess(sum(distances[:6]), middle)
        self.assertLess(sum(distances[-6:]), middle)
        self.assertLess(sum(row["stepsNum"] for row in step_rows[:6]),
                        sum(row["stepsNum"] for row in step_rows[30:36]))

        pace = (SECONDS / 60.0) / (DISTANCE / 1000.0)
        self.assertGreaterEqual(pace, 4.0)
        self.assertLessEqual(pace, 12.0)

    def test_replay_from_existing_loc_json_keeps_metrics(self):
        replay = RunningTrackBuilder(1, 2, 1, uuid_str=UUID)
        body, obs, _ = replay.build_submission_payload(
            DISTANCE, SECONDS, STEPS, START, START + SECONDS * 1000,
            self.points, self.fixed, loc_json=self.body["allLocJson"])
        self.assertEqual(body["allLocJson"], self.body["allLocJson"])
        self.assertEqual(body["totalAscent"], self.body["totalAscent"])
        self.assertEqual(decode_obs(obs["laps_json"]),
                         decode_obs(self.obs["laps_json"]))


if __name__ == "__main__":
    unittest.main()
