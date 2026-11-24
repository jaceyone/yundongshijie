#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PC 预处理：把 Overpass OSM 快照转成 APK 可打包的校园数据与代价栅格。

输入（默认 `_analysis/osm_xuchang/data/`）：
  - overpass_campus_geometry.json  校园边界（way/280173698）
  - overpass_campus_features.json  bbox 内建筑 / 道路

输出（默认 `协议/7.3.92/running/data/`）：
  - campus_xuchang_280173698.json
  - campus_xuchang_280173698_grid.json

数据只用于离线规划；OSM 数据按 ODbL 许可分发，APK 内需保留署名。
本脚本不参与 APK 运行时，也不访问网络。
"""
import argparse
import json
import math
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROTOCOL_DIR = os.path.join(ROOT, "协议", "7.3.92")
if PROTOCOL_DIR not in sys.path:
    sys.path.insert(0, PROTOCOL_DIR)

from running.osm.campus import (  # noqa: E402
    LocalProjection,
    point_in_polygon_xy,
    point_polygon_distance,
    point_segment_distance,
    polygon_centroid,
    wgs84_to_gcj02,
)
from running.osm.grid import (  # noqa: E402
    CELL_BLOCKED,
    CELL_NEAR_BUILDING,
    CELL_NEAR_ROAD,
    CELL_OPEN,
    CELL_ROAD,
    LEGEND,
)

DEFAULT_CAMPUS_ID = "xuchang_280173698"
DEFAULT_OSM_ID = 280173698


def load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def element_geometry(element):
    """Overpass `out geom` 的 way 几何 → [(lat, lon), ...]（WGS84）。"""
    points = []
    for item in element.get("geometry") or []:
        if not isinstance(item, dict) or "lat" not in item or "lon" not in item:
            raise ValueError(f"OSM way {element.get('id')} 的 geometry 不完整")
        points.append((float(item["lat"]), float(item["lon"])))
    return points


def to_gcj(points):
    return [wgs84_to_gcj02(lat, lon) for lat, lon in points]


def convex_hull(points):
    """Andrew monotone chain；points 为 [(lat, lon), ...]。"""
    unique = sorted(set((float(lat), float(lon)) for lat, lon in points))
    if len(unique) <= 2:
        return unique

    def cross(o, a, b):
        return (a[0]-o[0])*(b[1]-o[1]) - (a[1]-o[1])*(b[0]-o[0])

    lower = []
    for point in unique:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], point) <= 0:
            lower.pop()
        lower.append(point)
    upper = []
    for point in reversed(unique):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], point) <= 0:
            upper.pop()
        upper.append(point)
    return lower[:-1] + upper[:-1]


def _orientation(ax, ay, bx, by, cx, cy):
    value = (by-ay)*(cx-bx) - (bx-ax)*(cy-by)
    if abs(value) < 1e-12:
        return 0
    return 1 if value > 0 else -1


def _on_segment(ax, ay, bx, by, cx, cy):
    return (min(ax, bx)-1e-9 <= cx <= max(ax, bx)+1e-9
            and min(ay, by)-1e-9 <= cy <= max(ay, by)+1e-9)


def _segments_intersect(a, b, c, d):
    ax, ay = a; bx, by = b; cx, cy = c; dx, dy = d
    o1 = _orientation(ax, ay, bx, by, cx, cy)
    o2 = _orientation(ax, ay, bx, by, dx, dy)
    o3 = _orientation(cx, cy, dx, dy, ax, ay)
    o4 = _orientation(cx, cy, dx, dy, bx, by)
    if o1 != o2 and o3 != o4:
        return True
    if o1 == 0 and _on_segment(ax, ay, bx, by, cx, cy):
        return True
    if o2 == 0 and _on_segment(ax, ay, bx, by, dx, dy):
        return True
    if o3 == 0 and _on_segment(cx, cy, dx, dy, ax, ay):
        return True
    if o4 == 0 and _on_segment(cx, cy, dx, dy, bx, by):
        return True
    return False


def _polyline_intersects_boundary(points_xy, boundary_xy):
    edges = list(zip(boundary_xy, boundary_xy[1:] + boundary_xy[:1]))
    for a, b in zip(points_xy, points_xy[1:]):
        for c, d in edges:
            if _segments_intersect(a, b, c, d):
                return True
    return False


def buffer_polygon(polygon, buffer_m):
    """凸包 + 从形心径向外扩 buffer_m；用于包含围栏外的官方点位。"""
    if buffer_m <= 0:
        return [(float(p[0]), float(p[1])) for p in polygon]
    hull = convex_hull(polygon)
    if len(hull) < 3:
        raise ValueError("凸包点不足 3 个，无法外扩")
    lat0 = sum(p[0] for p in hull)/len(hull)
    lon0 = sum(p[1] for p in hull)/len(hull)
    projection = LocalProjection((lat0, lon0))
    cx, cy = projection.xy(lat0, lon0)
    expanded = []
    for lat, lon in hull:
        x, y = projection.xy(lat, lon)
        dx, dy = x-cx, y-cy
        distance = math.hypot(dx, dy)
        if distance < 1e-9:
            continue
        scale = (distance+buffer_m)/distance
        expanded.append(projection.latlon(cx+dx*scale, cy+dy*scale))
    if len(expanded) < 3:
        raise ValueError("外扩后边界点不足 3 个")
    expanded.append(expanded[0])
    return expanded


def find_campus_element(document, osm_id):
    for element in document.get("elements") or []:
        if element.get("type") == "way" and int(element.get("id", -1)) == int(osm_id):
            return element
    raise ValueError(f"Overpass 响应里找不到 way/{osm_id}")


def build_campus(geometry_doc, features_doc, campus_id, osm_id, fence=None,
                 boundary_buffer_m=0.0):
    campus_element = find_campus_element(geometry_doc, osm_id)
    boundary_wgs = element_geometry(campus_element)
    if len(boundary_wgs) < 3:
        raise ValueError("校园边界少于 3 个点")

    buildings = []
    roads = []
    for element in features_doc.get("elements") or []:
        tags = element.get("tags") or {}
        points = element_geometry(element)
        if len(points) < 2:
            continue
        converted = to_gcj(points)
        if "building" in tags:
            if len(converted) >= 3:
                buildings.append({
                    "osm_id": int(element.get("id", 0)),
                    "name": str(tags.get("name") or ""),
                    "polygon": [[lat, lon] for lat, lon in converted],
                })
        elif "highway" in tags:
            roads.append({
                "osm_id": int(element.get("id", 0)),
                "highway": str(tags.get("highway") or ""),
                "name": str(tags.get("name") or ""),
                "polyline": [[lat, lon] for lat, lon in converted],
            })

    if fence is not None:
        # 官方 getGeoFenceForRun 的围栏已经是 GCJ-02，直接作为权威边界；
        # OSM 的校园 way 只保留用于数据来源与名称。
        boundary = [(float(p[0]), float(p[1])) for p in fence.get("polygon") or []]
        if len(boundary) < 3:
            raise ValueError("官方围栏少于 3 个点")
        boundary_source = "getGeoFenceForRun"
    else:
        boundary = to_gcj(boundary_wgs)
        boundary_source = "OpenStreetMap"
    if boundary_buffer_m > 0:
        boundary = buffer_polygon(boundary, boundary_buffer_m)
        boundary_source = f"{boundary_source}+buffer{boundary_buffer_m:g}m"
    lats = [point[0] for point in boundary]
    lons = [point[1] for point in boundary]
    projection = LocalProjection((min(lats), min(lons)))
    boundary_xy = [projection.xy(lat, lon) for lat, lon in boundary]
    buildings = [
        building for building in buildings
        if point_in_polygon_xy(
            *projection.xy(*polygon_centroid([(p[0], p[1]) for p in building["polygon"]])),
            boundary_xy)
    ]
    roads = [
        road for road in roads
        if any(point_in_polygon_xy(*projection.xy(lat, lon), boundary_xy)
               for lat, lon in road["polyline"])
        or _polyline_intersects_boundary(
            [projection.xy(lat, lon) for lat, lon in road["polyline"]], boundary_xy)
    ]
    timestamp = ((geometry_doc.get("osm3s") or {}).get("timestamp_osm_base")
                 or (features_doc.get("osm3s") or {}).get("timestamp_osm_base")
                 or "")
    tags = campus_element.get("tags") or {}
    return {
        "campus_id": campus_id,
        "osm_object": f"way/{osm_id}",
        "name": str(tags.get("name") or "许昌学院"),
        "name_en": str(tags.get("name:en") or "Xuchang University"),
        "center": {
            "lat": round((min(lats)+max(lats))/2, 7),
            "lon": round((min(lons)+max(lons))/2, 7),
        },
        "bbox": {
            "minlat": round(min(lats), 7),
            "minlon": round(min(lons), 7),
            "maxlat": round(max(lats), 7),
            "maxlon": round(max(lons), 7),
        },
        "boundary": [[lat, lon] for lat, lon in boundary],
        "buildings": buildings,
        "roads": roads,
        "obstacles": [],
        "meta": {
            "source": "OpenStreetMap",
            "license": "ODbL",
            "timestamp": timestamp,
            "coordinate_system": "GCJ-02",
            "boundary_source": boundary_source,
            "boundary_buffer_m": boundary_buffer_m,
            "processor": "tools/build_osm_campus.py",
        },
    }


def _cell_range(resolution, min_x, min_y, max_x, max_y, width, height):
    col0 = max(0, int(math.floor(min_x/resolution)) - 1)
    col1 = min(width-1, int(math.ceil(max_x/resolution)) + 1)
    row0 = max(0, int(math.floor(min_y/resolution)) - 1)
    row1 = min(height-1, int(math.ceil(max_y/resolution)) + 1)
    return col0, col1, row0, row1


def build_grid(campus, resolution_m=2.0, building_buffer_m=1.5,
               near_building_m=3.0, near_road_m=5.0, road_width_m=1.5):
    bbox = campus["bbox"]
    origin = (bbox["minlat"], bbox["minlon"])
    projection = LocalProjection(origin)
    max_x, max_y = projection.xy(bbox["maxlat"], bbox["maxlon"])
    width = max(1, int(math.ceil(max_x/resolution_m)))
    height = max(1, int(math.ceil(max_y/resolution_m)))
    costs = bytearray([CELL_BLOCKED])*(width*height)

    boundary_xy = [projection.xy(lat, lon) for lat, lon in campus["boundary"]]

    # 1. 校园边界内先记为可走空地；边界外保持 blocked。
    for row in range(height):
        y = (row+0.5)*resolution_m
        base = row*width
        for col in range(width):
            x = (col+0.5)*resolution_m
            if point_in_polygon_xy(x, y, boundary_xy):
                costs[base+col] = CELL_OPEN

    # 2. 建筑：轮廓内 + 1.5 m 缓冲不可走；外侧 3 m 内高代价。
    for building in campus["buildings"]:
        polygon_xy = [projection.xy(lat, lon) for lat, lon in building["polygon"]]
        xs = [p[0] for p in polygon_xy]
        ys = [p[1] for p in polygon_xy]
        margin = max(near_building_m, building_buffer_m) + resolution_m
        col0, col1, row0, row1 = _cell_range(
            resolution_m,
            min(xs)-margin, min(ys)-margin, max(xs)+margin, max(ys)+margin,
            width, height)
        for row in range(row0, row1+1):
            y = (row+0.5)*resolution_m
            for col in range(col0, col1+1):
                index = row*width + col
                distance = point_polygon_distance((col+0.5)*resolution_m, y, polygon_xy)
                if distance <= building_buffer_m:
                    costs[index] = CELL_BLOCKED
                elif distance <= near_building_m and costs[index] != CELL_BLOCKED:
                    costs[index] = CELL_NEAR_BUILDING

    # 3. 道路：中心线 1.5 m 内最低代价，5 m 内次低；不覆盖 blocked。
    for road in campus["roads"]:
        points = road["polyline"]
        for a_latlon, b_latlon in zip(points, points[1:]):
            ax, ay = projection.xy(*a_latlon)
            bx, by = projection.xy(*b_latlon)
            margin = near_road_m + resolution_m
            col0, col1, row0, row1 = _cell_range(
                resolution_m,
                min(ax, bx)-margin, min(ay, by)-margin,
                max(ax, bx)+margin, max(ay, by)+margin,
                width, height)
            for row in range(row0, row1+1):
                y = (row+0.5)*resolution_m
                for col in range(col0, col1+1):
                    index = row*width + col
                    if costs[index] == CELL_BLOCKED:
                        continue
                    distance = point_segment_distance(
                        (col+0.5)*resolution_m, y, (ax, ay), (bx, by))
                    if distance <= road_width_m:
                        costs[index] = CELL_ROAD
                    elif distance <= near_road_m and costs[index] in (
                            CELL_OPEN, CELL_NEAR_BUILDING):
                        costs[index] = CELL_NEAR_ROAD

    return {
        "campus_id": campus["campus_id"],
        "resolution_m": resolution_m,
        "origin": {"lat": origin[0], "lon": origin[1]},
        "width": width,
        "height": height,
        "cost_runs": _rle(costs),
        "legend": LEGEND,
    }


def _rle(costs):
    runs = []
    if not costs:
        return runs
    current = costs[0]
    count = 1
    for value in costs[1:]:
        if value == current:
            count += 1
        else:
            runs.append({"value": int(current), "count": count})
            current = value
            count = 1
    runs.append({"value": int(current), "count": count})
    return runs


def validate_campus(campus):
    boundary = campus["boundary"]
    projection = LocalProjection((campus["bbox"]["minlat"], campus["bbox"]["minlon"]))
    boundary_xy = [projection.xy(lat, lon) for lat, lon in boundary]
    outside = []
    for building in campus["buildings"]:
        lat, lon = polygon_centroid([(p[0], p[1]) for p in building["polygon"]])
        if not point_in_polygon_xy(*projection.xy(lat, lon), boundary_xy):
            outside.append(building["osm_id"])
    return {
        "boundary_closed": boundary[0] == boundary[-1],
        "boundary_points": len(boundary),
        "buildings": len(campus["buildings"]),
        "buildings_outside_boundary": outside,
        "roads": len(campus["roads"]),
    }


def main():
    parser = argparse.ArgumentParser(
        description="把 OSM Overpass 快照预处理成校园 JSON 与代价栅格")
    parser.add_argument("--input-dir",
                        default=os.path.join(ROOT, "_analysis", "osm_xuchang", "data"))
    parser.add_argument("--output-dir",
                        default=os.path.join(PROTOCOL_DIR, "running", "data"))
    parser.add_argument("--geometry",
                        help="Overpass 校园边界 JSON（默认 <input-dir>/overpass_campus_geometry.json）")
    parser.add_argument("--features",
                        help="Overpass 建筑/道路 JSON（默认 <input-dir>/overpass_campus_features.json）")
    parser.add_argument("--fence-gcj",
                        help="官方 getGeoFenceForRun 围栏 JSON（GCJ-02，含 polygon 数组）；"
                             "提供时用它替代 OSM 校园 way 作为边界")
    parser.add_argument("--boundary-buffer-m", type=float, default=0.0,
                        help="边界外扩米数（凸包+径向），用于包含围栏外的官方点位；默认 0")
    parser.add_argument("--campus", default=DEFAULT_CAMPUS_ID)
    parser.add_argument("--osm-id", type=int, default=DEFAULT_OSM_ID)
    parser.add_argument("--resolution", type=float, default=2.0,
                        help="栅格分辨率（米，默认 2.0）")
    parser.add_argument("--building-buffer", type=float, default=1.5,
                        help="建筑轮廓外扩不可走缓冲（米，默认 1.5）")
    parser.add_argument("--near-building", type=float, default=3.0,
                        help="建筑高代价区半径（米，默认 3.0）")
    parser.add_argument("--near-road", type=float, default=5.0,
                        help="道路次低代价区半径（米，默认 5.0）")
    parser.add_argument("--road-width", type=float, default=1.5,
                        help="道路中心线低代价半宽（米，默认 1.5）")
    args = parser.parse_args()

    geometry_path = args.geometry or os.path.join(
        args.input_dir, "overpass_campus_geometry.json")
    features_path = args.features or os.path.join(
        args.input_dir, "overpass_campus_features.json")
    for path in (geometry_path, features_path):
        if not os.path.isfile(path):
            raise SystemExit(f"缺少输入文件: {path}")
    fence = None
    if args.fence_gcj:
        fence = load_json(args.fence_gcj)
        if not isinstance(fence.get("polygon"), list):
            raise SystemExit(f"--fence-gcj 缺少 polygon 数组: {args.fence_gcj}")

    campus = build_campus(load_json(geometry_path), load_json(features_path),
                          args.campus, args.osm_id, fence=fence,
                          boundary_buffer_m=args.boundary_buffer_m)
    report = validate_campus(campus)
    grid = build_grid(campus, resolution_m=args.resolution,
                      building_buffer_m=args.building_buffer,
                      near_building_m=args.near_building,
                      near_road_m=args.near_road,
                      road_width_m=args.road_width)

    os.makedirs(args.output_dir, exist_ok=True)
    campus_path = os.path.join(args.output_dir, f"campus_{args.campus}.json")
    grid_path = os.path.join(args.output_dir, f"campus_{args.campus}_grid.json")
    with open(campus_path, "w", encoding="utf-8") as handle:
        json.dump(campus, handle, ensure_ascii=False, separators=(",", ":"))
    with open(grid_path, "w", encoding="utf-8") as handle:
        json.dump(grid, handle, ensure_ascii=False, separators=(",", ":"))

    counts = {name: 0 for name in LEGEND.values()}
    total = grid["width"]*grid["height"]
    for run in grid["cost_runs"]:
        counts[LEGEND[str(run["value"])]] += run["count"]

    print(f"校园数据: {campus_path}")
    print(f"栅格数据: {grid_path}")
    print(f"边界: 闭合={report['boundary_closed']} 点数={report['boundary_points']}")
    print(f"建筑: {report['buildings']} 栋 边界外={len(report['buildings_outside_boundary'])}")
    print(f"道路: {report['roads']} 条")
    print(f"栅格: {grid['width']}×{grid['height']}={total} 格 "
          f"@{grid['resolution_m']}m 可走={total-counts['blocked']}")
    print("代价分布: " + " ".join(f"{name}={count}"
                                for name, count in counts.items()))


if __name__ == "__main__":
    main()
