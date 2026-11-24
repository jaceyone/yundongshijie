# -*- coding: utf-8 -*-
"""OSM 路线回归：用 2026-11-12 真实提交的 5 个官方点验证避建筑。

真实记录 rrid=1362756035 走的是 5 点直线合成路线，371 个点里有 82 个
落在建筑内、119 段穿建筑。这里把同一组 GCJ-02 点交给 OSM 规划器，
断言最终采样轨迹不再穿建筑，且 5 个点仍在轨迹上。
"""
import math
import unittest

from running.builder import RunningTrackBuilder
from running.client import resolve_path_source
from running.osm import OsmRouteError, TrackPlanner
from running.osm.campus import point_in_polygon

# 2026-11-12 真实记录中的官方 5 点（GCJ-02，position 0..4）。
REAL_CHECKPOINTS = [
    (34.041704, 113.865436),
    (34.042947, 113.865619),
    (34.040533, 113.865646),
    (34.042713, 113.866548),
    (34.041369, 113.865501),
]


def _dist_m(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371000 * math.asin(math.sqrt(h))


def _segments_cross(p, q, r, s):
    def orient(a, b, c):
        return (b[1] - a[1]) * (c[0] - a[0]) - (b[0] - a[0]) * (c[1] - a[1])

    def on_segment(a, b, c):
        return (min(a[1], b[1]) - 1e-15 <= c[1] <= max(a[1], b[1]) + 1e-15
                and min(a[0], b[0]) - 1e-15 <= c[0] <= max(a[0], b[0]) + 1e-15)

    o1, o2 = orient(p, q, r), orient(p, q, s)
    o3, o4 = orient(r, s, p), orient(r, s, q)
    if (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0):
        return True
    if abs(o1) < 1e-15 and on_segment(p, q, r):
        return True
    if abs(o2) < 1e-15 and on_segment(p, q, s):
        return True
    if abs(o3) < 1e-15 and on_segment(r, s, p):
        return True
    if abs(o4) < 1e-15 and on_segment(r, s, q):
        return True
    return False


def _count_building_collisions(track, buildings):
    inside = 0
    crossing = 0
    for lat, lon in track:
        if any(point_in_polygon(lat, lon, b.polygon) for b in buildings):
            inside += 1
    for a, b in zip(track, track[1:]):
        hit = False
        for building in buildings:
            polygon = building.polygon
            for index in range(len(polygon)):
                if _segments_cross(a, b, tuple(polygon[index]),
                                   tuple(polygon[(index + 1) % len(polygon)])):
                    hit = True
                    break
            if hit:
                break
        if hit:
            crossing += 1
    return inside, crossing


class OsmRouteRegressionTests(unittest.TestCase):
    def test_auto_routes_counted_run_to_osm(self):
        self.assertEqual(resolve_path_source("auto", 1), "osm")
        self.assertEqual(resolve_path_source("auto", 6), "synthetic")
        self.assertEqual(resolve_path_source("synthetic", 1), "synthetic")
        self.assertEqual(resolve_path_source("osm", 1), "osm")

    def test_real_checkpoints_3000m_avoid_all_buildings(self):
        planner = TrackPlanner.from_campus("xuchang_280173698")
        plan = planner.plan(REAL_CHECKPOINTS, 3000, policy=1)

        checkpoints = [
            {
                "lat": lat,
                "lng": lon,
                "glat": lat,
                "glon": lon,
                "pointName": "P%d" % index,
                "position": index,
            }
            for index, (lat, lon) in enumerate(REAL_CHECKPOINTS)
        ]
        builder = RunningTrackBuilder(1, 2, 1)
        gps_points, _ = builder.generate_track_around_points(
            center_lat=REAL_CHECKPOINTS[0][0],
            center_lng=REAL_CHECKPOINTS[0][1],
            checkpoints=checkpoints,
            total_distance_m=3000,
            total_time_sec=1060,
            start_time_ms=1794466439000,
            route_polyline=plan.polyline,
            route_points=REAL_CHECKPOINTS,
        )
        track = [(point.lat, point.lon) for point in gps_points]

        inside, crossing = _count_building_collisions(track, planner.campus.buildings)
        self.assertEqual(inside, 0)
        self.assertEqual(crossing, 0)
        # 上传前兜底使用同一份校园数据，不应报错。
        planner.validate_track(track)
        self.assertAlmostEqual(
            sum(_dist_m(a, b) for a, b in zip(track, track[1:])), 3000, delta=1.0)

        for checkpoint in REAL_CHECKPOINTS:
            nearest = min(_dist_m(checkpoint, point) for point in track)
            self.assertLess(nearest, 0.01)

    def test_validate_track_rejects_building_and_allows_access_corridor(self):
        planner = TrackPlanner.from_campus("xuchang_280173698")
        building = planner.campus.buildings[0]
        lat = sum(point[0] for point in building.polygon) / len(building.polygon)
        lon = sum(point[1] for point in building.polygon) / len(building.polygon)
        with self.assertRaises(OsmRouteError) as caught:
            planner.validate_track([(lat, lon), (lat, lon)])
        self.assertEqual(caught.exception.code, "osm_building_collision")
        # 官方点落在建筑内时，规划器会记录接入通道建筑；该建筑放行。
        planner.validate_track([(lat, lon), (lat, lon)],
                               allow_building_ids=[building.osm_id])


if __name__ == "__main__":
    unittest.main()
