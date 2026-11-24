# -*- coding: utf-8 -*-
"""OSM 校园数据：加载、WGS84 → GCJ-02 转换与纯标准库几何工具。

坐标约定：本包内所有 lat/lon 都已经是 GCJ-02。WGS84 只出现在
`tools/build_osm_campus.py` 的 OSM 原始输入里，预处理时统一转换，
运行时不再混用两套坐标。
"""
import json
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..consistency import distance

Point = Tuple[float, float]
EARTH_RADIUS_M = 6371000.0
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

# GCJ-02 标准偏移算法参数（国家测绘局公开算法，纯标准库实现）。
_A = 6378245.0
_EE = 0.00669342162296594323


class OsmRouteError(Exception):
    """OSM 路线规划失败；``code`` 对应方案文档 §15.2 的错误码。"""

    def __init__(self, code: str, message: str, diagnostics: Optional[Dict[str, Any]] = None):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.diagnostics = dict(diagnostics or {})


def _transform_lat(x: float, y: float) -> float:
    ret = (-100.0 + 2.0*x + 3.0*y + 0.2*y*y + 0.1*x*y + 0.2*math.sqrt(abs(x)))
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(2.0*x*math.pi)) * 2.0/3.0
    ret += (20.0*math.sin(y*math.pi) + 40.0*math.sin(y/3.0*math.pi)) * 2.0/3.0
    ret += (160.0*math.sin(y/12.0*math.pi) + 320.0*math.sin(y*math.pi/30.0)) * 2.0/3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (300.0 + x + 2.0*y + 0.1*x*x + 0.1*x*y + 0.1*math.sqrt(abs(x)))
    ret += (20.0*math.sin(6.0*x*math.pi) + 20.0*math.sin(2.0*x*math.pi)) * 2.0/3.0
    ret += (20.0*math.sin(x*math.pi) + 40.0*math.sin(x/3.0*math.pi)) * 2.0/3.0
    ret += (150.0*math.sin(x/12.0*math.pi) + 300.0*math.sin(x/30.0*math.pi)) * 2.0/3.0
    return ret


def out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def wgs84_to_gcj02(lat: float, lon: float) -> Point:
    """WGS84 → GCJ-02；境外坐标原样返回。"""
    lat, lon = float(lat), float(lon)
    if out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon-105.0, lat-35.0)
    dlon = _transform_lon(lon-105.0, lat-35.0)
    radlat = math.radians(lat)
    magic = 1 - _EE*math.sin(radlat)**2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat*180.0) / ((_A*(1-_EE)) / (magic*sqrtmagic) * math.pi)
    dlon = (dlon*180.0) / (_A/sqrtmagic*math.cos(radlat) * math.pi)
    return lat+dlat, lon+dlon


class LocalProjection:
    """以 origin 为原点的等距圆柱投影，单位米；只用于校园尺度。"""

    def __init__(self, origin: Point):
        self.lat0, self.lon0 = float(origin[0]), float(origin[1])
        self._cos = math.cos(math.radians(self.lat0))

    def xy(self, lat: float, lon: float) -> Tuple[float, float]:
        x = math.radians(lon-self.lon0) * EARTH_RADIUS_M * self._cos
        y = math.radians(lat-self.lat0) * EARTH_RADIUS_M
        return x, y

    def latlon(self, x: float, y: float) -> Point:
        lat = self.lat0 + math.degrees(y/EARTH_RADIUS_M)
        lon = self.lon0 + math.degrees(x/(EARTH_RADIUS_M*self._cos))
        return lat, lon


def point_in_polygon(lat: float, lon: float, polygon: Sequence[Point]) -> bool:
    """射线法；polygon 为 [lat, lon] 序列，首尾可闭合可不闭合。"""
    if len(polygon) < 3:
        return False
    x, y = float(lon), float(lat)
    inside = False
    j = len(polygon) - 1
    for i in range(len(polygon)):
        xi, yi = float(polygon[i][1]), float(polygon[i][0])
        xj, yj = float(polygon[j][1]), float(polygon[j][0])
        if ((yi > y) != (yj > y)) and (x < (xj-xi)*(y-yi)/(yj-yi) + xi):
            inside = not inside
        j = i
    return inside


def point_in_polygon_xy(x: float, y: float, polygon_xy: Sequence[Tuple[float, float]]) -> bool:
    if len(polygon_xy) < 3:
        return False
    inside = False
    j = len(polygon_xy) - 1
    for i in range(len(polygon_xy)):
        xi, yi = polygon_xy[i]
        xj, yj = polygon_xy[j]
        if ((yi > y) != (yj > y)) and (x < (xj-xi)*(y-yi)/(yj-yi) + xi):
            inside = not inside
        j = i
    return inside


def point_segment_distance(x: float, y: float,
                           a: Tuple[float, float],
                           b: Tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx-ax, by-ay
    if dx == 0 and dy == 0:
        return math.hypot(x-ax, y-ay)
    t = ((x-ax)*dx + (y-ay)*dy) / (dx*dx + dy*dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(x-(ax+t*dx), y-(ay+t*dy))


def point_polygon_distance(x: float, y: float,
                           polygon_xy: Sequence[Tuple[float, float]]) -> float:
    """点到多边形边界的最近距离；点在多边形内返回 0。"""
    if point_in_polygon_xy(x, y, polygon_xy):
        return 0.0
    best = float("inf")
    for a, b in zip(polygon_xy, list(polygon_xy[1:]) + [polygon_xy[0]]):
        best = min(best, point_segment_distance(x, y, a, b))
    return best


def polyline_length(points: Sequence[Point]) -> float:
    return sum(distance(a, b) for a, b in zip(points, points[1:]))


def polygon_centroid(polygon: Sequence[Point]) -> Point:
    if not polygon:
        raise ValueError("empty polygon")
    lat = sum(p[0] for p in polygon) / len(polygon)
    lon = sum(p[1] for p in polygon) / len(polygon)
    return lat, lon


@dataclass
class Building:
    osm_id: int
    polygon: List[Point]
    name: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Building":
        return cls(int(data.get("osm_id", 0)),
                   [(float(p[0]), float(p[1])) for p in data.get("polygon") or []],
                   str(data.get("name") or ""))


@dataclass
class Road:
    osm_id: int
    polyline: List[Point]
    highway: str = ""
    name: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Road":
        return cls(int(data.get("osm_id", 0)),
                   [(float(p[0]), float(p[1])) for p in data.get("polyline") or []],
                   str(data.get("highway") or ""),
                   str(data.get("name") or ""))


@dataclass
class CampusData:
    campus_id: str
    name: str
    center: Point
    bbox: Dict[str, float]
    boundary: List[Point]
    buildings: List[Building]
    roads: List[Road]
    obstacles: List[Any]
    meta: Dict[str, Any]

    @property
    def origin(self) -> Point:
        return self.bbox["minlat"], self.bbox["minlon"]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CampusData":
        if not isinstance(data, dict):
            raise ValueError("校园数据必须是 JSON 对象")
        bbox = data.get("bbox") or {}
        required = ("minlat", "minlon", "maxlat", "maxlon")
        if any(key not in bbox for key in required):
            raise ValueError("校园数据 bbox 不完整")
        boundary = [(float(p[0]), float(p[1])) for p in data.get("boundary") or []]
        if len(boundary) < 3:
            raise ValueError("校园边界至少需要 3 个点")
        center = data.get("center") or {
            "lat": (bbox["minlat"]+bbox["maxlat"])/2,
            "lon": (bbox["minlon"]+bbox["maxlon"])/2,
        }
        return cls(
            campus_id=str(data.get("campus_id") or ""),
            name=str(data.get("name") or ""),
            center=(float(center["lat"]), float(center["lon"])),
            bbox={key: float(bbox[key]) for key in required},
            boundary=boundary,
            buildings=[Building.from_dict(item) for item in data.get("buildings") or []],
            roads=[Road.from_dict(item) for item in data.get("roads") or []],
            obstacles=list(data.get("obstacles") or []),
            meta=dict(data.get("meta") or {}),
        )

    def contains(self, lat: float, lon: float) -> bool:
        return point_in_polygon(lat, lon, self.boundary)

    def building_at(self, lat: float, lon: float) -> Optional[Building]:
        for building in self.buildings:
            if point_in_polygon(lat, lon, building.polygon):
                return building
        return None


def campus_data_filename(campus_id: str) -> str:
    safe = str(campus_id).strip().replace("/", "_").replace("\\", "_")
    return f"campus_{safe}.json"


def grid_data_filename(campus_id: str) -> str:
    safe = str(campus_id).strip().replace("/", "_").replace("\\", "_")
    return f"campus_{safe}_grid.json"


def resolve_data_path(campus_id: str, filename: str, data_dir: Optional[str] = None) -> str:
    """campus_id 可以是纯 id、文件名或完整路径。"""
    raw = str(campus_id).strip()
    if raw.lower().endswith(".json") and os.path.isfile(raw):
        return os.path.abspath(raw)
    base = data_dir or DATA_DIR
    if raw.lower().endswith(".json") and filename.startswith("campus_"):
        stem = os.path.basename(raw)
        if filename.endswith("_grid.json"):
            return os.path.join(base, stem[:-5] + "_grid.json")
        return os.path.join(base, stem)
    return os.path.join(base, filename)


def load_campus(campus_id: str = "xuchang_280173698",
                data_dir: Optional[str] = None) -> CampusData:
    path = resolve_data_path(campus_id, campus_data_filename(campus_id), data_dir)
    if not os.path.isfile(path):
        raise OsmRouteError("osm_campus_not_found", f"找不到校园数据: {path}")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return CampusData.from_dict(json.load(handle))
    except (OSError, ValueError) as exc:
        raise OsmRouteError("osm_campus_not_found", f"校园数据无法解析: {path}: {exc}") from None
