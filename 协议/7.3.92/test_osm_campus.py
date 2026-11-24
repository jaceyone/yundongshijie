"""Offline tests for OSM campus data loading and geometry helpers."""
import unittest

from running.osm.campus import (
    CampusData,
    OsmRouteError,
    load_campus,
    out_of_china,
    point_in_polygon,
    point_polygon_distance,
    polygon_centroid,
    wgs84_to_gcj02,
)


class Wgs84ToGcjTests(unittest.TestCase):
    def test_offset_is_plausible_for_xuchang(self):
        # 许昌学院 OSM 中心（WGS84）；GCJ-02 偏移应为数百米量级。
        lat, lon = wgs84_to_gcj02(34.0447395, 113.8630331)
        self.assertAlmostEqual(lat, 34.0447395, delta=0.01)
        self.assertAlmostEqual(lon, 113.8630331, delta=0.01)
        self.assertNotEqual((lat, lon), (34.0447395, 113.8630331))

    def test_known_beijing_offset(self):
        # 39.90469, 116.40717 是常见的 WGS84→GCJ-02 测试向量。
        lat, lon = wgs84_to_gcj02(39.90469, 116.40717)
        self.assertAlmostEqual(lat, 39.90605, delta=0.0002)
        self.assertAlmostEqual(lon, 116.41333, delta=0.0002)

    def test_out_of_china_is_unconverted(self):
        self.assertTrue(out_of_china(51.5, -0.12))
        self.assertEqual(wgs84_to_gcj02(51.5, -0.12), (51.5, -0.12))


class GeometryTests(unittest.TestCase):
    square = [(0.0, 0.0), (0.0, 1.0), (1.0, 1.0), (1.0, 0.0)]

    def test_point_in_polygon(self):
        self.assertTrue(point_in_polygon(0.5, 0.5, self.square))
        self.assertFalse(point_in_polygon(1.5, 0.5, self.square))
        self.assertFalse(point_in_polygon(-0.1, 0.5, self.square))

    def test_polygon_distance(self):
        self.assertEqual(point_polygon_distance(0.5, 0.5, self.square), 0.0)
        self.assertAlmostEqual(
            point_polygon_distance(1.5, 0.5, self.square), 0.5)


class CampusDataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.campus = load_campus()

    def test_identity_and_counts(self):
        self.assertEqual(self.campus.campus_id, "xuchang_280173698")
        self.assertEqual(len(self.campus.buildings), 36)
        self.assertGreaterEqual(len(self.campus.roads), 11)
        self.assertGreaterEqual(len(self.campus.boundary), 3)

    def test_boundary_is_closed_and_bbox_matches(self):
        boundary = self.campus.boundary
        self.assertEqual(boundary[0], boundary[-1])
        lats = [p[0] for p in boundary]
        lons = [p[1] for p in boundary]
        self.assertAlmostEqual(min(lats), self.campus.bbox["minlat"], places=6)
        self.assertAlmostEqual(max(lons), self.campus.bbox["maxlon"], places=6)

    def test_buildings_have_closed_polygons_inside_campus(self):
        for building in self.campus.buildings:
            self.assertGreaterEqual(len(building.polygon), 3)
            self.assertEqual(building.polygon[0], building.polygon[-1])
            self.assertTrue(self.campus.contains(*polygon_centroid(building.polygon)),
                            f"building {building.osm_id} centroid outside campus")

    def test_building_lookup(self):
        building = self.campus.buildings[0]
        lat, lon = polygon_centroid(building.polygon)
        self.assertIsNotNone(self.campus.building_at(lat, lon))
        self.assertIsNone(self.campus.building_at(self.campus.center[0],
                                                  self.campus.center[1]))

    def test_missing_campus_raises_typed_error(self):
        with self.assertRaises(OsmRouteError) as context:
            load_campus("no_such_campus")
        self.assertEqual(context.exception.code, "osm_campus_not_found")

    def test_from_dict_rejects_bad_bbox(self):
        with self.assertRaises(ValueError):
            CampusData.from_dict({"boundary": [[0, 0], [0, 1], [1, 1]]})


if __name__ == "__main__":
    unittest.main()
