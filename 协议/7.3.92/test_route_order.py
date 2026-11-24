"""Offline tests for the official five-point ordering port."""
import random
import unittest

from running.route_order import (
    _coord,
    _path_distance,
    _window,
    select_official_order,
)

# Real Xuchang account checkpoints: lat/lon are BD-09, glat/glon are GCJ-02.
REAL_POINTS = [
    {"lat": 34.049191, "lng": 113.872027,
     "glat": 34.042947, "glon": 113.865619, "pointName": "开拓路1"},
    {"lat": 34.047612, "lng": 113.871910,
     "glat": 34.041369, "glon": 113.865501, "pointName": "学生公寓2"},
    {"lat": 34.046778, "lng": 113.872055,
     "glat": 34.040533, "glon": 113.865646, "pointName": "学生公寓3"},
    {"lat": 34.047946, "lng": 113.871845,
     "glat": 34.041704, "glon": 113.865436, "pointName": "学生公寓1"},
    {"lat": 34.048968, "lng": 113.872952,
     "glat": 34.042713, "glon": 113.866548, "pointName": "路口2"},
]


class RouteOrderTests(unittest.TestCase):
    def test_coord_prefers_gcj_pair(self):
        self.assertEqual(_coord({"lat": 0, "lng": 0,
                                 "glat": 34.04, "glon": 113.86}),
                         (34.04, 113.86))

    def test_window_strength_and_normal(self):
        self.assertEqual(_window(3000, 1000, 1), (1800.0, 3000.0))
        self.assertEqual(_window(3000, 1000, 0), (800.0, 1200.0))
        # minDistance <= 0 falls back to 1.2 * minDistance.
        self.assertEqual(_window(3000, 0, 0), (0.0, 0.0))

    def test_positions_are_zero_based_and_original_coords_preserved(self):
        result = select_official_order(REAL_POINTS, 3000, 1000, 0,
                                       rng=random.Random(7))
        self.assertEqual([p["position"] for p in result.points], list(range(5)))
        self.assertEqual(sorted(p["pointName"] for p in result.points),
                         sorted(p["pointName"] for p in REAL_POINTS))
        for point in result.points:
            source = next(p for p in REAL_POINTS
                          if p["pointName"] == point["pointName"])
            self.assertEqual(point["lat"], source["lat"])
            self.assertEqual(point["lng"], source["lng"])
            self.assertEqual(point["glat"], source["glat"])
            self.assertEqual(point["glon"], source["glon"])

    def test_normal_window_uses_glat_glon_distance(self):
        result = select_official_order(REAL_POINTS, 3000, 1000, 0,
                                       rng=random.Random(7))
        self.assertEqual(result.mode, "within")
        self.assertGreaterEqual(result.distance_m, result.low_m)
        self.assertLessEqual(result.distance_m, result.high_m)
        # The selected order's distance is the open path over glat/glon.
        self.assertAlmostEqual(result.distance_m,
                               _path_distance(result.points), places=6)

    def test_below_window_falls_back_to_longest(self):
        result = select_official_order(REAL_POINTS, 30000, 10000, 0,
                                       rng=random.Random(1))
        self.assertEqual(result.mode, "below")
        self.assertAlmostEqual(result.distance_m, 852.4342413867918, places=3)

    def test_above_window_falls_back_to_shortest(self):
        result = select_official_order(REAL_POINTS, 1000, 100, 0,
                                       rng=random.Random(1))
        self.assertEqual(result.mode, "above")
        self.assertAlmostEqual(result.distance_m, 360.34662729660647, places=3)

    def test_strength_window_falls_back_below(self):
        result = select_official_order(REAL_POINTS, 3000, 1000, 1,
                                       rng=random.Random(1))
        self.assertEqual(result.mode, "below")

    def test_missing_coordinates_rejected(self):
        for point in ({"lat": 34.0}, {"glon": 113.8}, {}):
            with self.assertRaises(ValueError):
                _coord(point)


if __name__ == "__main__":
    unittest.main()
