"""Offline tests for the prebuilt OSM cost grid."""
import unittest

from running.osm.campus import load_campus, polygon_centroid
from running.osm.grid import (
    CELL_BLOCKED,
    CELL_ROAD,
    CostGrid,
)


class CostGridTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.grid = CostGrid.load()
        cls.campus = load_campus()

    def test_dimensions_and_rle_expansion(self):
        self.assertGreater(self.grid.width, 100)
        self.assertGreater(self.grid.height, 100)
        self.assertEqual(len(self.grid.costs), self.grid.width*self.grid.height)
        self.assertEqual(self.grid.resolution_m, 2.0)

    def test_outside_boundary_is_blocked(self):
        self.assertEqual(self.grid.cost(0, 0), CELL_BLOCKED)
        self.assertFalse(self.grid.walkable(0, 0))
        self.assertFalse(self.grid.walkable(self.grid.width-1,
                                           self.grid.height-1))

    def test_building_interior_is_blocked(self):
        for building in self.campus.buildings[:5]:
            lat, lon = polygon_centroid(building.polygon)
            col, row = self.grid.latlon_to_cell(lat, lon)
            self.assertEqual(self.grid.cost(col, row), CELL_BLOCKED,
                             f"building {building.osm_id} not blocked")

    def test_roads_have_lowest_cost(self):
        road = next(r for r in self.campus.roads
                    if r.highway == "residential" and len(r.polyline) >= 2)
        lat, lon = road.polyline[1]
        col, row = self.grid.latlon_to_cell(lat, lon)
        self.assertLessEqual(self.grid.cost(col, row), CELL_ROAD)
        self.assertTrue(self.grid.walkable(col, row))

    def test_snap_to_road_point(self):
        road = next(r for r in self.campus.roads
                    if r.highway == "residential" and len(r.polyline) >= 2)
        lat, lon = road.polyline[1]
        snapped = self.grid.snap(lat, lon, radius_m=30)
        self.assertIsNotNone(snapped)
        col, row, distance = snapped
        self.assertLess(distance, 5.0)
        self.assertTrue(self.grid.walkable(col, row))

    def test_snap_fails_far_outside(self):
        self.assertIsNone(self.grid.snap(0.0, 0.0, radius_m=30))

    def test_cell_distance(self):
        self.assertAlmostEqual(self.grid.cell_distance_m((0, 0), (1, 0)), 2.0)
        self.assertAlmostEqual(self.grid.cell_distance_m((0, 0), (1, 1)),
                               2.0*2**0.5)

    def test_cost_distribution_has_all_walkable_kinds(self):
        stats = self.grid.stats()
        self.assertGreater(stats.get("road", 0), 0)
        self.assertGreater(stats.get("open", 0), 0)
        self.assertGreater(stats.get("blocked", 0), 0)


if __name__ == "__main__":
    unittest.main()
