"""Offline tests for the building-avoiding A* route planner."""
import json
import math
import os
import tempfile
import unittest

from running.osm import OsmRouteError, TrackPlanner, export_route_geojson
from running.osm.campus import load_campus, polyline_length, polygon_centroid

CHECKPOINTS = [
    (34.041338, 113.868042),
    (34.041275, 113.866634),
    (34.043554, 113.866505),
    (34.045138, 113.869376),
    (34.044587, 113.870827),
]


def _sample_segments(polyline, step_m=1.0):
    for a, b in zip(polyline, polyline[1:]):
        length = polyline_length([a, b])
        steps = max(1, int(math.ceil(length/step_m)))
        for i in range(steps+1):
            fraction = i/steps
            yield (a[0] + (b[0]-a[0])*fraction,
                   a[1] + (b[1]-a[1])*fraction)


class PlannerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.planner = TrackPlanner.from_campus()
        cls.campus = load_campus()

    def test_plan_returns_closed_loop_through_all_checkpoints(self):
        result = self.planner.plan(CHECKPOINTS, 3000)
        self.assertGreater(len(result.polyline), 10)
        self.assertLessEqual(result.loop_length_m, 3000.5)
        self.assertLess(polyline_length([result.polyline[0],
                                         result.polyline[-1]]), 0.05)
        self.assertEqual(result.laps,
                         math.ceil(3000/result.loop_length_m))
        for checkpoint in CHECKPOINTS:
            nearest = min(polyline_length([checkpoint, point])
                          for point in result.polyline)
            self.assertLessEqual(nearest, 0.1,
                                 f"checkpoint {checkpoint} missing from route")

    def test_plan_avoids_buildings_and_stays_inside_fence(self):
        result = self.planner.plan(CHECKPOINTS, 3000)
        grid = self.planner.grid
        for lat, lon in _sample_segments(result.polyline):
            col, row = grid.latlon_to_cell(lat, lon)
            self.assertTrue(grid.walkable(col, row),
                            f"route leaves walkable area at {lat:.6f},{lon:.6f}")
            self.assertIsNone(self.campus.building_at(lat, lon),
                              f"route crosses building at {lat:.6f},{lon:.6f}")

    def test_checkpoints_are_preserved_exactly(self):
        result = self.planner.plan(CHECKPOINTS, 3000)
        for checkpoint in CHECKPOINTS:
            self.assertIn(checkpoint, result.polyline)

    def test_snap_diagnostics(self):
        result = self.planner.plan(CHECKPOINTS, 3000)
        self.assertEqual(len(result.diagnostics["snap_distance_m"]), 5)
        self.assertLessEqual(max(result.diagnostics["snap_distance_m"]), 30.0)
        self.assertEqual(result.diagnostics["orders_tried"], 1)

    def test_policy_permutations_are_supported(self):
        result = self.planner.plan(CHECKPOINTS, 3000, policy=2)
        self.assertEqual(result.diagnostics["orders_tried"], 120)
        self.assertGreaterEqual(result.diagnostics["orders_valid"], 1)
        self.assertLessEqual(result.loop_length_m, 3000.5)

    def test_checkpoint_inside_building_gets_access_corridor(self):
        building = self.campus.buildings[0]
        lat, lon = polygon_centroid(building.polygon)
        result = self.planner.plan([CHECKPOINTS[0], (lat, lon)], 3000)
        corridors = result.diagnostics["access_corridors"]
        self.assertEqual(len(corridors), 1)
        self.assertEqual(corridors[0]["building_id"], building.osm_id)
        nearest = min(polyline_length([(lat, lon), point])
                      for point in result.polyline)
        self.assertLessEqual(nearest, 0.1)
        # 只允许穿过打卡点所在的那一栋建筑；其它建筑仍不可走。
        for sample_lat, sample_lon in _sample_segments(result.polyline):
            hit = self.campus.building_at(sample_lat, sample_lon)
            if hit is not None:
                self.assertEqual(hit.osm_id, building.osm_id,
                                 f"route crosses unexpected building {hit.osm_id}")

    def test_target_distance_too_short_is_rejected(self):
        with self.assertRaises(OsmRouteError) as context:
            self.planner.plan(CHECKPOINTS, 100)
        self.assertEqual(context.exception.code,
                         "osm_target_distance_too_short")

    def test_checkpoint_outside_campus_is_rejected(self):
        with self.assertRaises(OsmRouteError) as context:
            self.planner.plan([CHECKPOINTS[0], (0.0, 0.0)], 3000)
        self.assertEqual(context.exception.code, "osm_no_free_cell")

    def test_short_loop_is_enlarged_instead_of_repeating_forever(self):
        # 两个很近的点闭环约 200 m；3000 m 目标需要补长到 ≤ 8 圈。
        result = self.planner.plan([
            (34.043554, 113.866505),
            (34.043733, 113.867545),
        ], 3000, max_laps=8)
        self.assertLessEqual(result.laps, 8)
        self.assertGreaterEqual(result.loop_length_m, 3000/8)
        for lat, lon in _sample_segments(result.polyline):
            col, row = self.planner.grid.latlon_to_cell(lat, lon)
            self.assertTrue(self.planner.grid.walkable(col, row))

    def test_geojson_export(self):
        result = self.planner.plan(CHECKPOINTS, 3000)
        with tempfile.TemporaryDirectory() as directory:
            path = export_route_geojson(result.polyline,
                                        os.path.join(directory, "track.geojson"),
                                        {"campus": "xuchang_280173698"})
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        self.assertEqual(data["type"], "FeatureCollection")
        coordinates = data["features"][0]["geometry"]["coordinates"]
        self.assertEqual(len(coordinates), len(result.polyline))
        self.assertEqual(data["features"][0]["properties"]["campus"],
                         "xuchang_280173698")


if __name__ == "__main__":
    unittest.main()
